# Echo Button firmware: hardware and chip reference (CYW20735**B0**)

Everything known about reading, writing and replacing the Echo Button's firmware through its
test pads. For the hands-on procedure (soldering, flashing, setup) see
[`../firmware/GUIDE.md`](../firmware/GUIDE.md). Facts marked **verified** were confirmed on
hardware (two rev-1 units, November 2026).

What is published and what isn't: [`../firmware/prebuilt/`](../firmware/prebuilt/) has AButton
builds. The stock firmware isn't published (it's Amazon's; an image of the DS bank is
available from the maintainer on request). A full dump of your own button stays
private: sector 0 holds its BD_ADDR, radio calibration and serial numbers, and sector
`0x2000` its pairing link keys. The ROM is Cypress's and isn't published.

## The SoC

Cypress/Broadcom **CYW20735B0** (the DS identifies the die as `BCM20735B0 fcbga`).
Cortex-M4, 2 MiB ROM and 384 KiB RAM on chip, Classic and LE Bluetooth on one radio.
The firmware lives in an **external 512 KiB SPI NOR flash**: JEDEC `1F 84 01`, Adesto
AT25SF041. It ships with block protection set (status `0x0c`), so an erase has to clear it
first (below).

## Test pads

On the PCB side facing the battery compartment. The **wikidevi** map is right; the
"SWD-on-TMF" map in some teardowns is wrong (TMF2 is GND):

| Pad | Signal | Pad | Signal |
|---|---|---|---|
| TMF1 | VCC 3.3 V | TMF9 | HCI UART TXD (chip → host) |
| TMF2 | GND | TMF10 | HCI UART CTS_N (chip input) |
| TMF3 | battery | TMF11 | HCI UART RXD (host → chip) |
| TMF7 | button | TMF12 | HCI UART RTS_N (chip output) |
| TMF8 | SPI MOSI | TMF22 | LHL_VDDO |

**SWD** is on **J2**, a 2×3 Tag-Connect (TC2030-style) footprint, not on the TMF row (not
used here). Only MOSI of the flash's SPI bus is broken out, so the flash is read and
written **through the SoC**, over the HCI UART.

## Board pins and power (rev 1, verified)

Decoded from the stock firmware's rev-1 board init (`0x21b7ec`; the board revision is byte
`0xC20` of the flash) and confirmed by AButton:

| Pin | Function | Notes |
|---|---|---|
| **P5** | **power latch**: high = stay on, low = off | stock: `0x21bf10(1)` drives it high at the end of board init (after BTM_ENABLED) |
| P6 | button, active low | stock configures pull-up + both-edge IRQ (`0x40D`), but **the edge interrupt never fires** with the SDK HAL (verified: pin reads 0 on every press, IRQ count stays 0); AButton polls it every 10 ms |
| P13 | battery-divider enable | high only while reading ADC P0; with P13 low P0 reads < 0.9 V |
| ADC P0 (enum `0xB`) | battery | read with P13 high; battery % = `(mV − 900) / 7`, clamped 0–100 (stock's formula); fresh cells read 100 % |
| P27 / P28 / P29 | LED R / G / B = PWM1 / 2 / 3 | ACLK1 256 kHz, PWM init `0xFDFF` (512-tick period), toggle `0xFFFF − 2.008·v`; white balance R 1.0 / G 0.407 / B 0.6 |

Rev-2 boards use other pins (button P16, battery ADC P1; table at `0x21b650`); the P5 latch
is rev-1 only and rev 2's power hold is unknown. AButton supports rev 1 only.

**Power behaviour (verified).**
- A press powers the board for **about 2 s, even if the button is held**. Firmware has to
  drive **P5 high** to stay on. Firmware that doesn't dies about 2 s after every press and
  looks exactly like firmware that never booted.
- The stock "sleep after 10 idle minutes" is a **hard power-off**: `0x21becc` arms a
  600 000 ms timer whose callback `0x21bf68` → `0x21bf10(0)` drives P13 and P5 low. The next
  press powers the board from cold, and is lost (it only boots the chip).
- A software reset (after an OTA update) keeps the board powered; it comes back by itself.
- On the flashing rig, each power-up re-enumerates the USB-serial adapter (back-feed
  through the data lines), and the port is gone for about 1 s. Nothing the chip prints in
  its first second is seen, and a VM with USB pass-through loses the adapter for good: run
  the tools on a real Linux host.

## Wiring (verified)

A **3.3 V** USB-serial adapter (an FT232 was used), **data lines only**; the board runs from
its batteries:

```
adapter GND ──────────────► TMF2   (GND)
adapter RXD ◄────────────── TMF9   (chip TXD)
adapter TXD ───[1 kΩ]─────► TMF11  (chip RXD)
adapter RTS ───[1 kΩ]─────► TMF10  (chip CTS_N: the tools hold it asserted = low)
TMF1 (3.3 V): leave unconnected
```

- **Don't power the board from the adapter's 3V3 pin:** it browns out on inrush.
- The 1 kΩ resistors limit back-feed from the adapter's outputs into the unpowered board.
  Recommended; the units were also flashed without them.
- There is **no boot strap**: holding CTS low at power-up does nothing (an old belief,
  disproved from the ROM).

Untried fallback, for a unit whose firmware boots far enough to block the UART but is
unusable: hold **TMF8 (SPI MOSI) to GND** with a momentary switch while pressing the button,
so the ROM can't read `BRCMcfgS` and drops into its download handler; release it as soon as
`HCI_Reset` is acknowledged (flash access needs MOSI). TMF8 is a chip output: keep the short
to about a second.

## Who answers on the HCI UART

The HCI handshake is always the same: host `01 03 0C 00` (HCI_Reset), chip
`04 0E 04 01 03 0C 00`. What happens after depends on what runs:

The 2-second press windows matter only in the first row, a unit with bad firmware; a working
button is flashed in one go (stock) or over the air (AButton).

| Running | Vendor commands (READ/WRITE/LAUNCH_RAM) | How to flash |
|---|---|---|
| **ROM boot-download handler**: the DS is broken or missing, for the ~2 s a press gives | accepted; flash reads `0x12` until the flash is detected | `echobtn_resume_flash.py`, a few sectors per press |
| **Stock firmware** (awake for 10 min after a press) | usually accepted; `0x0C` while it is busy (e.g. reconnecting) | `echobtn_resume_flash.py`, one press, about 25 s |
| **AButton** (any working build) | always `0x0C` Command Disallowed | over the air only (`firmware/abutton_ble.py ota`) |

**Why AButton refuses (from the ROM):** `mpaf_df_hci_cmd_filter` (`0x745bc`) routes every
HCI command except OGF 4 to `mpaf_hci_cmd_handler` while the embedded Bluetooth stack is up
(`btu_hcievt_handler`, `0x201f4c`, non-NULL), and that handler answers anything not in its
small table `mpaf_hcicmd_tbl` with `0x0C`. Stock evidently runs without that hook at times.

**The ROM boot-download handler (verified).** The ROM boots the SS/DS when it finds
`BRCMcfgS` at flash 0; when the DS doesn't start (for example a missing end record) it ends
up in its download handler (`bt_boot_hci_*`):

- Accepts (dispatcher `0x60d32`): `0xFC2E` Download_Minidriver, `0xFC4C` WRITE_RAM, `0xFC4D`
  READ_RAM, `0xFC4E` LAUNCH_RAM, `0xFC71` WRITE_RAM_Compressed, `0xFFCE` Chip_Erase (**never
  use**: it erases sector 0 too), `0xFFED` download info. No `0xFC18` baud change.
- **Send `0xFC2E` first.** Before it, the ROM's HCI stack keeps live data from `0x2207bc`;
  writing RAM there crashes the chip (vendor event `04 FF … 1B 03 …`, a crash dump).
- **Flash reads return `0x12` until the flash is detected.** The first WRITE_RAM to
  `0xFF……` after power-up runs `nvram_detect()`; writing `FF FF FF FF` (a no-op on NOR) is
  enough. A RAM write first sets the "first write" flag and skips the detect.
- LAUNCH_RAM replies, then calls the target and returns to the command loop, so a small
  RAM stub (the sector erase below) runs between commands.
- At 115200 baud a 4 KB sector erase + write + full read-back takes about 0.5 s: 1–2
  sectors per press.
- RAM: untouched below `0x2207bc`; ROM `.bss` from `0x2207bc`; heap `0x22e3d0`–`0x249394`;
  RAM ends at `0x250000` (reading past it crashes the chip). The B0 minidriver (SDK,
  9472 B at `0x220000`) also loads after `0xFC2E`, but isn't needed.
- Not the 2 s limit: the watchdog (SP805-style at `0x329000`, control at `+8`, about one
  tick per 50 ms). The limit is the press's power pulse.

Vendor commands (opcodes little-endian on the wire):

| Name | Opcode | Command | Reply |
|---|---|---|---|
| READ_RAM | 0xFC4D | `01 4D FC 05 <addr LE32> <n ≤ 251>` | `04 0E <4+n> 01 4D FC <status> <n bytes>` |
| WRITE_RAM | 0xFC4C | `01 4C FC <4+N> <addr LE32> <N bytes>` | `04 0E 04 01 4C FC <status>` |
| LAUNCH_RAM | 0xFC4E | `01 4E FC 04 <addr LE32>` | `04 0E 04 01 4E FC <status>` |

Status `0x00` OK, `0x12` refused by the ROM handler (e.g. flash not detected), `0x0C`
Command Disallowed (the running firmware's stack owns the HCI). The ROM queues a
Command Complete for each HCI_Reset sent while waiting; drain those before the first
real command.

## Serial-flash layout (512 KiB, at `0xFF000000`)

| Offset | Contents | Notes |
|---|---|---|
| `0x00000` | SS `BRCMcfgS`: bootstrap, BD_ADDR (record `0x0300`), calibration | unit-unique; **never write it** |
| `0x00C00` | factory data: 16-char serial, `0xC10` PCB serial, `0xC20` board revision | in sector 0; the stock name is `EchoBtn` + serial[13:16] |
| `0x00D00` | AButton identity record (optional name) | in sector 0 |
| `0x01000` | fail-safe sector: OTA bank switch | erased = boot DS1; DS2 magic + address = boot DS2 |
| `0x02000`–`0x03FFF` | VS: pairing / link keys | unit-unique, private |
| `0x04000`–`0x41FFF` | DS1 `BRCMcfgD`: the firmware | stock ends at `0x1e4b4`; AButton 0.4.0 at `0x19515` (`0x19f0a` debug) |
| `0x42000`–`0x7FFFF` | DS2: the OTA bank | erased on stock units |

**Bank switching (SDK `fw_upgrade_lib`, verified with OTA):** after a verified download the
firmware switches banks by rewriting sector `0x1000`: going to DS2 writes a DS2 magic plus
the bank's address; going back to DS1 erases it. Then it resets. So after an odd number of
OTA updates the unit boots DS2. **To make a UART-written DS1 boot, sector `0x1000` must be
erased too**: always include `0x1000` when flashing (`--sectors 0x1000,0x4000-0x1e000`).

**DS format** (`research/ds_tool.py`): `"BRCMcfgD"` + 4 reserved bytes + a 32-bit length (at
DS+12, counted from DS+16), then records `<id u16 LE> <len LEB128> <data>`. The first is
`0x0303` `"BCM20735B0 fcbga\0"`; code and data are `0x0103` Data records
(`<RAM addr u32> <bytes>`); `0x0110` patch entries; `0x0106` function calls. **The section
ends with the end record `fe 00 00`** (id `0x00FE`, length 0), which the header length
includes. Without it the ROM runs off the end of the DS and never starts the app. **No integrity check:** a stock DS with 4 bytes changed
(`fw_mod.py --press-color`) boots (verified).

Memory map: ROM `0x00000000`–`0x001FFFFF` (reset SP `0x00200400`, PC `0x000003F5`); RAM
from `0x00200000` (app and patches load here); serial flash at `0xFF000000`, aliased every
`0x80000`.

## The B0 SDK

WICED Studio **6.0** ships a `20735-B0_Bluetooth` platform, encrypted and disabled by its
public `config.eml`; `install_wiced_b0_sdk.sh` decrypts and installs it. It has the real B0
ROM symbols (`patches/patch.elf`, 17 887 names), the B0 minidriver and `configdef20735B0.hdf`.
ModusToolbox and WICED 6.2 only have **B1**, whose ROM is different (B1 symbol addresses don't
match B0 code), so B1 tools can't build or flash this chip.

The button's stock patch is the same family as the SDK's but a different revision, so an
SDK-built app can't be spliced onto the stock patch: AButton replaces the whole DS. The SDK's
SS is byte-identical to the stock SS except the BD_ADDR. The SDK can't rebuild the stock app
(no source); changes to it stay binary DS edits (`fw_mod.py`).

## Transmit power and range (measured)

**The button already advertises at its maximum power;** no firmware setting adds range.
Measured 2026-11-19 on a rev-1 unit with a test build: RSSI 1 ft from a USB adapter,
medians over 12 s, comparing the normal adverts with a second advert stream from the same
button (the two interleave, so the room affects both alike).

| Normal adverts set to (ROM `blecm_setTxPowerInADV`) | −16 | 0 | +4 | +8 | +12 | default |
|---|---|---|---|---|---|---|
| Received (dBm) | −65 | −44 | −40 | −34 | −33 | −33 to −38 |

- **`blecm_setTxPowerInADV(int8 dBm)`** (ROM `0x146455`, not in the B0 headers) works, and
  sets the power of **every** advert stream, multi-adv instances included. The default
  matches the top of the range, and +8 and +12 come out within 1 dB of each other. It could
  lower the power, not raise it.
- **The multi-adv power parameter** (`wiced_set_multi_advertisement_params`, documented as
  −21 to +9 dBm) has **no effect**: an instance set anywhere in that range came out within
  3 dB of the normal adverts.
- The ROM's dBm table (`lm_DualMode_TxPwrTable`, RAM `0x280550`, initial copy at ROM
  `0x1c77a8`) has 8 nominal steps, +12 to −16 dBm in 4 dB steps, and clamps requests above
  +12. Neither the stock firmware nor AButton changes it: their RF config records
  (`0x0700`–`0x0704`) are byte-identical, the class 1.5 table (`0x0705`) is absent, and the
  stock app sets no TX power.
- The B0 SDK has no API for LE 2M, Long Range (coded PHY) or extended advertising.
- **Trap:** the multi-adv wrappers (`wiced_set_multi_advertisement_*`,
  `wiced_start_multi_advertisements`) send vendor command `0xFD54` with no callback, and so
  return `BTM_SUCCESS` (0) on success, which reads as `FALSE` for their `wiced_bool_t` type.
  Ignore the return value, as the SDK samples do.

AButton 0.4.0 uses `blecm_setTxPowerInADV` for its transmit power option (lower only).
For more range, put a Bluetooth proxy (an ESP32 with ESPHome) closer to the button.

## Erasing a sector from a LAUNCH_RAM stub (verified)

The flash ships with block protection on (status `0x0c` = BP0+BP1): WREN works, but an
erase is silently ignored. The stub (`ERASE_STUB` in `echobtn_hci_flash.py`) calls only
unpatched ROM leaves, because the high-level `sfi_erase` goes through DS patches that don't
exist in the ROM handler:

0. `WRSR(0)`: `sfi_writeStatus` `0x7e34a`, r0 = 0 (clears BP bits). Required.
1. WREN: `sfi_write_enable` `0x7e316`.
2. Sector erase: send `20 <addr hi> <addr mid> <addr lo>` with `spiffy_write` `0x63734`
   (r0 = 1 for SPIFFY2, the serial flash; r1 = buffer; r2 = transfer length; r3 = write count;
   two stack flags both 1).
3. Poll RDSR (`sfi_read_status` `0x7e2fa`) until bit 0 (WIP) clears.
4. WRDI: `sfi_write_disable` `0x7e484`.

Flash controller: SPIFFY2 at `0x352400` (SPIFFY1 at `0x360600`); `+0x00` cfg, `+0x04`
length, `+0x08` TX FIFO level, `+0x24` IntStatus (bit 19 = done, write 1 to clear),
`+0x100` RX FIFO, `+0x104` TX FIFO. Other leaves: `spiffy_read` `0x6378c`,
`spiffy_write_then_read` `0x637d6`, `sfi_detected` `0x7e24e` (JEDEC id vs table `0x7e54c`).
Patched (absent in the ROM handler): `sfi_sector_erase` `0x7e616`, `0x7e186`, `0x7e448`.

## Tools (this folder; all need only `pyserial`)

| Tool | What it does |
|---|---|
| `echobtn_hci_dump.py` | Dump ROM/RAM/flash with READ_RAM (stock firmware awake). Survives adapter re-enumeration. |
| `echobtn_resume_flash.py` | **The flasher.** Whole 4 KB sectors of a full image: quick check, erase stub, write, full read-back verify; resumes across press windows (`IMAGE.state`, `--fresh` to reset); checks the unit's serial first; refuses sector 0; explains `0x0C`. |
| `echobtn_resume_read.py` | Resumable chunked reads in press windows (RAM, or flash with `--minidriver`). |
| `echobtn_trace.py` | Listen-only reader for `TRACE=1` AButton traces (`0x19 <op> <len> <text>`), HCI events and stock's plain-text prints. |
| `echobtn_window_probe.py` | Handshake and timing probe for the press window (watchdog, minidriver experiments). |
| `echobtn_hci_flash.py` | Older flasher (needs the chip to stay up); home of `ERASE_STUB` and the Intel-HEX parser. |
| `ds_tool.py` | Parse and repack BRCMcfg sections. |
| `fw_mod.py` | Binary edits to the stock DS, e.g. `--press-color ffffff`. |
| `install_wiced_b0_sdk.sh` | Install the WICED 6.0 B0 platform. |
