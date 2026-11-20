"""The example animations in examples/ must be valid for the firmware and the integration."""

from pathlib import Path

import pytest

from echo_button import abutton as A
from echo_button.protocol import MAX_STEPS as STOCK_MAX_STEPS

yaml = pytest.importorskip("yaml")
EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "animations.yaml"


def _color(value) -> tuple[int, int, int]:
    return A.parse_hex_color(value) if isinstance(value, str) else tuple(value)


def _animations():
    scripts = yaml.safe_load(EXAMPLES.read_text())["script"]
    for name, script in scripts.items():
        for action in script["sequence"]:
            if action.get("action") == "echo_button.play_animation":
                yield name, action["data"]


def test_examples_exist():
    assert {name for name, _ in _animations()} >= {"abutton_aurora", "abutton_rainbow", "abutton_doorbell"}


@pytest.mark.parametrize(("name", "data"), list(_animations()))
def test_example_is_a_valid_animation(name, data):
    steps = [A.Step(s["duration"], _color(s["color"]), s.get("fade", False)) for s in data["steps"]]
    writes = A.led_animation(steps, data.get("repeat", 1))        # raises on any limit
    assert all(len(w) <= A.DEFAULT_WRITE_SIZE for w in writes)
    assert len(steps) <= STOCK_MAX_STEPS


def test_web_page_presets_match_the_examples():
    """web/index.html carries the same animations as presets (regenerate it if this fails)."""
    import json
    import re

    html = (EXAMPLES.parent.parent / "web" / "index.html").read_text()
    presets = json.loads(re.search(r'<script type="application/json" id="presets">(.*?)</script>', html, re.S)[1])
    scripts = yaml.safe_load(EXAMPLES.read_text())["script"]
    expected = []
    for script in scripts.values():
        data = script["sequence"][0]["data"]
        expected.append({
            "name": script["alias"].split(": ", 1)[1], "repeat": data.get("repeat", 1),
            "steps": [{"ms": s["duration"], "color": list(_color(s["color"])), "fade": bool(s.get("fade"))}
                      for s in data["steps"]],
        })
    assert presets == expected
