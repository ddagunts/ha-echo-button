#!/usr/bin/env python3
"""Generate candidate encodings of "show one solid color" and feed them to echobtn_lab.py.

Each group of candidates uses one distinctive color, so the colors an observer sees
identify the group that contained a working encoding. Wrong guesses are silently
ignored by the button; a right one lights the LED for ~3 s.

  python3 research/light_sweep.py list                               # groups and counts
  python3 research/light_sweep.py run --fifo /tmp/lab.fifo [group ...]  # default: all groups
  python3 research/light_sweep.py run --fifo /tmp/lab.fifo red:0-14     # a slice of one group
  python3 research/light_sweep.py run --fifo /tmp/lab.fifo --status     # status request after each

How command 06 was found: the color-coded groups found nothing. The "fill" groups
(a header of 01 bytes, then FF bytes) worked because, under most layouts, they
read as count 1 / repeat 1 / white / long duration. That leaves only the field
lengths to guess. Group fill06-01, candidate h=4 s=6, lit up white; bisecting and
color-coded follow-ups then decoded every field (see the main README).
"""

import argparse
import itertools
import time

DUR_MS = 3000
GAP_S = 0.2
GROUP_PAUSE_S = 0.5

COLORS = {
    "red": "ff0000", "green": "00ff00", "blue": "0000ff", "white": "ffffff",
    "yellow": "ffff00", "magenta": "ff00ff", "cyan": "00ffff",
}


def u16be(v):
    return f"{v >> 8 & 0xFF:02x}{v & 0xFF:02x}"


def u16le(v):
    return f"{v & 0xFF:02x}{v >> 8 & 0xFF:02x}"


# One animation step. Keys are labels, values build hex from (rgb, duration).
STEPS = {
    "dBE+rgb+blend": lambda c, d: u16be(d) + c + "00",
    "dBE+rgb": lambda c, d: u16be(d) + c,
    "rgb+dBE": lambda c, d: c + u16be(d),
    "rgb+dBE+blend": lambda c, d: c + u16be(d) + "00",
    "rgb+blend+dBE": lambda c, d: c + "00" + u16be(d),
    "dLE+rgb+blend": lambda c, d: u16le(d) + c + "00",
    "dLE+rgb": lambda c, d: u16le(d) + c,
    "rgb+dLE": lambda c, d: c + u16le(d),
}

# Header layouts. T = trigger, DD = 16-bit delay, R = repeat, L = light mask,
# N = step count, A = animation count. Fields are filled in by fill().
HEADERS = [
    "T DD R N", "T DD A R L N", "L T DD R N", "T DD L R N", "T DD R L N", "T DD N R",
    "T DD A L R N", "L R N T DD", "A L R N T DD", "R N", "N", "L R N", "R L N",
    "T R N", "T DD R", "T DD L R", "L T DD R", "T DD",
]


def fill(header: str, trigger: str) -> str:
    out = []
    for field in header.split():
        out.append({"T": trigger, "DD": "0000", "R": "01", "L": "01", "N": "01", "A": "01"}[field])
    return "".join(out)


def light_candidates(msg_type, color, headers, trigger):
    for header, (step_name, step) in itertools.product(headers, STEPS.items()):
        data = fill(header, trigger) + step(color, DUR_MS)
        yield msg_type, data, f"type {msg_type} trig {trigger} hdr [{header}] step [{step_name}]"


def simple_candidates(color):
    """Fixed-size 'set color' style commands on every data-taking command type."""
    for msg_type in ("02", "05", "06", "09", "0a"):
        for layout in ("{c}", "01{c}", "{c}" + u16be(DUR_MS), "01{c}" + u16be(DUR_MS),
                       "00{c}", "0001{c}", "{c}00", "01{c}00", "{c}" + u16be(DUR_MS) + "00"):
            yield msg_type, layout.format(c=color), f"type {msg_type} simple [{layout}]"


def fill_candidates(msg_type, first_byte):
    """Header of small values (first byte varied, rest 01) + one step of all FF.

    Under most layouts that reads as repeat 1 / one step / light 1 / white /
    ~65 s, so only the header and step lengths have to be right.
    """
    for h in range(0, 11):
        for s in range(3, 9):
            header = "" if h == 0 else first_byte + "01" * (h - 1)
            yield msg_type, header + "ff" * s, f"type {msg_type} fill first={first_byte} h={h} s={s}"


def groups():
    h = HEADERS
    fills = {f"fill{t}-{fb}": list(fill_candidates(t, fb))
             for t in ("02", "05", "06") for fb in ("00", "01", "02")}
    return fills | {
        "red": list(light_candidates("02", COLORS["red"], h[:6], "00")),
        "green": list(light_candidates("02", COLORS["green"], h[6:12], "00")),
        "blue": list(light_candidates("02", COLORS["blue"], h[12:], "00")),
        "white": list(light_candidates("02", COLORS["white"], [x for x in h if "T" in x][:7], "01")),
        "yellow": list(light_candidates("02", COLORS["yellow"], [x for x in h if "T" in x][7:], "01")),
        "magenta": list(simple_candidates(COLORS["magenta"])),
        "cyan": list(light_candidates("05", COLORS["cyan"], h[:2], "00"))
                + list(light_candidates("06", COLORS["cyan"], h[:2], "00")),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["list", "run"])
    ap.add_argument("groups", nargs="*", help="group names, optionally with :first-last slices")
    ap.add_argument("--fifo", help="control FIFO of a running echobtn_lab.py (required for run)")
    ap.add_argument("--status", action="store_true", help="send a status request (type 07) after each candidate")
    args = ap.parse_args()
    all_groups = groups()
    if args.cmd == "list":
        for name, cands in all_groups.items():
            print(f"{name:10} {len(cands):3} candidates, ~{len(cands) * GAP_S + 3:.0f} s")
        return
    if not args.fifo:
        ap.error("run needs --fifo")
    with_status = args.status
    selection = args.groups or list(all_groups)
    with open(args.fifo, "w") as fifo:
        for sel in selection:
            name, _, rng = sel.partition(":")
            cands = all_groups[name]
            if rng:
                a, b = (int(x) for x in rng.split("-"))
                cands = [(i, c) for i, c in enumerate(cands) if a <= i <= b]
            else:
                cands = list(enumerate(cands))
            fifo.write(f"# GROUP {sel} ({len(cands)} candidates)\n")
            fifo.flush()
            for i, (msg_type, data, label) in cands:
                fifo.write(f"# {name}[{i}] {label}\n")
                fifo.write(f"msg {msg_type} {data}\n")
                fifo.flush()
                time.sleep(GAP_S)
                if with_status:
                    fifo.write("msg 07 \n")
                    fifo.flush()
                    time.sleep(0.15)
            fifo.write(f"# END GROUP {sel}\n")
            fifo.flush()
            time.sleep(GROUP_PAUSE_S)


if __name__ == "__main__":
    main()
