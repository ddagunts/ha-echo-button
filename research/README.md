# Research tools

The scripts used to work out the Echo Button protocol. They are here so others can
continue, for example to decode commands `0x02`, `0x05`, `0x09` and `0x0a`; one of
them may control the built-in blue press flash. See the main README for what is
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
