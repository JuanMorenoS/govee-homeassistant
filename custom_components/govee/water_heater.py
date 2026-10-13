"""Water heater platform for Govee integration.

Exposes Govee smart kettles (e.g. H7170) as HA water_heater entities:
power on/off, target temperature (``sliderTemperature``) and the live water
temperature (``sensorTemperature``). Brewing modes (``workMode``) are not
exposed: they start the kettle, and HomeKit turns mode selectors into
switches whose OFF still selects the mode, so Siri could start boiling.

A water_heater is also what HomeKit Bridge turns into a thermostat with
Heat/Off and a target temperature, so the kettle becomes fully controllable
from Apple Home and Siri.

On the H7170 writing ``sliderTemperature`` starts heating. So while the
kettle is off a new target is only remembered (coordinator.kettle_pending)
and shown, and it is sent when the kettle is turned on, from this entity or
the power switch. A kettle must never start boiling because someone moved a
slider.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.water_heater import WaterHeaterEntity, WaterHeaterEntityFeature
from homeassistant.const import ATTR_TEMPERATURE, STATE_OFF, STATE_ON, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_API_TEMPERATURE_UNIT,
    DEFAULT_API_TEMPERATURE_UNIT,
    resolve_fahrenheit_conversion,
)
from .coordinator import GoveeConfigEntry, GoveeCoordinator
from .entity import GoveeEntity
from .models import DeviceCommand, GoveeDevice, KettleTemperatureCommand, PowerCommand

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


def kettle_turn_on_commands(coordinator: GoveeCoordinator, device_id: str) -> list[DeviceCommand]:
    """Commands that turn a kettle on, applying a target held while it was off.

    Shared by the water_heater entity and the kettle power switch.
    """
    commands: list[DeviceCommand] = []
    target = coordinator.kettle_pending(device_id).pop("target", None)
    if target is not None:
        commands.append(KettleTemperatureCommand(temperature=int(target)))
    commands.append(PowerCommand(power_on=True))
    return commands


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GoveeConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Govee kettle water heater entities from a config entry."""
    coordinator: GoveeCoordinator = entry.runtime_data

    entities: list[WaterHeaterEntity] = []
    for device in coordinator.devices.values():
        if device.supports_kettle_temperature and not device.is_group:
            entities.append(GoveeKettleEntity(coordinator, device))
            _LOGGER.debug("Created kettle water_heater entity for %s (%s)", device.name, device.sku)

    async_add_entities(entities)


class GoveeKettleEntity(GoveeEntity, WaterHeaterEntity):
    """Govee smart kettle as a water heater."""

    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 1

    def __init__(self, coordinator: GoveeCoordinator, device: GoveeDevice) -> None:
        """Initialize the kettle entity."""
        super().__init__(coordinator, device)
        self._attr_name = None  # use device name (has_entity_name = True)

        min_t, max_t = device.get_kettle_temperature_range()
        self._attr_min_temp = float(min_t)
        self._attr_max_temp = float(max_t)

        self._attr_supported_features = WaterHeaterEntityFeature.TARGET_TEMPERATURE | WaterHeaterEntityFeature.ON_OFF

    # --------------------------------------------------------------------- #
    # State
    # --------------------------------------------------------------------- #

    @property
    def current_temperature(self) -> float | None:
        """Return the water temperature in Celsius.

        ``sensorTemperature`` comes in the unit the kettle is set to (the
        ``sliderTemperature`` STRUCT names it), resolved the same way as the
        temperature sensor entity.
        """
        state = self.device_state
        if state is None or state.sensor_temperature is None:
            return None
        value = float(state.sensor_temperature)
        config_entry = self.coordinator.config_entry
        api_unit = (
            config_entry.options.get(CONF_API_TEMPERATURE_UNIT, DEFAULT_API_TEMPERATURE_UNIT)
            if config_entry is not None
            else DEFAULT_API_TEMPERATURE_UNIT
        )
        unit_hint = state.device_temperature_unit
        if unit_hint is None:
            unit_hint = self.coordinator.account_temperature_unit(self._device_id)
        if resolve_fahrenheit_conversion(self._device.sku, api_unit, unit_hint):
            value = (value - 32.0) * (5.0 / 9.0)
        return round(value, 1)

    @property
    def target_temperature(self) -> float | None:
        """Return the target temperature in Celsius (a pending one while off)."""
        state = self.device_state
        pending = self._pending.get("target")
        if pending is not None and not (state and state.power_state):
            return float(pending)
        if state is None or state.kettle_target_temperature is None:
            return None
        return float(state.kettle_target_temperature)

    @property
    def current_operation(self) -> str | None:
        """Return "on" while heating and "off" otherwise."""
        state = self.device_state
        if state is None:
            return None
        return STATE_ON if state.power_state else STATE_OFF

    @property
    def is_on(self) -> bool | None:
        """Return True while the kettle is heating."""
        state = self.device_state
        return state.power_state if state else None

    @property
    def _pending(self) -> dict[str, Any]:
        return self.coordinator.kettle_pending(self._device_id)

    @property
    def _is_heating(self) -> bool:
        state = self.device_state
        return bool(state and state.power_state)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Drop settings held for an off kettle once it is on by other means."""
        if self._is_heating:
            self._pending.clear()
        super()._handle_coordinator_update()

    # --------------------------------------------------------------------- #
    # Commands
    # --------------------------------------------------------------------- #

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Start heating, applying a target chosen while off."""
        for command in kettle_turn_on_commands(self.coordinator, self._device_id):
            await self._async_send_command(command)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop heating."""
        await self._async_send_command(PowerCommand(power_on=False))

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set the target temperature (Celsius, clamped to the kettle's range).

        Sent right away only while heating; while off it is held until turn_on.
        """
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        target = round(max(self._attr_min_temp, min(self._attr_max_temp, float(temperature))))
        if self._is_heating:
            await self._async_send_command(KettleTemperatureCommand(temperature=target))
            return
        self._pending["target"] = target
        self.async_write_ha_state()
