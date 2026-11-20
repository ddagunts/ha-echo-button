# Prebuilt images

Ready-to-flash images, so you don't need the SDK. Use them with
[`../GUIDE.md`](../GUIDE.md). Each `.bin` is a full 512 KiB flash image, but **flash only
`--sectors 0x1000,0x4000-0x1e000`**: the rest of a unit's flash (its MAC, calibration,
serials and pairing keys) stays the unit's own. Checksums: `SHA256SUMS`.

| File | What | Tested |
|---|---|---|
| `abutton-0.3.0.bin` | AButton 0.3.0, release (no UART traces) | running on two rev-1 units (installed over the air); BTHome encryption verified with Home Assistant; flash mode experimental |
| `abutton-0.3.0.ota.signed` | the same, as a signed OTA update | installed with it |
| `abutton-0.3.0-dbg.bin` | AButton 0.3.0 with UART traces (`research/echobtn_trace.py`) | built from the same source; not run on hardware |
| `abutton-0.3.0-dbg.ota.signed` | the same, as a signed OTA update | |

- **AButton images** are generic: no unit identity. The SDK placeholder MAC in their sector 0
  is never flashed; the button keeps its own MAC and names itself `EchoBtn` + the last 3
  characters of its serial.
- **Signing key:** these images are built with the repository maintainer's public key. Over
  the air, a button running them accepts **only** updates signed by the maintainer (the
  `.ota.signed` files published here). These images (0.2.1 and later) include a UART flash
  mode meant to let owners with the test-pad wiring flash their own builds; it's untested
  over the UART so far (guide step 6b).

**Stock firmware:** not published here (it's Amazon's). Your own dump (guide step 3) is the
way back to stock. A stock image of a rev-1 unit, the firmware sectors only with no unit
data, is available from the maintainer on request; `research/fw_mod.py` can make a
white-press-flash variant of any stock dump (`--press-color ffffff`).

**No warranty.** Opening, wiring and reflashing a button is entirely at your own risk. The maintainer is not responsible for bricked, broken or unflashable buttons, or any other damage, however it happens.
