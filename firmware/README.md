# AButton: open firmware for the Amazon Echo Button

Replacement firmware for the 2017 Amazon Echo Button (Cypress CYW20735B0), built from
source with Cypress's WICED SDK. It turns the button into a plain BLE button that Home
Assistant picks up through **ESPHome Bluetooth proxies** (or the host's own adapter),
with no Echo, Alexa or pairing.

> **Status (November 2026):** runs on two rev-1 buttons. It boots, holds its own power,
> reports single / double / triple / long presses and battery over BTHome, takes LED colors,
> animations and settings over BLE, and updates over the air (signed OTA). Ready-made images are in [`prebuilt/`](prebuilt/); the step-by-step
> wiring, flashing and setup procedure is **[`GUIDE.md`](GUIDE.md)**. Open items (the UART flash mode,
> going back to stock from AButton) are in
> [`../research/STATUS.md`](../research/STATUS.md#open). Keep your button's original flash dump.

> **Prebuilt images only take updates signed with the maintainer's key.** The images in
> [`prebuilt/`](prebuilt/) have the repository maintainer's public key built in, so over the
> air a button running them accepts **only** updates the maintainer signs (the
> `.ota.signed` files published here). Firmware 0.2.1 and later (the prebuilt images) add a UART
> **flash mode** (hold the button 8 s at power-up) meant to let owners with the test-pad
> wiring flash their own builds onto a running AButton; it is experimental: the hold and the magenta LED work, flashing over the UART in this mode is not yet tested.

> **No warranty.** Opening, wiring and reflashing a button is entirely at your own risk. The maintainer is not responsible for bricked, broken or unflashable buttons, or any other damage, however it happens.

## What it does

- **Never sleeps.** The stock firmware drops off after 10 idle minutes and loses the press
  that wakes it. AButton keeps advertising and reports every press. This costs battery.
- **BTHome v2** advertisements (service data UUID `0xFCD2`), which Home Assistant's BTHome
  integration decodes: `press`, `double_press`, `triple_press`, `long_press`,
  `long_double_press`, `long_triple_press`, and battery %. Fast advertising for 2.5 s after
  each event, then at the configured interval (250 ms by default). It keeps advertising
  (non-connectable) while a client is connected, so no press is missed.
- **Encrypted BTHome** (0.3.0, optional): with a key set, the adverts are encrypted and
  authenticated (AES-128-CCM, the BTHome bind key), so nobody nearby can read the presses or
  fake them. Home Assistant's BTHome integration asks for the key. Setting or changing the key
  only works within 60 s of a press on the button. The key is never readable over BLE.
- **LED control over BLE:** a steady color and animations (up to 40 steps with
  fades, repeated or looping), from Home Assistant (this repo's integration: a `light`
  entity and `echo_button.play_animation`) or `abutton_ble.py`. The steady color survives
  restarts.
- **Status on the LED:** the power-up blink shows the battery (green / amber / red), two red
  blinks after a press when the battery is low, dim blue during an OTA update (green /
  red at the end), and an identify blink on request.
- **Settings over BLE, kept on the button:** LED brightness, the color while held, long-press
  time, double/triple-click window, advert interval, low-battery level, and the status
  options. Also the **name** (up to 26 characters).
- **Signed OTA updates** over BLE (WICED secure OTA: ECDSA P-256 + SHA-256). The button only
  accepts images signed with the key its firmware was built with, so nobody nearby can
  reflash it: the maintainer's key for the prebuilt images, yours for your own build (see
  the note above).
- **UART flash mode** (0.2.1, experimental: the hold and the magenta LED work, flashing over the UART in this mode is not yet tested): hold the button for 8 s right after
  power-up; the LED turns magenta, Bluetooth stops until the next power cycle, and the
  chip should accept the UART flasher as under the stock firmware. Physical access only.
- **One image for every unit:** the MAC stays in the unit's flash sector 0 and the name and
  settings in its NVRAM, so OTA never changes a unit's identity.
- Device Information service: manufacturer, model, firmware version.
- **Web page** ([`../web/`](../web/)): light, settings, name, encryption key and commands from
  Chrome or Edge over Web Bluetooth, with nothing to install.

The GATT protocol (service `0e5a1000-a8b9-4c3b-9d1a-ab7ff0ab7700`) is documented at the top
of [`abutton/abutton.c`](abutton/abutton.c) and in
[`../custom_components/echo_button/abutton.py`](../custom_components/echo_button/abutton.py).

## Configure a button from the command line

```sh
B=AA:BB:CC:DD:EE:FF
python3 firmware/abutton_ble.py info     $B                    # firmware, name, settings, LED
python3 firmware/abutton_ble.py color    $B ff8800             # steady color ("off" turns it off)
python3 firmware/abutton_ble.py animate  $B ff0000:300 000000:300 --repeat 0   # blink until stopped
python3 firmware/abutton_ble.py animate  $B ff0000:500 0000ff:500:fade --repeat 3
# Aurora (examples/animations.yaml has it and more as Home Assistant scripts):
python3 firmware/abutton_ble.py animate  $B 00c8a0:1800:fade 28ff5a:1600:fade 007a8c:1400:fade \
    1aff9c:900:fade 8228ff:2000:fade 1e3cff:1500:fade c828c8:1200:fade 00283a:1600:fade 00c8a0:1400:fade --repeat 0
python3 firmware/abutton_ble.py stop     $B
python3 firmware/abutton_ble.py config   $B --brightness 128 --press-color 00ff00 --long-press-ms 800
python3 firmware/abutton_ble.py config   $B --advert-interval-ms 1000 --low-battery-pct 25 --boot-battery off
python3 firmware/abutton_ble.py name     $B "Garage Button"    # "" goes back to EchoBtnXXX
python3 firmware/abutton_ble.py identify $B                    # three white blinks
python3 firmware/abutton_ble.py reboot   $B
python3 firmware/abutton_ble.py defaults $B                    # settings, name and LED back to defaults
# BTHome encryption (0.3.0+). Press the button once first: key changes need a press in the last 60 s.
python3 firmware/abutton_ble.py key      $B --generate         # random key: stored, and printed for HA
python3 firmware/abutton_ble.py key      $B 231d39c1d7cc1ab1aee224cd096db932   # your own key
python3 firmware/abutton_ble.py key      $B off                # stop encrypting
python3 firmware/abutton_ble.py key      $B                    # on or off?
python3 firmware/abutton_ble.py monitor --key 231d39c1d7cc1ab1aee224cd096db932  # decrypt adverts
```

`config` with no options prints the current settings. Each command connects, retries if
BlueZ's first attempt times out, and disconnects. Home Assistant overwrites the settings
with its own options whenever it connects, so with the integration in use, change them
there.

## Hardware (board rev 1)

From the stock firmware's own hardware table (`research/app_map.md`):

| Function | Pin |
|---|---|
| Button | P6, active low, pull-up; polled every 10 ms (its edge interrupt never fires) |
| LED red / green / blue | P27 / P28 / P29 = PWM1 / PWM2 / PWM3 (ACLK1 256 kHz, 512-tick period) |
| Battery | ADC P0 with P13 high: 0.9 V = 0 %, 1.6 V = 100 % (stock's scale) |
| **Power latch** | **P5, output: high = stay on, low = power off** |
| Battery divider enable | P13, output: high only while reading ADC P0 (stock and AButton); low otherwise |

**Power.** A press powers the board for only **~2 s, even while held**. Firmware that wants
to keep running drives **P5 high** (stock: right after BTM_ENABLED); driving it low switches
the board off until the next press. The stock firmware's "sleep after 10 idle minutes" is
exactly that: a 600 000 ms timer that drives P5 low. AButton drives P5 high first thing in
`app_init` and never lets go. Firmware that doesn't latch P5 looks dead: it dies ~2 s after
every press.

The board revision is byte `0xC20` of the flash (stock factory data); rev 2 boards use
other pins and are not supported yet.

## Build

Needs Linux x86-64, Python 3 with `cryptography` (and `bleak` for the BLE tool).

1. **The SDK.** Get *WICED Studio 6.0.0.43* for Linux from Infineon's WICED community
   downloads, run its installer once (the base install works on current distros; only
   its old IDE doesn't), then add the 20735-B0 platform, which the installer ships
   encrypted and skips:

   ```sh
   WICED_PLATFORM_KEY=... research/install_wiced_b0_sdk.sh path/to/WICED-Studio-6.0.0.43-IDE-Installer.bin
   ```

   The platform archives are encrypted with a key that the installer itself carries (it
   passes it to its own decryption helper from the install script it runs); extract it from
   your copy of the installer. It isn't published here. The SDK is Cypress's and isn't in
   this repository.

2. **Build:**

   ```sh
   firmware/build.sh                  # release
   TRACE=1 firmware/build.sh          # with WICED traces on the HCI UART, 115200 baud
   ABTN_FW_VERSION=0.3.0 firmware/build.sh
   ```

   The first build creates your **OTA signing key** at `~/.config/abutton/ota_key.pem`
   (or `$ABTN_OTA_KEY`). Back it up and never commit it: firmware built with it only
   accepts updates signed with it. Outputs in `firmware/out/`:

   | File | What |
   |---|---|
   | `abutton.hex` | the SDK's flash image (static section + data section) |
   | `abutton.bin` | a full 512 KiB flash image (generic: no unit identity) |
   | `abutton.ota.signed` | the signed OTA image |

   `build.sh` also runs `check.sh`, a `-Wall -Wextra -Werror` pass the SDK's build lacks.

## Flash a button

Follow [`GUIDE.md`](GUIDE.md): solder four wires, dump the flash, then flash a prebuilt image
or your own build with `research/echobtn_resume_flash.py`. To flash your own build, turn it
into a full image first:

```sh
python3 firmware/abutton_image.py assemble firmware/out/abutton.hex -o firmware/out/unit.bin
python3 research/echobtn_resume_flash.py firmware/out/unit.bin \
    --sectors 0x1000,0x4000-0x1e000 --expect-serial <the unit's 16-char serial>
```

On a button running the stock firmware this takes one press: stock keeps the board powered
for 10 minutes, and the flash takes about 25 s. Once it runs AButton, update it over the air
(below). The 2-second press-window recovery in the guide is only for a button whose firmware
is bad (it doesn't start or doesn't hold its power).

Only the firmware sectors are written; sector 0 (the unit's MAC, calibration and factory
data) stays as it is, so the button keeps its MAC and names itself like stock (`EchoBtn` +
the serial's last 3 characters). `abutton_image.py patch --name/--mac` edits sector 0 of an
image, which the flasher deliberately never writes. Set a name over BLE instead
(`abutton_ble.py name`); custom MACs aren't supported on real units. `abutton_image.py show IMAGE` prints an image's MAC, name and serial.

`assemble` keeps the DS **end record** (`fe 00 00`, id `0x00FE`, length 0) and checks it
against the DS header length. Without it the ROM runs off the end of the DS and never starts
the application.

## Update over the air

```sh
python3 firmware/abutton_ble.py monitor                    # see buttons and their events
python3 firmware/abutton_ble.py info AA:BB:CC:DD:EE:FF     # firmware version
python3 firmware/abutton_ble.py ota  AA:BB:CC:DD:EE:FF firmware/out/abutton.ota.signed
```

Over the air is the normal way to update a running AButton. While its Bluetooth stack is
up, the ROM refuses the UART flasher's commands (status `0x0C`); flash mode (0.2.1, untested over
the UART, see the guide) is meant for wired flashes. The prebuilt images accept only
updates signed with the maintainer's key (published here); your own builds accept only yours. On BlueZ, if the button's MAC
was once bonded with the stock firmware (a classic HID gamepad), `bluetoothctl remove` it
first, or the connection fails with `br-connection-create-socket`. With a `TRACE=1` build,
keep `research/echobtn_trace.py` reading the UART, or a connection can crash the button.

The button writes the new image to its second bank (flash `0x42000`), checks the signature,
switches banks and reboots. A bad signature is rejected and the old firmware keeps running.
The LED is dim blue during an update, green when it verified, red if it failed.

## Home Assistant

Nothing to install: with an ESPHome Bluetooth proxy (or a local adapter) in range, Home
Assistant discovers the button through the **BTHome** integration. Presses arrive as
`event` entities with the event types above, and battery as a sensor.

With encryption on, give Home Assistant the key: when the BTHome integration first sees the
encrypted adverts it asks for it (a re-authentication notice on the BTHome entry, or the
prompt when you add the button). A new key needs the same step. Turning encryption off again
needs the button's BTHome device deleted and added back: once BTHome has a key, it ignores
unencrypted adverts so they can't be faked.

The encryption counter in each advert keeps rising across restarts (the button reserves
counters in NVRAM 256 at a time), as BTHome's replay check requires. Restore defaults keeps
the key.

## Signing notes

`ota_sign.py` does key generation and signing with `cryptography`. The SDK's own
`wiced_tools/ecdsa256` Linux tools are **not usable**: they're built with a 64-bit `DWORD`
and produce signatures their own verifier rejects, and they take keys and nonces from
unseeded `rand()`, which would make the private key recoverable. The signature format
(SHA-256 over the image, then `r‖s`, 32 bytes each, big-endian) was checked against the SDK
verifier's algorithm rebuilt with 32-bit words.

## Files

| File | |
|---|---|
| `abutton/abutton.c` | the application: button, LED, BTHome, OTA hooks, sleep policy, identity |
| `abutton/bthome.c` | BTHome service data and its encryption (AES-128-CCM), host-tested by `tests/test_bthome_crypto.py` |
| `abutton/wiced_bt_cfg.c` | Bluetooth stack configuration (BLE only, advertising intervals) |
| `abutton/makefile.mk` | SDK app makefile (secure OTA, `TRACE=1`) |
| `build.sh`, `check.sh` | build, and the strict compile check |
| `ota_sign.py` | key generation, the firmware's public-key source, signing, verification |
| `abutton_image.py` | assemble full flash images, set name/MAC, show identity |
| `abutton_ble.py` | BTHome monitor (decrypts with `--key`), info, LED, animations, settings, name, commands, encryption key, OTA client |
| `../web/` | the Web Bluetooth setup page (`index.html`, protocol in `abutton.js`) |
| `GUIDE.md` | wiring, flashing, setup, OTA and recovery, step by step |
| `prebuilt/` | ready-made AButton images (release and debug, flash and OTA) and the stock firmware |
