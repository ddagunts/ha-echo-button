# Amazon Echo Button for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories)
[![Validate](https://github.com/ddagunts/ha-echo-button/actions/workflows/validate.yml/badge.svg)](https://github.com/ddagunts/ha-echo-button/actions/workflows/validate.yml)

Give a second life to the **Amazon Echo Button**, the round, light-up Alexa game
buzzer from 2017 that Amazon discontinued. This custom integration turns it into a
**toggle switch** with **colored press feedback**, a **controllable RGB LED**, and
**press / long-press events** for automations.

It talks to the button directly over Classic Bluetooth through your Home Assistant. No Echo, Alexa app or Amazon account is involved and nothing goes
through the cloud.

https://github.com/user-attachments/assets/a3f55fe2-0d2b-4031-b9c9-473aa6e054a4

## Open firmware

Custom firmware is not required to use the button with Home Assistant.

[`firmware/`](firmware/README.md) has **AButton**, replacement firmware built from source for
the button's chip: it stays on (with an optional light sleep), reports presses (single, double, triple, long) and battery
as BTHome over BLE, so ESPHome Bluetooth proxies relay them to Home Assistant, and updates
over the air with signed images. This integration discovers AButtons too and gives
them the same LED light entity and `play_animation` action, an Identify button, and the
button's settings. Its presses can be encrypted, and a [web page](web/) sets a button
up from Chrome or Edge over Web Bluetooth. It needs a one-time wired flash:
four wires soldered to the test pads in the battery compartment and a 3.3 V USB-serial
adapter. **[`firmware/GUIDE.md`](firmware/GUIDE.md)** is the step-by-step procedure, and
[`firmware/prebuilt/`](firmware/prebuilt/) has ready-made images. A button running them takes
OTA updates signed with the maintainer's key only; build from source to use your own key. It runs on two test units; see [`research/STATUS.md`](research/STATUS.md) for open
items.

## Features

For each button you get:

| Entity | What it does |
|---|---|
| `switch.<button>_toggle` | Flips on/off with each **short press**. The button then glows the new state's color (green = on, red = off by default) and fades back. You can also switch it from Home Assistant. Its state survives restarts. |
| `event.<button>_button` | Fires `press` (released within 1 s) or `long_press` (held 1 s or longer). |
| `light.<button>_led` | The button's LED as a color light with brightness. It stays on until turned off, and feedback and animations return to it afterwards. |
| `binary_sensor.<button>_connectivity` | Diagnostic: whether the button is connected. |
| `sensor.<button>_battery` | Diagnostic: the battery level the button reports, in %. It updates when the button connects, every 10 minutes while connected, and with each Keep-awake request, but only from readings taken while the LED is dark (see [Known limitations](#known-limitations)). The last value is kept while the button sleeps. |

There is also an **`echo_button.play_animation`** action for custom light animations
(colors, durations, fades, repeats).

`<button>` comes from the button's Bluetooth name, for example `EchoBtn2V8` gives
`switch.echobtn2v8_toggle`.

## Requirements

- **A Classic Bluetooth adapter on the Home Assistant host** (Bluetooth 2.1 or newer
  with BR/EDR). A Raspberry Pi 4/5's built-in Bluetooth, an Intel laptop/NUC adapter,
  or a modern USB dongle all work. **ESPHome and Shelly Bluetooth proxies won't work**:
  they only do Bluetooth LE, and the Echo Button needs Classic.
- Home Assistant **2026.11** or newer (developed and tested on 2026.11.1).
- An Echo Button: model **H5V83Y** (sold in 2-packs, also listed as XC56PY).

## Installation

### HACS (recommended)

1. In HACS, open the ⋮ menu → **Custom repositories**.
2. Add `https://github.com/ddagunts/ha-echo-button` with type **Integration**.
3. Find **Amazon Echo Button** in HACS, download it, and restart Home Assistant.

### Manual

Copy `custom_components/echo_button` into your Home Assistant `config/custom_components/`
folder (for example with the *Samba share* or *Terminal & SSH* add-on), then restart
Home Assistant.

## Setup

1. If the button is paired with an Echo, **unplug that Echo** for now; otherwise it
   will grab the button. You can also forget the button in the Alexa app.
2. **Settings → Devices & services → Add integration → Amazon Echo Button.**
3. **Hold the button down until it glows orange.** This takes 5–20 seconds; keep
   holding through any blue or green flashes. Release it.
4. Select **Submit**. Pairing takes about 15–25 seconds.

Repeat for each button. If a button was paired with this host before (for example
after you reset it), the integration removes the old pairing and pairs again.

### Options

On the integration card, select **Configure** to set:

- **Press flash color** (default blue, as the button ships): the button's own short
  flash the moment it's pressed. Pick any color, or black to turn it off. Home Assistant
  stores it on the button each time it connects.
- **Press feedback:** the on/off colors and the feedback duration (default 2000 ms;
  set 0 to turn feedback off). The feedback color appears right after the press flash.
- **Keep awake** (off by default): the button goes to sleep after 10 idle minutes. The
  next press only wakes it and isn't reported; the button reconnects about 2 seconds
  later. With this on, Home Assistant asks the button for its status when it connects
  and then every **Keep-awake interval** (default 240 s, up to 540 s), which keeps it
  awake and connected. This likely drains the batteries faster.

## Example automations

Toggle a lamp:

```yaml
automation:
  - alias: Echo Button toggles the lamp
    triggers:
      - trigger: state
        entity_id: switch.echobtn2v8_toggle
        to: ["on", "off"]
    actions:
      - action: "light.turn_{{ trigger.to_state.state }}"
        target:
          entity_id: light.living_room_lamp
```

Run a scene on a long press:

```yaml
  - alias: Echo Button long press
    triggers:
      - trigger: state
        entity_id: event.echobtn2v8_button
    conditions:
      - condition: state
        entity_id: event.echobtn2v8_button
        attribute: event_type
        state: long_press
    actions:
      - action: scene.turn_on
        target:
          entity_id: scene.good_night
```

Use the LED as a status light (amber while the garage door is open):

```yaml
  - alias: Garage open indicator
    triggers:
      - trigger: state
        entity_id: cover.garage_door
    actions:
      - if: "{{ trigger.to_state.state == 'open' }}"
        then:
          - action: light.turn_on
            target: { entity_id: light.echobtn2v8_led }
            data: { rgb_color: [255, 140, 0], brightness: 150 }
        else:
          - action: light.turn_off
            target: { entity_id: light.echobtn2v8_led }
```

Flash red three times when the doorbell rings:

```yaml
  - alias: Doorbell flash
    triggers:
      - trigger: state
        entity_id: binary_sensor.doorbell
        to: "on"
    actions:
      - action: echo_button.play_animation
        target: { entity_id: light.echobtn2v8_led }
        data:
          repeat: 3
          steps:
            - { color: "#ff0000", duration: 300 }
            - { color: "#000000", duration: 300 }
```

### `echo_button.play_animation`

Ready-made animations (Aurora, Rainbow, Breathe, Police, Doorbell) as Home Assistant scripts:
[`examples/animations.yaml`](examples/animations.yaml).

| Field | Description |
|---|---|
| `steps` | 1–41 steps. Each has a `color` (`[r, g, b]` or `"#rrggbb"`), a `duration` in milliseconds (1–65535), and an optional `fade: true` that fades into this step's color. |
| `repeat` | How many times to play the sequence (1–255). Default 1. |

When the animation ends, the LED goes back to the LED entity's color (or off). A
`fade` that starts right after a physical press can render wrong, because the button's
own press flash interferes (set the press flash color to black to avoid that). Press
feedback avoids it by building its fade from short fixed-color steps.

## Troubleshooting

- **"No Echo Button in pairing mode was found":** the button must be glowing
  **orange**, not blinking blue or green. If it never turns orange, take the batteries
  out for 30 seconds, make sure no Echo nearby is powered on, and hold again.
- **Pairing fails:** very old adapters (Bluetooth 2.0, before 2008) can't do the
  "Secure Simple Pairing" the button requires. Use a newer adapter.
- **Connectivity stays off:** only one host can hold the button. Make sure no Echo or
  other computer is connected to it, then press the button once; it reconnects on its own.
- **Debug logs:** go to **Settings → Devices & services → Amazon Echo Button → ⋮ →
  Enable debug logging**, reproduce the problem, then select **Disable debug logging**
  to download the log. When filing an issue, include it along with a diagnostics
  download (device page → ⋮ → Download diagnostics). To also capture startup, add this
  to `configuration.yaml` instead:
  ```yaml
  logger:
    logs:
      custom_components.echo_button: debug
  ```

## How it works

The Echo Button (Bluetooth name `EchoBtn…`, Cypress CYW20735) is a Classic Bluetooth
device. It offers a serial port (RFCOMM 2), HID, and an **Alexa Gadgets channel on
RFCOMM 4** (SDP class `0x1201`, plus Amazon's UUID `6088d2b3-983a-4eed-9f94-5ad1256816b7`).
Presses only come out of channel 4: the serial port and HID channels stay silent.

The integration connects to RFCOMM channel 4 directly, without asking the button's SDP
server first: after an abrupt disconnect (for example an unplugged adapter) the button
can refuse SDP for a long time while channel 4 keeps working. Some Python builds,
including Home Assistant's, lack `AF_BLUETOOTH` sockets, so the socket is created through
libc. BlueZ's D-Bus API is still used to find the adapter that holds the pairing and to
learn when the button's Bluetooth link comes up. Frames use the Alexa Gadgets Classic
framing:

```
F0 02 00 <seq> <payload, F0/F1/F2 escaped as F2 xx> <checksum hi lo> F1
checksum = (sum(payload) + 0x02) & 0xFFFF
payload  = 01 <type> 01 10 <16-byte serial> 01 <len> <data>
```

The button checks every payload strictly: the serial must be its own and the data
length must be exact. Anything else is **silently ignored**, and nothing is acknowledged.

| Type | Direction | Data |
|---|---|---|
| `0x01` | button → host | press/release: `data[3]` `02` = pressed, `03` = released; `data[8:11]` = the LED's R G B at that moment |
| `0x03` | button → host | status: `00 <battery> <reason>`. Battery is 0–100, read as a percentage and measured right then, so it sags while the LED is lit. Reason is `03` on connect and in reply to `0x07`, `00` when sent unprompted (every 10 minutes) |
| `0x04` | button → host | capability list on connect: a count, then `<command, 16-bit> ff <01 = takes data, 00 = none>` for each of `02 05 06 07 09 0a` |
| `0x06` | host → button | **play light animation now** (below) |
| `0x07` | host → button | status request, no data. Also keeps the button awake: it sleeps 10 minutes after the last one (a new connection doesn't reset that timer) |
| `0x02` `0x05` `0x09` `0x0a` | host → button | take data; meaning unknown |

Light animation (`0x06`):

```
01 01 <repeat 1-255> <step count>   then per step:
<duration ms, 16-bit big-endian> <R> <G> <B> <blend: 01 = fade into this color, 00 = switch>
```

A new animation replaces the running one, and the LED turns off when an animation
ends. The longest single command is 255 × 65.5 s (about 4.6 h), so the LED entity
resends its color every hour. When the link drops, the button reconnects to its paired
host on its own within a few seconds.

The protocol was worked out from scratch; [`research/`](research/) has the tools and
notes on what was tried.

## Known limitations

- **Home Assistant OS** hasn't been tested yet; development was on Home Assistant Core
  on Debian. Both use the host's BlueZ over D-Bus, so it's expected to work. Reports welcome.
- **Battery life** is unknown, especially with **Keep awake** on, which keeps the
  button's Bluetooth connection open all the time.
- **The battery level** is the button's own reading. It looks like a percentage
  (Duracells read about 90, older off-brand cells about 75), but the level at which
  the button stops working isn't known yet. The reading drops while the LED is lit
  (75 falls to about 52 at full red and to 0 at full white, while the button keeps
  working), so only readings taken after the LED has been dark for 10 seconds are used.
  With the LED entity on all the time, the level doesn't update.
- **The press flash color** is stored on the button each time it connects. It survives
  reconnects; whether it survives the button's sleep isn't tested yet. If it doesn't, the
  press that wakes a sleeping button flashes the default blue.
- **Multiple buttons** share one Bluetooth link and should work, but only one button
  has been tested.
- Without **Keep awake**, the press that wakes a sleeping button (10 idle minutes) is lost.

## Development

```sh
pip install pytest ruff dbus-fast
pytest            # protocol and client tests, no Bluetooth needed
ruff check .
```

`protocol.py`, `bluez.py`, `rfcomm.py`, `link.py` and `client.py` don't import Home Assistant.
`tools/echobtn_cli.py` (pair, list, print presses) and `tools/demo.py` (LED showcase)
run the same code without Home Assistant.

## Disclaimer

**No warranty.** Opening, wiring and reflashing a button is entirely at your own risk. The maintainer is not responsible for bricked, broken or unflashable buttons, or any other damage, however it happens. The software is provided "as is" (see the license).

Amazon, Alexa and Echo are trademarks of Amazon.com, Inc. or its affiliates. This
project is not affiliated with, endorsed by, or supported by Amazon.

## License

[MIT](LICENSE)
