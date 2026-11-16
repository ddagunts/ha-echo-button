"""Make custom_components/echo_button importable as `echo_button` without Home Assistant.

protocol.py, bluez.py, link.py and client.py don't import Home Assistant, so they
can be tested with just pytest and dbus-fast installed.
"""

import sys
import types
from pathlib import Path

_pkg = types.ModuleType("echo_button")
_pkg.__path__ = [str(Path(__file__).resolve().parent.parent / "custom_components" / "echo_button")]
sys.modules.setdefault("echo_button", _pkg)
