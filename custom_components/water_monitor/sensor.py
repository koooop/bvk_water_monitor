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

from .const import CONF_USERNAME, DOMAIN, SENSOR_DAILY, SENSOR_METER_INDEX, SENSOR_MONTHLY
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
    """Set up BVK Water Monitor sensors from a config entry."""
    coordinator: BVKWaterCoordinator = hass.data[DOMAIN][entry.entry_id]
    username = entry.data[CONF_USERNAME]

    async_add_entities(
        BVKWaterSensor(coordinator, description, username, entry.entry_id)
        for description in SENSOR_DESCRIPTIONS
    )


class BVKWaterSensor(CoordinatorEntity[BVKWaterCoordinator], SensorEntity):
    """A single water consumption sensor backed by the BVK coordinator."""

    entity_description: BVKSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: BVKWaterCoordinator,
        description: BVKSensorDescription,
        username: str,
        entry_id: str,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry_id}_{description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry_id)},
            "name": f"BVK Water Meter ({username})",
            "manufacturer": "BVK / SUEZ Smart Solutions",
            "model": "Smart Water Meter",
        }

    @property
    def native_value(self) -> float | None:
        """Return the current sensor value from coordinator data."""
        if self.coordinator.data is None:
            return None
        value = self.coordinator.data.get(self.entity_description.data_key)
        if value is None:
            return None
        return float(value)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose extra attributes relevant to this sensor."""
        if not self.coordinator.data:
            return {}
        attrs: dict[str, Any] = {}
        data = self.coordinator.data
        if self.entity_description.key == SENSOR_DAILY:
            attrs["reading_date"] = data.get("daily_date")
            history = data.get("daily_history")
            if history:
                # Last 7 days as extra attribute
                items = list(history.items())[-7:]
                attrs["last_7_days"] = {d: v for d, v in items}
        elif self.entity_description.key == SENSOR_METER_INDEX:
            attrs["last_reading_at"] = data.get("last_reading_at")
        return {k: v for k, v in attrs.items() if v is not None}
