"""Water heater platform for Govee integration.

Exposes Govee smart kettles (e.g. H7170) as HA water_heater entities:
power on/off, target temperature (``sliderTemperature``), the live water
temperature (``sensorTemperature``) and the brewing modes (``workMode``) as
operation modes.

A water_heater is also what HomeKit Bridge turns into a thermostat with
Heat/Off and a target temperature, so the kettle becomes fully controllable
from Apple Home and Siri.

On the H7170 writing ``sliderTemperature`` (and possibly ``workMode``) starts
heating. So while the kettle is off a new target or mode is only remembered
(coordinator.kettle_pending) and shown, and it is sent when the kettle is
turned on. A kettle must never start boiling because someone moved a slider.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.water_heater import WaterHeaterEntity, WaterHeaterEntityFeature
from homeassistant.const import ATTR_TEMPERATURE, STATE_OFF, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_API_TEMPERATURE_UNIT,
    DEFAULT_API_TEMPERATURE_UNIT,
    DOMAIN,
    resolve_fahrenheit_conversion,
)
from .coordinator import GoveeConfigEntry, GoveeCoordinator
from .entity import GoveeEntity
from .models import GoveeDevice, KettleTemperatureCommand, PowerCommand, WorkModeCommand

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


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

        # Brewing modes, keyed by their display name ("Green Tea", "DIY 1"...).
        self._modes: dict[str, tuple[int, int]] = {
            opt["name"]: (opt["work_mode"], opt["mode_value"]) for opt in device.get_kettle_mode_options()
        }

        features = WaterHeaterEntityFeature.TARGET_TEMPERATURE | WaterHeaterEntityFeature.ON_OFF
        if self._modes:
            features |= WaterHeaterEntityFeature.OPERATION_MODE
            self._attr_operation_list = [STATE_OFF, *self._modes]
        self._attr_supported_features = features

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
        """Return "off" while the kettle is off, otherwise its brewing mode."""
        state = self.device_state
        if state is None:
            return None
        if not state.power_state:
            return STATE_OFF
        mode = self._mode_name(state.work_mode, state.mode_value)
        # A powered kettle in a mode we can't name is still heating; report the
        # first mode rather than "off" so HomeKit shows it as heating.
        return mode or (next(iter(self._modes), None) if self._modes else None)

    @property
    def is_on(self) -> bool | None:
        """Return True while the kettle is heating."""
        state = self.device_state
        return state.power_state if state else None

    def _mode_name(self, work_mode: int | None, mode_value: int | None) -> str | None:
        if work_mode is None:
            return None
        candidates = [name for name, (wm, _mv) in self._modes.items() if wm == work_mode]
        for name in candidates:
            if self._modes[name][1] == mode_value:
                return name
        return candidates[0] if candidates else None

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
        """Start heating, applying any target or mode chosen while off."""
        mode = self._pending.pop("mode", None)
        target = self._pending.pop("target", None)
        if mode is not None:
            await self._async_send_command(WorkModeCommand(work_mode=mode[0], mode_value=mode[1]))
        if target is not None:
            await self._async_send_command(KettleTemperatureCommand(temperature=int(target)))
        await self._async_send_command(PowerCommand(power_on=True))

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

    async def async_set_operation_mode(self, operation_mode: str) -> None:
        """Switch brewing mode; "off" turns the kettle off.

        A brewing mode picked while off is held until turn_on (and replaces a
        held DIY target, since the preset brings its own temperature).
        """
        if operation_mode == STATE_OFF:
            await self.async_turn_off()
            return
        mode = self._modes.get(operation_mode)
        if mode is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unsupported_mode",
                translation_placeholders={"device": self._device.name, "mode": operation_mode},
            )
        work_mode, mode_value = mode
        if self._is_heating:
            await self._async_send_command(WorkModeCommand(work_mode=work_mode, mode_value=mode_value))
            return
        self._pending["mode"] = (work_mode, mode_value)
        self._pending.pop("target", None)
        self.async_write_ha_state()
