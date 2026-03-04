"""Sensor platform for BVK Water Monitor."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, SENSOR_DAILY, SENSOR_METER_INDEX, SENSOR_MONTHLY
from .coordinator import BVKWaterCoordinator


@dataclass(frozen=True, kw_only=True)
class BVKSensorDescription(SensorEntityDescription):
    """Extended description with a data_key to read from coordinator.data."""

    data_key: str = ""


SENSOR_DESCRIPTIONS: tuple[BVKSensorDescription, ...] = (
    BVKSensorDescription(
        key=SENSOR_METER_INDEX,
        data_key="meter_index_m3",
        name="Water Meter Index",
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        device_class=SensorDeviceClass.WATER,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:water-pump",
    ),
    BVKSensorDescription(
        key=SENSOR_DAILY,
        data_key="daily_l",
        name="Daily Water Consumption",
        native_unit_of_measurement=UnitOfVolume.LITERS,
        device_class=SensorDeviceClass.WATER,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:water",
    ),
    BVKSensorDescription(
        key=SENSOR_MONTHLY,
        data_key="monthly_l",
        name="Monthly Water Consumption",
        native_unit_of_measurement=UnitOfVolume.LITERS,
        device_class=SensorDeviceClass.WATER,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:water-outline",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up BVK Water Monitor sensors — one set of sensors per consumption place."""
    coordinator: BVKWaterCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[BVKWaterSensor] = []
    for cp_id, place_data in coordinator.data.items():
        cp_num = place_data.get("label", cp_id)
        for description in SENSOR_DESCRIPTIONS:
            entities.append(
                BVKWaterSensor(coordinator, description, cp_id, cp_num, entry.entry_id)
            )

    async_add_entities(entities)


class BVKWaterSensor(CoordinatorEntity[BVKWaterCoordinator], SensorEntity):
    """A single water consumption sensor for one consumption place."""

    entity_description: BVKSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: BVKWaterCoordinator,
        description: BVKSensorDescription,
        cp_id: str,
        cp_num: str,
        entry_id: str,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._cp_id = cp_id
        # Unique ID includes the consumption place so each meter gets distinct sensors
        self._attr_unique_id = f"{entry_id}_{cp_id}_{description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, f"{entry_id}_{cp_id}")},
            "name": f"BVK Water Meter {cp_num}",
            "manufacturer": "BVK / SUEZ Smart Solutions",
            "model": "Smart Water Meter",
        }

    @property
    def _place_data(self) -> dict[str, Any]:
        if self.coordinator.data is None:
            return {}
        return self.coordinator.data.get(self._cp_id, {})

    @property
    def native_value(self) -> float | None:
        value = self._place_data.get(self.entity_description.data_key)
        return float(value) if value is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self._place_data
        attrs: dict[str, Any] = {}
        if self.entity_description.key == SENSOR_DAILY:
            attrs["reading_date"] = data.get("daily_date")
            history = data.get("daily_history")
            if history:
                attrs["last_7_days"] = dict(list(history.items())[-7:])
        elif self.entity_description.key == SENSOR_METER_INDEX:
            attrs["last_reading_at"] = data.get("last_reading_at")
        return {k: v for k, v in attrs.items() if v is not None}
