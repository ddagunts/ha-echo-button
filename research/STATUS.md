# Echo Button firmware work: status

Where the firmware work stands (November 2026). The how-to is
[`../firmware/GUIDE.md`](../firmware/GUIDE.md); chip and board facts are in
[`firmware_access.md`](firmware_access.md); the stock application's internals are in
[`app_map.md`](app_map.md).

## What works

| | State |
|---|---|
| Dump, modify and reflash the stock firmware over the test pads | verified |
| Recover a unit stuck in the ROM download handler (press windows) | verified, both test units |
| **AButton** (open firmware, `firmware/`) boots and holds its own power | verified |
| Button presses → BTHome `press`, `double_press`, `triple_press`, `long_press`, `long_double_press` | verified (debug and release builds) |
| Battery % over BTHome | reads 100 % on fresh cells; the low end is not yet checked on discharged cells |
| Signed OTA updates over BLE | verified, four times in a row on one unit |
| White (or any color) press flash on the **stock** firmware, by protocol or by a DS edit | verified |
| AButton GATT service: LED color and animations, settings, name, commands | verified on both units (release and debug): read-back, and the LED output by eye |
| Status on the LED: battery color at power-up, low-battery warning, press color, identify | verified by eye |
| Settings, name and steady LED color persist across reboots (NVRAM) | verified |
| Advert interval setting takes effect | verified (3 vs 24 adverts in 10 s at 2000 vs 250 ms) |
| BTHome adverts continue while a client is connected (non-connectable) | verified (21 adverts in 10 s while connected) |
| Home Assistant: discovery, LED light, play_animation, identify, options | verified on a live Home Assistant (integration 0.2.1), plus `tests_ha/` |
| A stock button switched to AButton: its old Classic pairing in the HA host's BlueZ | removed automatically by integration 0.2.1 (caused `br-connection-create-socket`) |
| BTHome encryption: AES-128-CCM adverts, key set over GATT after a press | verified on a rev-1 unit (2026-11-19): key taken after a press, refused 60 s later; adverts decrypt with the scanned address; counter 256 after a reboot; key kept. Encoder host-tested against bthome-ble. Home Assistant's BTHome integration took the key and decodes the presses |
| Web Bluetooth setup page (`web/`, GitHub Pages) | verified: encryption turned on for a second unit from the published page |
| Transmit power setting (Power characteristic) | verified (2026-11-19): received at 1 ft −34 dBm at the default, −45.5 at 0 dBm, −66 at −16; kept across a restart and applied at boot |
| Light sleep option | verified on a rev-1 unit (2026-11-19): adverts at the same rate as a unit without it (25-minute side-by-side soak, no restart), connections work, presses reach Home Assistant. The power it saves is **not measured** |
| Home Assistant options for transmit power and sleep (integration 0.3.0) | `tests_ha/` only; not yet on a live Home Assistant |

Current build: **0.4.0** (`firmware/prebuilt/`): the release build runs on both test units
(installed over the air).

## Open

- **Battery saving isn't measured.** The current drawn with light sleep on and off (a meter
  in series with the cells) would show what it saves.
- **Integration 0.3.0** (transmit power and sleep options) hasn't run on a live Home Assistant.
- **Battery %** is only measured while the LED is dark (the LED's load sags it), so a
  constantly lit LED freezes the value. Fresh cells read 77–100 %; discharge not yet checked.
- **UART flash mode:** hold the button 8 s at power-up (magenta LED); the firmware
  stops advertising, starts the HCI UART and clears `btu_hcievt_handler`, so the ROM's HCI
  filter lets the flasher's vendor commands through. This also covers going back to stock
  from AButton. On 47U: the 8 s hold and the magenta LED work; flashing over the UART in
  this mode is **not yet tested** (next: the flasher with a wrong `--expect-serial` as a
  read-only check, then a real flash). Included in the prebuilt images.
- **The prebuilt images only accept OTA images signed with the maintainer's key** (documented
  prominently). Flash mode (untested over the UART) is meant to let owners switch to their own builds.
- BLE connection attempts from BlueZ often time out once; a retry usually works.
- The P6 edge interrupt never fires (AButton polls the pin instead); why is unknown.
- Rev-2 boards: different pins (button P16, battery ADC P1, stock table at `0x21b650`) and an
  unknown power latch.
- Stock firmware: whether a protocol-set press flash survives its 10-minute power-off.

## Stock firmware findings (protocol side)

- The built-in blue press flash is a stored light animation (data, not code): 100 ms
  `(0,0,255)`, 100 ms off, trigger "button-down", kept twice at `0x2200a5`/`0x2200b8`.
- Command `0x06` takes an optional 3-byte trailer `<trigger> <delay u16 BE>`; trigger `2`
  stores the animation as the button-down flash, `1` as button-up. Verified: green and white
  press flashes on a stock button; the Home Assistant integration's **Press flash color**
  option uses this. Never use trigger 3/4 with more than 3/2 steps (unchecked copy into small
  buffers).
- `fw_mod.py --press-color RRGGBB` makes the same change permanent in the DS (4 bytes in
  sector `0x1e000`); hardware-verified with white.

## Key resources

| Thing | Where |
|---|---|
| AButton builds | `firmware/prebuilt/` |
| Stock firmware (DS bank of a rev-1 unit, no unit data) | not published; from the maintainer on request |
| B0 SDK + ROM symbols | WICED Studio 6.0, installed with `research/install_wiced_b0_sdk.sh` |
| GCC ARM toolchain | inside the WICED install (`43xxx_Wi-Fi/tools/ARM_GNU/bin/Linux64`) |
| Your own dump | `dump/` (gitignored: holds your BD_ADDR, calibration and link keys) |
