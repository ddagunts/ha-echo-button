"""Home Assistant-level tests (need Home Assistant and its test harness).

    python3 -m venv .venv-ha && .venv-ha/bin/pip install pytest-homeassistant-custom-component dbus-fast
    .venv-ha/bin/pytest tests_ha -o asyncio_mode=auto
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # custom_components/


@pytest.fixture(autouse=True)
def auto_enable(enable_custom_integrations, mock_bluetooth):
    yield


@pytest.fixture(autouse=True)
def forget_classic():
    """Never touch the host's real BlueZ from tests."""
    with patch("custom_components.echo_button.bluez.forget_classic_pairing", AsyncMock(return_value=0)) as m:
        yield m
