# Echo Button application reverse‑engineering (B0 DS)

Map of the Echo Button's **application** (the "gadget" app in the serial‑flash DS), built by
reverse engineering `dump/sflash.bin`. Method and addresses only — no firmware bytes committed.
Addresses are RAM load addresses (where the DS places the code at boot). See
[`firmware_access.md`](firmware_access.md) for how the DS and flash were accessed.

## How the app was recovered

The DS (`BRCMcfgD` at flash `0x4000`) is a record stream `<id:2 LE><len:LEB128><data>`; record
`0x0103` "Data" = `<addr:4><bytes>` loads code/data to RAM. The 826 `0x0103` records rebuild the
**application image into RAM `0x215900–0x283854`** (~96 KB). Disassemble with
`arm-none-eabi-objdump -b binary -marm -Mforce-thumb --adjust-vma=0x200000` on the reconstructed
image. The app kept its **function‑name strings** (`gadget_*`, `hci_*`, `*_cback`), each referenced
once by the function that logs it, which is what makes the map below possible.

## Infrastructure

- **`0x2725d8`** — WICED trace / printf (format string in `r2`); the `WICED_BT_TRACE` sink.
- **`0x21b25c`** — "is debug/trace enabled" predicate; the app guards traces with
  `bl 0x21b25c; cbz r0, skip`. (Patching it to return 1 would turn on the built‑in tracing.)
- The app calls the Bluetooth stack and HAL in **ROM** (e.g. `0x6310c` `wiced_bt_stack_init`,
  `0x8a8a6` `wiced_hal_gpio_get_pin_interrupt_status`) and the DS patch area (`0x27xxxx`); its
  own code is in RAM (`0x215900+`). ROM names come from the WICED 6.0 20735‑B0 `patch.elf`
  (exact for the ROM; the patch area is a different revision, see [`STATUS.md`](STATUS.md)).

## Application symbol map (name → function start)

| Addr | Function |
|---|---|
| `0x21845c` | `application_start` (logs "gadget application start"; registers mgmt cback `0x218394` via `wiced_bt_stack_init`) |
| `0x2184f8` | button GPIO interrupt (logs "FACTORY_TEST: button triggered", clears the pin IRQ, sends `0x21d3bc`, then → `0x2184a0`) |
| `0x2184a0` | button‑event dispatcher: looks the pin up in the table at `0x2202b0` (`{pin, callback}` ×2, filled by `0x218570`), calls the callback with event 1 = down / 2 = up, arms a 5000 ms long‑press timer |
| `0x21c5fc` | the button callback (registered by app init `0x21c710`); plays the press/release light slots |
| `0x218cdc` | `gadget_ble_master_conn_cback` |
| `0x218e10` | `init_ble_connection` / `add_bda_to_scan_list` |
| `0x218f00` | `gatt_conn_cback` |
| `0x219970` | `hci_set_hid_state` |
| `0x219af4` | `hci_connect_hid_device` |
| `0x219c10` | `gadget_set_bt_pairing_state` |
| `0x219c88` | `gadget_delete_pairing_keys` |
| `0x21ad28` | `hci_hid_handle_command` |
| `0x21c344` | `directive_data_cback` (Alexa‑gadget directive handler — the host/Echo protocol) |
| `0x21c5a0` | `spp_connection_callback` |
| `0x21d9e4` | `gadget_decorate_header` (builds the Alexa‑gadget frame header) |
| `0x21b3ac` | board init (from the BTM_ENABLED handler `0x218394`): `0x21b330` reads the board revision (flash `0xC20`), then rev 1 → `0x21b7ec`, rev 2 → `0x21b650` |
| `0x21b7ec` | rev‑1 board init: builds the hardware tables (battery `{ADC 0xB = P0, …, enable P13}` at ctx+60, button P6 at ctx+68), P13 output low, button IRQ `0x2185ec(6)`, LEDs `0x21ab10`, then **`0x21bf10(1)`** |
| `0x21bf10` | **power latch** `(on)`: rev 1 only (ctx+32 == 1). on: `configure_pin(P5, OUTPUT, 1)`, P13 high; off: P13 low, `configure_pin(P5, OUTPUT, 0)` |
| `0x21becc` | arms the idle power‑off: 600 000 ms timer → `0x21bf68` = `0x21bf10(0)` (the stock "sleep") |
| `0x21c9a6` | battery read: P13 high, ADC `0x21aaf0(P0)`, P13 low |
| `0x27293e` / `0x272a2e` | `wiced_hal_gpio_configure_pin` / set‑output (patch‑area thunks) |

Device‑state struct at **`0x2202b0`**; config strings `EchoBtn` (`0x21f4b7`), `Echo Buttons`
(`0x21fa38`). There are ~216 functions in `0x215000–0x21e000`; the rest are un‑named leaves.

## LED and press feedback

**LED driver.** The RGB LED is three PWM channels driven by the app itself:
`0x21a06a` sets one channel (`wiced_hal_pwm_start` / `wiced_hal_pwm_change_values`), `0x21a0a4`
enables the PWM clock (`wiced_hal_aclk_enable`, 256 kHz), and **`0x21a288` sets an RGB color**
(each channel scaled by a float brightness, then through `0x21a02c` to a PWM value).

**Animation engine.** An animation is `{?, repeat, n, steps*, trigger, delay u16}` with 6‑byte
steps `{ms u16, R, G, B, blend}`. `0x21a168` starts one (current animation pointer at `0x220cdc`),
`0x21a5d0` is the step timer (fades are interpolated in doubles), `0x21a590` turns the LED off.

**Slots and triggers.** The light command handler **`0x21d6d8`** (command `0x06`) parses the wire
format

    01 01 <repeat> <n> + n × (<ms u16 BE> <R> <G> <B> <blend>) [+ <trigger> <delay u16 BE>]

and `0x21d5fc` files the result by **trigger**: `0` = play now (the default when the 3‑byte
trailer is absent), `1` = button‑up slot `0x220b08`, **`2` = button‑down slot `0x220ce6`**,
`3`/`4` = state slots `0x220c51`/`0x220cb8`. `0x21a928` plays slot 1–4. The command type `0x06`
reaches `0x21d6d8` from `directive_data_cback` with the message's data field and its exact
length byte, so a trailer is delivered intact.

**Step buffers have no bounds check** (`0x21d5fc` copies `n × 6` bytes). Sized from the
neighboring RAM references:

| Trigger | Slot | Steps buffer | Room |
|---|---|---|---|
| 0 | `0x220cc2` | `0x220d40` | 41 steps |
| 1 | `0x220b08` | `0x220e3c` | ~41 steps |
| 2 | `0x220ce6` | `0x220b4a`–`0x220c40` | 41 steps |
| 3 | `0x220c51` | `0x220b38`–`0x220b4a` | **3 steps** |
| 4 | `0x220cb8` | `0x220b20`–`0x220b30` | **2 steps** |

A host must never send trigger 3/4 with more steps than that: it would overwrite app RAM.
With trigger 2 and nothing playing, the animation is only stored; the next press plays it.

**Press path.** GPIO IRQ `0x2184f8` → dispatcher `0x2184a0` → button callback **`0x21c5fc`**: on
event 1 (down) it plays slot 2, on event 2 (up) slot 1, unless the connection state
(`0x219fb0`) is 1.

**The built‑in defaults** are light‑command payloads in initialized data, loaded at init
(`0x21c710`) and again on each connection‑state change (`0x21c6d8`):

| Data | Animation | Trigger | Role |
|---|---|---|---|
| `0x220092` | amber 500 ms / off 500 ms, ×3 | 0 | state indication (played now) |
| **`0x2200a5`** | **blue `(0,0,255)` 100 ms, off 100 ms** | **2** | **the press flash** (state 2) |
| **`0x2200b8`** | **same** | **2** | **the press flash** (state 0/1) |
| `0x2200cb` | orange breathing, ×120 | 3 | pairing glow |
| `0x2200e4` | blue 10 s, off 500 ms | 4 | state slot 4 |

So the "un‑removable" blue press flash is data: **`research/fw_mod.py --press-color`**
recolors step 0 of `0x2200a5` and `0x2200b8` (bytes `+6..+8`). The host can also replace it
at runtime by sending a trigger‑2 animation: **hardware‑verified** (green and white press
flashes on stock firmware; it survived a gadget‑channel reconnect). See [`STATUS.md`](STATUS.md).

## Where custom firmware would hook in

- **OTA**: `application_start` (`0x21845c`) registers the app's GATT/management callback. Adding the
  WICED OTA FW Upgrade service (`fw_upgrade_lib/ota_fw_upgrade.c`; service UUID
  `c7261110-…`) means extending the GATT DB and the management callback. On the stock app that
  is grafting code into free DS space and re‑pointing a callback (the DS has no CRC records to
  fix); the alternative is an SDK build that replaces the whole DS.
- **BLE peripheral** (so HA BLE proxies can relay presses): the BLE central plumbing already exists
  (`init_ble_connection`, `gatt_conn_cback`); a peripheral advert + a press GATT characteristic is
  the addition.
- **Recolor or disable the press flash**: done as a data edit (`fw_mod.py --press-color`;
  `000000` would make it dark). See [LED and press feedback](#led-and-press-feedback).

Any of these is a binary‑patch‑the‑DS exercise, now unblocked by the working erase+write+verify
reflash path (`echobtn_hci_flash.py`).

## Gadget protocol: the command dispatch (`directive_data_cback` `0x21c344`)

The handler validates `msg[0] ≤ 1`, matches the device serial via a TLV loop, then switches on
the **command type `msg[1]`** to per‑command handlers:

| type | handler | meaning |
|---|---|---|
| 1 | `0x21d532` | — |
| 2 | `0x21d3cc` | configure a capability: parses a u16 header + `count` × (byte, u16) entries → `0x21cb28` (register via ROM `0x144d70`) |
| 3 | `0x21d4b6` | — |
| 4 | `0x21d46e` | set a 4‑byte parameter → `0x21caac` |
| 5 | `0x21d4a8` | no‑arg **gadget re‑init**: re‑registers ~11 capability objects (`0x221xxx`) via `0x144d70`, resets state → `0x21cdec` |
| **6** | `0x21d6d8` | **play light animation** (the integration's `0x06`) |
| **7** | `0x21d812` | **status request** (the integration's `0x07`) |
| 0xfd / 0xfe | `0x21d834` / `0x21d874` | (factory/diagnostic?) |

`6` and `7` match the Home Assistant integration exactly, confirming the mapping. The commands the
integration's research left undecoded (`0x02`, `0x05`) are **capability registration/re‑init**, not
LED control. The press flash is reachable through `6` itself, via its optional trigger trailer
(see [LED and press feedback](#led-and-press-feedback)). The integration only needs `6` (light)
and `7` (status); the rest are the Alexa‑gadget provisioning surface.

## Repacking the DS (integrity: solved)

A modified DS can be produced and will be accepted, because:

- The DS has **no CRC records** (`0x010E "CRC Start"` / `0x010F "CRC End"` are absent), and the
  header's 4 bytes after the signature are zero (not a checksum). The firmware also has **no
  secure‑boot / signature** strings.
- **Parse → re‑emit round‑trips the real DS byte‑for‑byte** (1053 records, 107,697 bytes), and the
  SS too — so the `<id:2><len:LEB128><data>` model is exactly right. The header's `+12` field
  (`0x1a4a4`) is *not* the section length (the stream runs to `0x1e4b1`, then a `fe 00 00`
  terminator and `0xFF` padding); it's preserved verbatim on repack.
- The **SS** (`BRCMcfgS` @0x0) is the bootstrap: a `0x0100 "Config layout"` record points the ROM
  at the DS (failsafe base `0x4000`) and defines the VS copies (`0x2000`/`0x3000`, len `0x1000`),
  plus the crystal freq and **BD_ADDR** (`0x0300`). Leave the SS untouched when only changing the app.

`research/ds_tool.py` implements this: `Section.parse()/to_bytes()`, `data_loads()` (the 826
`0x0103` app chunks), `patch_ram(addr, bytes)` (overwrite app code in place), and `add_data(addr,
bytes)` (inject a new RAM load). So the custom‑firmware loop is: parse the DS → `patch_ram`/`add_data`
→ `to_bytes()` → erase the DS sectors (`0x4000+`) and `WRITE_RAM` the new image → read‑back verify.

**HARDWARE‑VALIDATED (2026‑11‑17):** an inert one‑byte edit to a debug string in DS sector
`0x1d000` was erased + rewritten (full 4 KB verified), and the button **boots and runs normally**
from the modified DS. So the full custom‑firmware loop works: `ds_tool.patch_ram/add_data` →
`echobtn_hci_flash` erase+write+verify → the ROM boots it. There is **no DS integrity check** to
defeat. (Side effects of a reflash: the erase stub's `WRSR(0)` leaves the flash's block‑protect
cleared — harmless; restore with a WRSR if you want stock protection back.)
