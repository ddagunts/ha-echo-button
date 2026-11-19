# Research tools

The scripts used to work out the Echo Button protocol. They are here so others can
continue, for example to decode commands `0x02`, `0x05`, `0x09` and `0x0a`. The
built-in blue press flash turned out to be a stored `0x06` animation (see
[`app_map.md`](app_map.md#led-and-press-feedback)). See the main README for what is
known.

Run them on Linux with BlueZ and a Bluetooth 2.1+ adapter that supports Classic.
Disconnect Home Assistant from the button first: only one program can hold its
gadget channel.

| Script | What it does |
|---|---|
| `echobtn_probe.py` | Discovers and pairs a button in pairing mode (orange), logs SDP UUIDs, opens the serial port, and hex-dumps traffic. Logs every pairing step. |
| `echobtn_gadget.py` | Opens the gadget channel (RFCOMM 4), decodes every frame, and can send an Alexa Gadgets `Discover` directive (which the button ignores). |
| `echobtn_lab.py` | Holds the gadget channel open and sends commands written to a FIFO, one per line (`msg <type> <data-hex>`), logging all replies. |
| `light_sweep.py` | Feeds batches of candidate light commands to `echobtn_lab.py`. This is how command `0x06` was found. |
| `press_anim_test.py` | Sends one `0x06` animation with a trigger trailer (button‑down/up) and logs presses, to test setting the press flash over the protocol. |

### Firmware access (serial pads)

A separate line of work reads and writes the button's firmware directly through the
CYW20735B0's test pads, over its HCI UART. The hands-on procedure is
[`../firmware/GUIDE.md`](../firmware/GUIDE.md); the chip, board, pin and flash facts are in
[`firmware_access.md`](firmware_access.md); the state of things in [`STATUS.md`](STATUS.md).
These tools only need `pyserial`:

| Script | What it does |
|---|---|
| `echobtn_resume_flash.py` | **The flasher**: erases, writes and verifies whole 4 KB sectors of a full image, resuming across the ~2 s press windows of a unit that doesn't boot; checks the unit's serial first; never writes sector 0. |
| `echobtn_hci_dump.py` | Dumps ROM/RAM/serial flash via `READ_RAM` (stock firmware awake). Survives adapter re‑enumeration. |
| `echobtn_trace.py` | Reads AButton debug traces and the stock firmware's text output from the UART. |
| `echobtn_resume_read.py` | Resumable chunked reads in press windows. |
| `echobtn_window_probe.py` | Handshake and timing probe for the press window. |
| `echobtn_hci_flash.py` | The older flasher; holds the sector‑erase stub and the Intel‑HEX parser. |
| `ds_tool.py` | Parses and repacks the serial‑flash config sections (the DS holds the application). |
| `fw_mod.py` | Builds modified stock firmware, e.g. `--press-color ffffff` for a white press flash. |
| `install_wiced_b0_sdk.sh` | Decrypts and installs the WICED Studio 6.0 **20735‑B0** platform (the B0 SDK and its ROM symbols) next to a base install. |

Keep your own dumps **private** and out of git (`.gitignore` excludes `dump/`): sector 0
holds the button's BD_ADDR, calibration and serials, and sector `0x2000` its pairing link
keys. The stock firmware isn't published here.

Recording traffic with `btmon -w capture.btsnoop` (needs `CAP_NET_RAW`) and opening
it in Wireshark is the best way to see everything. **Don't publish raw captures.**
They contain the pairing link key and the addresses and names of nearby devices.

## What was tried

- Simple serial (RFCOMM 2, SPP) and HID: the button connects both, but sends nothing on them.
- Commands `0x00`–`0x0f` with empty data: only `0x07` (status request) gets a reply.
- Commands `02 05 06 09 0a` with all-zero data of length 0–64: no replies, no visible effect.
- Envelope variations on `0x07`: wrong serial, extra fields, version `02`, and empty
  lists are all silently ignored.
- Light: about 330 color-coded layout guesses found nothing. A header of `01` bytes
  followed by `FF` bytes, sent with command `0x06`, lit up; from there each field was
  isolated with single-variable tests watched by eye.
- Command `06` with header byte 0 = `02`/`03` still plays immediately, so there is no
  trigger byte. Commands `02`/`05` carrying the same data change nothing on press or
  release, so presses are probably answered by the host, as the Echo would have done.
- Alexa's `SetLight` directive gives `buttonDown`/`buttonUp` animations an extra
  `triggerEventTimeMs` field, so `02`/`05` were also sent a green/magenta 5 s `06` body
  with a zero delay added as a u16 in front, a u16 after the first two header bytes, a
  u32 in front, a u16 at the end and a u8 in front, and with the body's first one or two
  bytes removed. One press each: every press still gave only the blue flash.
  (The firmware later showed why: the trigger is a **3‑byte** trailer after the steps,
  `<trigger> <delay u16 BE>`, and fewer than 3 trailing bytes are ignored. Trigger `2` is
  button‑down: sending one turned the press flash green, then white, on a stock button.
  See [`STATUS.md`](STATUS.md) and [`app_map.md`](app_map.md#led-and-press-feedback).)
- The LED color in press/release messages (`data[8:11]`) shows host animations. The
  built-in blue flash showed up there only once in about 30 presses, on a bounce right
  at release (`00 00 ff`), so that field can't tell whether a stored animation ran.

## Status and battery

Status (`0x03`) data is `00 <battery> <reason>`. Byte 1 is a live battery reading.
On a button with older off-brand AAA cells, status requests sent while the LED
played a 20 s color gave:

| LED | byte 1 |
|---|---|
| off | 75–76 |
| full red | 52 |
| full blue | 55–56 |
| full white | 12, 47, 15, 18, 48 (jumps around); 0, 0, 50 in a second run |
| off again, after 4 s | 73–74, then 75 within a minute |

The button kept working and stayed connected while it reported 0.

The same model with Duracells read 88–92 at rest. Byte 2 is `03` on connect and in
reply to `0x07`, `00` for the report the button sends on its own every 10 minutes.
Opening the gadget channel and status requests don't reset that timer: the first report
came about 10 minutes after pairing, although the channel had been opened three times
and dozens of `0x07` sent in between.

## Other services

`sdptool records --raw` while the button is awake (it refuses SDP for a while after
an abrupt disconnect):

- **HID** (PSM 17/19): the descriptor is a stock WICED template, a boot keyboard
  (report 1, with the usual 5-bit Num/Caps/Scroll/Compose/Kana LED output report) plus
  Consumer Control (report 3, two 16-bit usages). It has no battery usage and no
  `HIDBatteryPower` attribute, so Linux shows no battery for it. BlueZ attaches it
  through `uhid` as `hidraw`. Supervision timeout is 2 s. Not tried yet: writing the
  keyboard LED output report (`/dev/hidraw*` is root-only).
- **PnP Information:** spec 1.03, vendor `0x1949` (Lab126's USB vendor ID, though the
  source field says Bluetooth SIG), product `0x0415`, version `0x0001`.
- **"gadget"** (Amazon UUID `6088d2b3-…`): a marker record only, no channel or other
  attributes. The gadget channel itself is the `0x1201` "RFC SERVER" record (RFCOMM 4).
- **"SPP SERVER"** (RFCOMM 2): silent, as above.

## Pairing

The button answers discovery a while before it can pair. Pairing then fails about
1.2 s in with `AuthenticationCanceled` or `AuthenticationFailed`; holding it again
until solid orange and retrying works.

## Hardware

Cypress CYW20735: a Cortex-M4 that runs the Bluetooth stack and the application,
with Classic and LE Bluetooth, PWM, a 10-bit ADC (the likely battery source), GPIO,
I2C, SPI and UART. The two AAA cells are in parallel (1.5 V), boosted to 3.3 V; a press
powers the board for about 2 s and the firmware keeps it on with **P5**. Rev-1 pins (from the
stock firmware, see [`firmware_access.md`](firmware_access.md#board-pins-and-power-rev-1-verified)):
LED on P27/P28/P29 (R/G/B), button on **P6** (TechInfoDepot's "P11" is wrong), battery on ADC
P0 behind a divider switched by P13. Test pads TMF1–22 are reachable from the battery
compartment: TMF1 3.3 V, TMF2 GND, TMF3 battery, TMF7 button, TMF8 SPI MOSI, TMF9–12 UART
TX/CTS/RX/RTS. SWD is on J2, a Tag-Connect footprint. Teardowns:
[mpetroff.net](https://mpetroff.net/2017/12/amazon-echo-button-teardown/),
[Medium](https://medium.com/@decrocksam/amazon-echo-buttons-teardown-9f066a84f156).
FCC ID UUU-4612.
