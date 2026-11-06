"""Water heater platform for Govee kettles.

H7175: one ``water_heater`` entity with the target temperature, the brew
modes and power (GoveeKettleWaterHeater, below).

Other kettles with a ``sliderTemperature`` setpoint (H7170):
GoveeKettleEntity, with power, the target temperature and the live water
temperature, all in °C. Brewing modes are not exposed: they start the kettle,
and HomeKit turns mode selectors into switches whose OFF still selects the
mode, so Siri could start boiling. On the H7170 writing ``sliderTemperature``
starts heating, so while the kettle is off a new target is only remembered
(coordinator.kettle_pending) and shown, and it is sent when the kettle is
turned on, from this entity or the power switch. A kettle must never start
boiling because someone moved a slider.

Units. The entity works in the unit the kettle reports (see
:meth:`KettleManager.reports_fahrenheit`) and Home Assistant converts for
display. The capability's 40-100 range is °C and is converted for a kettle
that reports °F.

Modes (see :mod:`.kettle.modes`). Following Home Assistant's convention a
powered-off kettle reports ``off``, choosing another mode while it is off
switches it on in that mode (the mode is sent first, so it never heats in the
previous one), and ``off`` powers it off. Mode states are stable keys
(``custom_1``, ``green_tea``, ``manual``, ...) translated for display.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.water_heater import (
    ATTR_OPERATION_MODE,
    WaterHeaterEntity,
    WaterHeaterEntityFeature,
)
from homeassistant.const import ATTR_TEMPERATURE, PRECISION_WHOLE, STATE_OFF, STATE_ON, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_API_TEMPERATURE_UNIT,
    DEFAULT_API_TEMPERATURE_UNIT,
    DOMAIN,
    SUFFIX_KETTLE,
    resolve_fahrenheit_conversion,
)
from .coordinator import GoveeConfigEntry, GoveeCoordinator
from .entity import GoveeEntity
from .kettle.entities import KettleModeMixin
from .kettle.modes import KETTLE_MANUAL_MODE, kettle_modes, manual_command, selected_mode, to_kettle_unit
from .models import DeviceCommand, GoveeDevice, KettleTemperatureCommand, PowerCommand, WorkModeCommand

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
    """Set up the kettle water heaters."""
    coordinator: GoveeCoordinator = entry.runtime_data
    entities: list[WaterHeaterEntity] = []
    for device in coordinator.devices.values():
        if device.is_group or not device.is_kettle:
            continue
        if device.decodes_kettle_frames:
            entities.append(GoveeKettleWaterHeater(coordinator, device))
        elif device.supports_kettle_temperature:
            entities.append(GoveeKettleEntity(coordinator, device))
            _LOGGER.debug("Created kettle water_heater entity for %s (%s)", device.name, device.sku)
    async_add_entities(entities)


class GoveeKettleWaterHeater(KettleModeMixin, WaterHeaterEntity):
    """H7175 kettle as a water heater: target, brew modes, power."""

    _attr_translation_key = "kettle"
    _attr_name = None  # the main feature: the device name
    _attr_precision = PRECISION_WHOLE
    _attr_target_temperature_step = 1

    def __init__(self, coordinator: GoveeCoordinator, device: GoveeDevice) -> None:
        """Initialize the kettle entity."""
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}{SUFFIX_KETTLE}"
        self._range_celsius = device.get_kettle_temperature_range()
        self._has_power = device.supports_power
        self._modes = kettle_modes(device)
        features = WaterHeaterEntityFeature(0)
        if device.supports_kettle_temperature:
            features |= WaterHeaterEntityFeature.TARGET_TEMPERATURE
        if self._modes:
            features |= WaterHeaterEntityFeature.OPERATION_MODE
            self._attr_operation_list = ([STATE_OFF] if self._has_power else []) + list(self._modes)
        if self._has_power:
            features |= WaterHeaterEntityFeature.ON_OFF
        self._attr_supported_features = features

    # ------------------------------------------------------------------ #
    # Units and range
    # ------------------------------------------------------------------ #

    @property
    def _fahrenheit(self) -> bool:
        return self.coordinator.kettles.reports_fahrenheit(self._device_id)

    @property
    def temperature_unit(self) -> str:
        """The kettle's own unit."""
        return UnitOfTemperature.FAHRENHEIT if self._fahrenheit else UnitOfTemperature.CELSIUS

    @property
    def min_temp(self) -> float:
        """Lowest settable target, in the entity's unit."""
        return round(to_kettle_unit(self._range_celsius[0], UnitOfTemperature.CELSIUS, self._fahrenheit))

    @property
    def max_temp(self) -> float:
        """Highest settable target, in the entity's unit."""
        return round(to_kettle_unit(self._range_celsius[1], UnitOfTemperature.CELSIUS, self._fahrenheit))

    # ------------------------------------------------------------------ #
    # State
    # ------------------------------------------------------------------ #

    @property
    def current_operation(self) -> str | None:
        """``off`` while powered off, otherwise the selected mode."""
        state = self.device_state
        if state is None:
            return None
        if self._has_power and not state.power_state:
            return STATE_OFF
        return selected_mode(self._modes, state)

    @property
    def current_temperature(self) -> float | None:
        """Water temperature, in the entity's unit."""
        state = self.device_state
        return state.sensor_temperature if state else None

    @property
    def target_temperature(self) -> float | None:
        """Target temperature, in the entity's unit."""
        state = self.device_state
        return state.kettle_target_temperature if state else None

    # ------------------------------------------------------------------ #
    # Commands
    # ------------------------------------------------------------------ #

    def _validation_error(self, key: str, **placeholders: str) -> ServiceValidationError:
        return ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key=key,
            translation_placeholders={"device": self._device.name, **placeholders},
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the kettle on (start heating)."""
        await self._async_send_command(PowerCommand(power_on=True))

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the kettle off."""
        await self._async_send_command(PowerCommand(power_on=False))

    async def async_set_operation_mode(self, operation_mode: str) -> None:
        """Run the kettle in a mode (switching it on), or turn it off for ``off``."""
        if operation_mode == STATE_OFF and self._has_power:
            await self.async_turn_off()
            return
        target = self._modes.get(operation_mode)
        if target is None:
            raise self._validation_error("unsupported_mode", mode=operation_mode)
        state = self.device_state
        if operation_mode == KETTLE_MANUAL_MODE:
            current = state.kettle_target_temperature if state is not None else None
            if current is None:
                raise self._validation_error("kettle_manual_no_target")
            await self._async_send_command(manual_command(current, self._fahrenheit))
        else:
            await self._async_send_command(WorkModeCommand(work_mode=target[0], mode_value=target[1]))
        if self._has_power and state is not None and not state.power_state:
            await self._async_send_command(PowerCommand(power_on=True))

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set the target, in the entity's unit; this selects manual.

        Setting the target puts the kettle in manual, so a target together
        with any other mode is refused.
        """
        operation_mode = kwargs.get(ATTR_OPERATION_MODE)
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            if operation_mode is not None:
                await self.async_set_operation_mode(operation_mode)
            return
        if operation_mode not in (None, KETTLE_MANUAL_MODE):
            raise self._validation_error("kettle_target_with_mode", mode=str(operation_mode))
        clamped = max(self.min_temp, min(self.max_temp, float(temperature)))
        await self._async_send_command(manual_command(clamped, self._fahrenheit))
        state = self.device_state
        if operation_mode is not None and self._has_power and state is not None and not state.power_state:
            await self._async_send_command(PowerCommand(power_on=True))


class GoveeKettleEntity(GoveeEntity, WaterHeaterEntity):
    """A non-H7175 smart kettle (H7170) as a water heater: power and target."""

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
        # The state keeps the setpoint in the kettle's own unit.
        value = float(state.kettle_target_temperature)
        unit = state.device_temperature_unit
        if isinstance(unit, str) and unit.lower() == "fahrenheit":
            value = (value - 32.0) * (5.0 / 9.0)
        return float(round(value))

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
