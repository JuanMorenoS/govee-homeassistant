"""Tests for the smart kettle water_heater entity and mode select (H7170)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from homeassistant.components.water_heater import WaterHeaterEntityFeature
from homeassistant.const import ATTR_TEMPERATURE, STATE_OFF
from homeassistant.exceptions import ServiceValidationError
import pytest

from custom_components.govee import select as select_mod
from custom_components.govee import water_heater as water_heater_mod
from custom_components.govee.models import (
    GoveeCapability,
    GoveeDevice,
    GoveeDeviceState,
    KettleTemperatureCommand,
    PowerCommand,
    WorkModeCommand,
)
from custom_components.govee.models.device import (
    CAPABILITY_ON_OFF,
    CAPABILITY_TEMPERATURE_SETTING,
    CAPABILITY_WORK_MODE,
    DEVICE_TYPE_KETTLE,
    INSTANCE_POWER,
    INSTANCE_SLIDER_TEMPERATURE,
    INSTANCE_WORK_MODE,
)
from custom_components.govee.select import GoveeKettleModeSelectEntity
from custom_components.govee.water_heater import GoveeKettleEntity

# Capability shapes as returned by the Govee Developer API for a real H7170.
SLIDER_TEMPERATURE = GoveeCapability(
    type=CAPABILITY_TEMPERATURE_SETTING,
    instance=INSTANCE_SLIDER_TEMPERATURE,
    parameters={
        "dataType": "STRUCT",
        "fields": [
            {
                "fieldName": "temperature",
                "dataType": "INTEGER",
                "range": {"min": 40, "max": 100, "precision": 1},
                "required": True,
            },
            {
                "fieldName": "unit",
                "defaultValue": "Celsius",
                "dataType": "ENUM",
                "options": [{"name": "Celsius", "value": "Celsius"}, {"name": "Fahrenheit", "value": "Fahrenheit"}],
                "required": True,
            },
        ],
    },
)
WORK_MODE = GoveeCapability(
    type=CAPABILITY_WORK_MODE,
    instance=INSTANCE_WORK_MODE,
    parameters={
        "dataType": "STRUCT",
        "fields": [
            {
                "fieldName": "workMode",
                "dataType": "ENUM",
                "options": [
                    {"name": "DIY", "value": 1},
                    {"name": "Green Tea", "value": 2},
                    {"name": "Oolong Tea", "value": 3},
                    {"name": "Coffee", "value": 4},
                    {"name": "Black Tea/Boil", "value": 5},
                ],
                "required": True,
            },
            {
                "fieldName": "modeValue",
                "dataType": "ENUM",
                "options": [
                    {
                        "dataType": "ENUM",
                        "name": "DIY",
                        "options": [{"value": 1}, {"value": 2}, {"value": 3}, {"value": 4}],
                    },
                    {"defaultValue": 0, "name": "Green Tea"},
                    {"defaultValue": 0, "name": "Oolong Tea"},
                    {"defaultValue": 0, "name": "Coffee"},
                    {"defaultValue": 0, "name": "Black Tea/Boil"},
                ],
                "required": False,
            },
        ],
    },
)
POWER = GoveeCapability(
    type=CAPABILITY_ON_OFF,
    instance=INSTANCE_POWER,
    parameters={"dataType": "ENUM", "options": [{"name": "on", "value": 1}, {"name": "off", "value": 0}]},
)

EXPECTED_MODES = [
    "DIY 1",
    "DIY 2",
    "DIY 3",
    "DIY 4",
    "Green Tea",
    "Oolong Tea",
    "Coffee",
    "Black Tea/Boil",
]


@pytest.fixture
def kettle() -> GoveeDevice:
    return GoveeDevice(
        device_id="AA:BB:CC:DD:EE:FF:70:70",
        sku="H7170",
        name="Smart Kettle",
        device_type=DEVICE_TYPE_KETTLE,
        capabilities=(POWER, SLIDER_TEMPERATURE, WORK_MODE),
        is_group=False,
    )


def _api_state(
    kettle: GoveeDevice, power: int, target_f: int, sensor_f: float, work_mode: int, **mode
) -> GoveeDeviceState:
    """A state parsed from the /device/state shape the H7170 really returns."""
    state = GoveeDeviceState.create_empty(kettle.device_id)
    state.update_from_api(
        {
            "capabilities": [
                {"type": "devices.capabilities.online", "instance": "online", "state": {"value": True}},
                {"type": "devices.capabilities.on_off", "instance": "powerSwitch", "state": {"value": power}},
                {
                    "type": "devices.capabilities.temperature_setting",
                    "instance": "sliderTemperature",
                    "state": {"value": {"unit": "Fahrenheit", "targetTemperature": target_f}},
                },
                {
                    "type": "devices.capabilities.property",
                    "instance": "sensorTemperature",
                    "state": {"value": sensor_f},
                },
                {
                    "type": "devices.capabilities.work_mode",
                    "instance": "workMode",
                    "state": {"value": {"workMode": work_mode, **mode}},
                },
            ]
        }
    )
    return state


def _entity(cls, kettle: GoveeDevice, state: GoveeDeviceState | None, *args):
    coordinator = MagicMock()
    coordinator.devices = {kettle.device_id: kettle}
    coordinator.get_state = MagicMock(return_value=state)
    coordinator.async_control_device = AsyncMock(return_value=True)
    coordinator.last_update_success = True
    coordinator.config_entry.options = {}
    coordinator.account_temperature_unit = MagicMock(return_value=None)
    entity = cls(coordinator, kettle, *args)
    entity.hass = MagicMock()
    entity.async_write_ha_state = MagicMock()
    return entity


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #


def test_capability_helpers(kettle):
    assert kettle.supports_kettle_temperature is True
    assert kettle.get_kettle_temperature_range() == (40, 100)
    modes = kettle.get_kettle_mode_options()
    assert [m["name"] for m in modes] == EXPECTED_MODES
    assert modes[1] == {"name": "DIY 2", "work_mode": 1, "mode_value": 2}
    assert modes[-1] == {"name": "Black Tea/Boil", "work_mode": 5, "mode_value": 0}


def test_non_kettle_has_no_kettle_helpers(kettle):
    heater = GoveeDevice(
        device_id="x",
        sku="H713B",
        name="Heater",
        device_type="devices.types.heater",
        capabilities=(POWER, SLIDER_TEMPERATURE, WORK_MODE),
        is_group=False,
    )
    assert heater.supports_kettle_temperature is False
    assert heater.get_kettle_mode_options() == []


def test_kettle_temperature_command_payload():
    payload = KettleTemperatureCommand(temperature=85).to_api_payload()
    assert payload["type"] == CAPABILITY_TEMPERATURE_SETTING
    assert payload["instance"] == INSTANCE_SLIDER_TEMPERATURE
    assert payload["value"] == {"temperature": 85, "unit": "Celsius"}


def test_state_parses_fahrenheit_target_as_celsius(kettle):
    state = _api_state(kettle, power=0, target_f=190, sensor_f=70, work_mode=1)
    assert state.kettle_target_temperature == 88  # 190 °F
    assert state.device_temperature_unit == "Fahrenheit"
    assert state.heater_temperature is None


# --------------------------------------------------------------------------- #
# Water heater entity
# --------------------------------------------------------------------------- #


async def test_platform_creates_entity_for_kettle_only(kettle):
    coordinator = MagicMock()
    coordinator.devices = {kettle.device_id: kettle}
    entry = MagicMock()
    entry.runtime_data = coordinator
    added: list = []
    await water_heater_mod.async_setup_entry(MagicMock(), entry, added.extend)
    assert len(added) == 1
    assert isinstance(added[0], GoveeKettleEntity)


def test_entity_attributes(kettle):
    entity = _entity(GoveeKettleEntity, kettle, _api_state(kettle, 0, 190, 70, 1, modeValue=1))
    assert entity.min_temp == 40
    assert entity.max_temp == 100
    assert entity.supported_features & WaterHeaterEntityFeature.ON_OFF
    assert entity.supported_features & WaterHeaterEntityFeature.TARGET_TEMPERATURE
    assert entity.supported_features & WaterHeaterEntityFeature.OPERATION_MODE
    assert entity.operation_list == [STATE_OFF, *EXPECTED_MODES]
    assert entity.target_temperature == 88
    assert entity.current_temperature == pytest.approx(21.1)
    assert entity.current_operation == STATE_OFF
    assert entity.is_on is False


def test_current_operation_when_heating(kettle):
    entity = _entity(GoveeKettleEntity, kettle, _api_state(kettle, 1, 212, 150, 2, modeValue=0))
    assert entity.current_operation == "Green Tea"
    assert entity.is_on is True

    entity = _entity(GoveeKettleEntity, kettle, _api_state(kettle, 1, 212, 150, 1, modeValue=3))
    assert entity.current_operation == "DIY 3"


def test_no_state(kettle):
    entity = _entity(GoveeKettleEntity, kettle, None)
    assert entity.current_temperature is None
    assert entity.target_temperature is None
    assert entity.current_operation is None


async def test_turn_on_off(kettle):
    entity = _entity(GoveeKettleEntity, kettle, None)
    await entity.async_turn_on()
    await entity.async_turn_off()
    sent = [c.args[1] for c in entity.coordinator.async_control_device.await_args_list]
    assert sent == [PowerCommand(power_on=True), PowerCommand(power_on=False)]


async def test_set_temperature_is_clamped(kettle):
    entity = _entity(GoveeKettleEntity, kettle, None)
    await entity.async_set_temperature(**{ATTR_TEMPERATURE: 85.4})
    await entity.async_set_temperature(**{ATTR_TEMPERATURE: 120})
    await entity.async_set_temperature(**{ATTR_TEMPERATURE: 10})
    await entity.async_set_temperature()  # no temperature: ignored
    sent = [c.args[1] for c in entity.coordinator.async_control_device.await_args_list]
    assert sent == [
        KettleTemperatureCommand(temperature=85),
        KettleTemperatureCommand(temperature=100),
        KettleTemperatureCommand(temperature=40),
    ]


async def test_set_operation_mode(kettle):
    entity = _entity(GoveeKettleEntity, kettle, None)
    await entity.async_set_operation_mode("Coffee")
    await entity.async_set_operation_mode("DIY 2")
    await entity.async_set_operation_mode(STATE_OFF)
    sent = [c.args[1] for c in entity.coordinator.async_control_device.await_args_list]
    assert sent == [
        WorkModeCommand(work_mode=4, mode_value=0),
        WorkModeCommand(work_mode=1, mode_value=2),
        PowerCommand(power_on=False),
    ]
    with pytest.raises(ServiceValidationError):
        await entity.async_set_operation_mode("Espresso")


# --------------------------------------------------------------------------- #
# Mode select
# --------------------------------------------------------------------------- #


async def test_select_platform_creates_kettle_mode(kettle):
    coordinator = MagicMock()
    coordinator.devices = {kettle.device_id: kettle}
    entry = MagicMock()
    entry.runtime_data = coordinator
    entry.options = {}
    added: list = []
    await select_mod.async_setup_entry(MagicMock(), entry, added.extend)
    kettle_selects = [e for e in added if isinstance(e, GoveeKettleModeSelectEntity)]
    assert len(kettle_selects) == 1
    assert kettle_selects[0].options == EXPECTED_MODES
    assert kettle_selects[0].unique_id.endswith("_kettle_mode")


async def test_select_option_sends_work_mode(kettle):
    entity = _entity(
        GoveeKettleModeSelectEntity,
        kettle,
        _api_state(kettle, 1, 212, 150, 3, modeValue=0),
        kettle.get_kettle_mode_options(),
    )
    assert entity.current_option == "Oolong Tea"
    await entity.async_select_option("Black Tea/Boil")
    entity.coordinator.async_control_device.assert_awaited_once_with(
        kettle.device_id, WorkModeCommand(work_mode=5, mode_value=0)
    )
