"""AButton support in the integration: discovery, entities, actions and options."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bleak.exc import BleakError

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import SOURCE_BLUETOOTH
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.echo_button import abutton as A

DOMAIN = "echo_button"
ADDRESS = "00:11:22:33:44:55"
NAME = "EchoBtnABC"


def service_info() -> BluetoothServiceInfoBleak:
    adv = AdvertisementData(local_name=NAME, manufacturer_data={}, service_data={},
                            service_uuids=[A.SERVICE_UUID], tx_power=-127, rssi=-50, platform_data=())
    return BluetoothServiceInfoBleak(
        name=NAME, address=ADDRESS, rssi=-50, manufacturer_data={}, service_data={},
        service_uuids=[A.SERVICE_UUID], source="local", device=BLEDevice(ADDRESS, NAME, None),
        advertisement=adv, connectable=True, time=0, tx_power=-127)


async def setup_abutton(hass: HomeAssistant, options=None) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=ADDRESS, title=NAME,
                            data={CONF_ADDRESS: ADDRESS, "kind": "abutton"}, options=options or {})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_bluetooth_discovery_creates_an_abutton_entry(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_BLUETOOTH},
                                                       data=service_info())
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "bluetooth_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_ADDRESS: ADDRESS, "kind": "abutton"}
    assert result["title"] == NAME
    await hass.async_block_till_done()
    assert hass.states.get("light.echobtnabc_led") is not None
    assert hass.states.get("button.echobtnabc_identify") is not None


async def test_discovery_switches_a_stock_entry_to_abutton(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=ADDRESS, title=NAME, data={CONF_ADDRESS: ADDRESS})
    entry.add_to_hass(hass)
    with patch("custom_components.echo_button.async_setup_entry", AsyncMock(return_value=True)):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_BLUETOOTH},
                                                           data=service_info())
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"
    assert entry.data["kind"] == "abutton"


async def setup_abutton_entry_with(hass: HomeAssistant, options) -> MockConfigEntry:
    """A second AButton entry (another address) with these options."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id="00:11:22:33:44:66", title="EchoBtnDEF",
                            data={CONF_ADDRESS: "00:11:22:33:44:66", "kind": "abutton"}, options=options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_light_animation_and_identify_writes(hass: HomeAssistant) -> None:
    entry = await setup_abutton(hass)
    client = entry.runtime_data.client
    with patch.object(client, "_write", AsyncMock()) as write:
        await hass.services.async_call("light", "turn_on", {"entity_id": "light.echobtnabc_led",
                                       "rgb_color": [255, 0, 0], "brightness": 128}, blocking=True)
        write.assert_awaited_with(A.LED_UUID, [A.led_stop(), A.led_color((128, 0, 0))])
        assert hass.states.get("light.echobtnabc_led").state == "on"

        await hass.services.async_call("light", "turn_off", {"entity_id": "light.echobtnabc_led"}, blocking=True)
        write.assert_awaited_with(A.LED_UUID, [A.led_stop(), A.led_color(None)])

        steps = [{"color": [255, 0, 0], "duration": 300}, {"color": "#0000ff", "duration": 300, "fade": True}]
        await hass.services.async_call(DOMAIN, "play_animation", {"entity_id": "light.echobtnabc_led",
                                       "steps": steps, "repeat": 0}, blocking=True)
        expected = A.led_animation([A.Step(300, (255, 0, 0)), A.Step(300, (0, 0, 255), True)], 0)
        write.assert_awaited_with(A.LED_UUID, expected)

        await hass.services.async_call("button", "press", {"entity_id": "button.echobtnabc_identify"},
                                       blocking=True)
        write.assert_awaited_with(A.COMMAND_UUID, [bytes([A.CMD_IDENTIFY])])


async def test_options_reach_the_button(hass: HomeAssistant) -> None:
    entry = await setup_abutton(hass)
    client = entry.runtime_data.client
    assert client._config == A.Config()                     # defaults until options are set
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "abutton"
    with patch.object(client, "_write", AsyncMock()) as write:
        result = await hass.config_entries.options.async_configure(result["flow_id"], {
            "press_color": [0, 255, 0], "brightness": 64, "long_press_ms": 800, "multi_click_ms": 400,
            "advert_interval_ms": 1000, "low_battery_pct": 15, "low_battery_warning": True,
            "boot_battery": False, "restore_led": True, "tx_power": "-8", "sleep": True})
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    write.assert_awaited_with(A.CONFIG_UUID, [])               # the pending config goes out first
    assert client._config == A.Config(flags=A.FLAG_LOW_BATTERY_WARNING | A.FLAG_RESTORE_LED, brightness=64,
                                      press_color=(0, 255, 0), long_press_ms=800, multi_click_ms=400,
                                      advert_interval_ms=1000, low_battery_pct=15)
    assert client._power == A.Power(tx_power=-8, sleep=True)


async def test_power_defaults_and_maximum(hass: HomeAssistant) -> None:
    entry = await setup_abutton(hass)
    assert entry.runtime_data.client._power == A.Power()            # maximum, no sleep
    entry = await setup_abutton_entry_with(hass, {"tx_power": "max", "sleep": False})
    assert entry.runtime_data.client._power == A.Power()


def fake_ble(firmware: bytes, power_char: bool) -> MagicMock:
    ble = MagicMock()
    ble.is_connected = True
    ble.services.get_service.return_value = object()
    ble.services.get_characteristic.side_effect = \
        lambda uuid: (object() if power_char else None) if uuid == A.POWER_UUID else object()
    ble.read_gatt_char = AsyncMock(return_value=firmware)
    ble.write_gatt_char = AsyncMock()
    ble.clear_cache = AsyncMock(return_value=True)
    ble.disconnect = AsyncMock()
    return ble


async def test_power_goes_out_after_the_config(hass: HomeAssistant) -> None:
    entry = await setup_abutton(hass, {"tx_power": "-16", "sleep": True})
    client = entry.runtime_data.client
    ble = fake_ble(b"0.4.0", power_char=True)
    with patch.object(client, "_establish", AsyncMock(return_value=ble)):
        await client.identify()
    uuids = [c.args[0] for c in ble.write_gatt_char.await_args_list]
    assert uuids == [A.CONFIG_UUID, A.POWER_UUID, A.COMMAND_UUID]
    assert ble.write_gatt_char.await_args_list[1].args[1] == A.Power(-16, True).encode()
    await client.disconnect()


async def test_stale_services_after_an_update_are_refreshed(hass: HomeAssistant) -> None:
    entry = await setup_abutton(hass, {"sleep": True})
    client = entry.runtime_data.client
    stale, fresh = fake_ble(b"0.4.0", power_char=False), fake_ble(b"0.4.0", power_char=True)
    with patch.object(client, "_establish", AsyncMock(side_effect=[stale, fresh])):
        await client.identify()
    stale.clear_cache.assert_awaited_once()
    assert [c.args[0] for c in fresh.write_gatt_char.await_args_list] == [A.CONFIG_UUID, A.POWER_UUID,
                                                                         A.COMMAND_UUID]
    await client.disconnect()


async def test_stock_options_flow_is_unchanged(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=ADDRESS, title=NAME, data={CONF_ADDRESS: ADDRESS})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "init"
    assert "keep_awake" in result["data_schema"].schema


async def test_setup_forgets_a_stale_classic_pairing(hass: HomeAssistant, forget_classic) -> None:
    await setup_abutton(hass)
    forget_classic.assert_awaited_with(ADDRESS)


async def test_connect_retries_after_removing_a_stale_classic_pairing(hass: HomeAssistant, forget_classic) -> None:
    entry = await setup_abutton(hass)
    client = entry.runtime_data.client
    good = MagicMock()
    good.services.get_service.return_value = object()
    good.read_gatt_char = AsyncMock(return_value=b"0.4.0")
    stale = BleakError("Failed to connect after 3 attempt(s): [org.bluez.Error.Failed] br-connection-create-socket")
    forget_classic.return_value = 1
    with patch.object(client, "_establish", AsyncMock(side_effect=[stale, good])) as establish, \
            patch("custom_components.echo_button.abutton_client.asyncio.sleep", AsyncMock()):
        assert await client._connect() is good
    assert establish.await_count == 2
    assert client.firmware == "0.4.0"


async def test_other_connect_errors_are_not_retried(hass: HomeAssistant, forget_classic) -> None:
    entry = await setup_abutton(hass)
    client = entry.runtime_data.client
    with patch.object(client, "_establish", AsyncMock(side_effect=BleakError("le-connection-abort-by-local"))), \
            pytest.raises(BleakError):
        await client._connect()
