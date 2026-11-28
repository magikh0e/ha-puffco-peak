"""Sensor platform for Puffco Peak."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import PuffcoConfigEntry, PuffcoCoordinator
from .entity import PuffcoEntity

# Device enum values mapped to Home Assistant state slugs (translated in
# strings.json). Several device states collapse to one slug (initializing).
OPERATING_STATE = {
    0: "initializing",
    1: "initializing",
    2: "initializing",
    3: "off",
    4: "sleep",
    5: "idle",
    6: "selecting_temperature",
    7: "preheating",
    8: "ready",
    9: "finishing",
    10: "showing_version",
    11: "showing_battery",
    12: "factory_test",
    13: "bonding",
}
CHARGE_STATE = {0: "charging", 1: "topping_up", 2: "full", 3: "paused", 4: "done"}
CHARGE_SOURCE = {0: "usb", 1: "qi", 2: "power_dock", 3: "not_charging"}


def _options(mapping: dict[int, str]) -> list[str]:
    seen: list[str] = []
    for value in mapping.values():
        if value not in seen:
            seen.append(value)
    return seen


def _slug(mapping: dict[int, str], value: Any) -> str | None:
    return mapping.get(int(value)) if isinstance(value, (int, float)) else None


@dataclass(frozen=True, kw_only=True)
class PuffcoSensorDescription(SensorEntityDescription):
    """Describes a Puffco sensor, with how to pull its value from poll data."""

    value_fn: Callable[[dict[str, Any]], Any]


SENSORS: tuple[PuffcoSensorDescription, ...] = (
    PuffcoSensorDescription(
        key="current_temperature",
        translation_key="current_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda d: _round(d.get("userHeaterTemp")),
    ),
    PuffcoSensorDescription(
        key="target_temperature",
        translation_key="target_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=0,
        value_fn=lambda d: _round(d.get("userHeaterTempCommand")),
    ),
    PuffcoSensorDescription(
        key="operating_state",
        translation_key="operating_state",
        device_class=SensorDeviceClass.ENUM,
        options=_options(OPERATING_STATE),
        value_fn=lambda d: _slug(OPERATING_STATE, d.get("operatingState")),
    ),
    PuffcoSensorDescription(
        key="battery",
        translation_key="battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda d: _round(d.get("battSoc")),
    ),
    PuffcoSensorDescription(
        key="charge_state",
        translation_key="charge_state",
        device_class=SensorDeviceClass.ENUM,
        options=_options(CHARGE_STATE),
        value_fn=lambda d: _slug(CHARGE_STATE, d.get("battChargeState")),
    ),
    PuffcoSensorDescription(
        key="charge_source",
        translation_key="charge_source",
        device_class=SensorDeviceClass.ENUM,
        options=_options(CHARGE_SOURCE),
        value_fn=lambda d: _slug(CHARGE_SOURCE, d.get("battChargeSource")),
    ),
    PuffcoSensorDescription(
        key="dabs_remaining",
        translation_key="dabs_remaining",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda d: _round(d.get("approxDabsRemaining")),
    ),
    PuffcoSensorDescription(
        key="total_heat_cycles",
        translation_key="total_heat_cycles",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: _int(d.get("totalHeatCycles")),
    ),
    PuffcoSensorDescription(
        key="battery_voltage",
        translation_key="battery_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.get("battVolts"),
    ),
    PuffcoSensorDescription(
        key="battery_temperature",
        translation_key="battery_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.get("battTemp"),
    ),
    PuffcoSensorDescription(
        key="battery_current",
        translation_key="battery_current",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.get("batteryCurrent"),
    ),
    PuffcoSensorDescription(
        key="heater_power",
        translation_key="heater_power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.get("heaterPower"),
    ),
)


def _round(value: Any) -> float | None:
    return round(value, 1) if isinstance(value, (int, float)) else None


def _int(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) else None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PuffcoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Puffco sensors."""
    coordinator = entry.runtime_data
    async_add_entities(PuffcoSensor(coordinator, desc) for desc in SENSORS)


class PuffcoSensor(PuffcoEntity, SensorEntity):
    """A single polled Puffco value."""

    entity_description: PuffcoSensorDescription

    def __init__(
        self, coordinator: PuffcoCoordinator, description: PuffcoSensorDescription
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.address}_{description.key}"

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data or {})

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None
