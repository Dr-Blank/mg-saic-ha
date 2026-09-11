"""Climate entity: iSmart-aligned modes/presets and local-control handling (#336).

Covers three things that previously had no entity-level coverage at all:

  * status 6 (climate running under the driver's own local control) must
    report the climate as ON rather than Off. Reporting Off was a deliberate
    1.2.0 choice, revisited here -- see the note in hvac_mode.
  * a remote climate command issued while the driver has local control must
    not be sent, because the car rejects it with a generic "instruction
    failed" and the attempt still costs one of the limited remote commands.
  * the preset set offered per vehicle, which is gated on what each car can
    actually do rather than being the same list everywhere.
"""

import asyncio
import importlib.util
import logging
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_DIR = REPO_ROOT / "custom_components" / "mg_saic"
PACKAGE = "mg_saic_climate_test"


class _Values:
    def __getattr__(self, name):
        return name.lower()


class _CoordinatorEntity:
    def __init__(self, coordinator):
        self.coordinator = coordinator

    def async_write_ha_state(self):
        pass


class _ClimateEntityFeature(int):
    TARGET_TEMPERATURE = 1
    FAN_MODE = 8
    PRESET_MODE = 16
    TURN_ON = 256
    TURN_OFF = 128


class _HVACMode:
    OFF = "off"
    COOL = "cool"
    HEAT = "heat"
    FAN_ONLY = "fan_only"
    HEAT_COOL = "heat_cool"


def _module(name, **attributes):
    module = ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_climate():
    homeassistant = _module("homeassistant")
    homeassistant.__path__ = []
    components = _module("homeassistant.components")
    components.__path__ = []
    helpers = _module("homeassistant.helpers")
    helpers.__path__ = []

    class _ClimateEntity:
        _attr_supported_features = 0
        _attr_preset_modes = None
        _attr_preset_mode = None

        def async_write_ha_state(self):
            pass

    climate_mod = _module(
        "homeassistant.components.climate",
        ClimateEntity=_ClimateEntity,
        ClimateEntityFeature=_ClimateEntityFeature,
        HVACMode=_HVACMode,
    )
    climate_mod.__path__ = []
    _module(
        "homeassistant.components.climate.const",
        FAN_LOW="Low",
        FAN_MEDIUM="Medium",
        FAN_HIGH="High",
    )
    _module(
        "homeassistant.helpers.update_coordinator",
        CoordinatorEntity=_CoordinatorEntity,
    )
    _module("homeassistant.helpers.entity", EntityCategory=_Values())
    _module(
        "homeassistant.const",
        UnitOfTemperature=_Values(),
        ATTR_TEMPERATURE="temperature",
    )

    package = _module(PACKAGE)
    package.__path__ = [str(PKG_DIR)]

    class _CommandsLimitReachedException(Exception):
        pass

    class _VehicleNotLockedException(Exception):
        pass

    _module(
        f"{PACKAGE}.api",
        CommandsLimitReachedException=_CommandsLimitReachedException,
        VehicleNotLockedException=_VehicleNotLockedException,
    )
    _module(
        f"{PACKAGE}.const",
        DOMAIN="mg_saic",
        LOGGER=logging.getLogger(PACKAGE),
        FRONT_DEFROST_TEMP_C=28,
        CLIMATE_STATUS_LOCAL_CONTROL=6,
        COMMAND_SYNC_GRACE_SECONDS=30,
    )
    _module(f"{PACKAGE}.utils", create_device_info=lambda *a: {})

    class _Feature:
        REAR_WINDOW_HEAT = "rear_window_heat"

    _module(
        f"{PACKAGE}.backends",
        Feature=_Feature,
        backend_supports=lambda client, feature: feature
        in getattr(client, "supported_features", {feature}),
    )
    return _load(f"{PACKAGE}.climate", PKG_DIR / "climate.py")


CLIMATE = _load_climate()


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Base(unittest.TestCase):
    def _entity(self, *, scheme="mode_select", status=0, heat={2}, defrost={5},
                rear_heat=True, max_cool=3, cool=2):
        coordinator = SimpleNamespace(
            climate_control_scheme=scheme,
            climate_status_heat=heat,
            climate_status_cool={cool},
            climate_status_defrost=defrost,
            climate_status_fan_only={1},
            climate_mode_cool=cool,
            climate_mode_heat=4,
            climate_mode_max_cool=max_cool,
            climate_mode_fan_only=1,
            climate_mode_defrost=5,
            max_cool_forces_min_temp=False,
            cool_uses_start_ac=False,
            climate_fan_auto=None,
            climate_fan_only_airflow=False,
            min_temp=16,
            max_temp=30,
            requested_target_temp=22.0,
            temp_offset=3,
            temp_index_map=None,
            temp_idx_inverted=False,
            data={
                "status": SimpleNamespace(
                    basicVehicleStatus=SimpleNamespace(remoteClimateStatus=status)
                )
            },
            climate_entity=None,
        )
        coordinator.is_climate_under_local_control = MagicMock(return_value=status == 6)
        coordinator.notify_climate_local_control = AsyncMock()
        coordinator.notify_front_defrost_blocked = AsyncMock()
        coordinator.is_climate_blocking_defrost = MagicMock(return_value=False)
        coordinator.notify_command_limit_reached = AsyncMock()
        coordinator.notify_vehicle_not_locked = AsyncMock()
        coordinator.record_command_error = MagicMock()
        coordinator.schedule_action_refresh = MagicMock()
        coordinator.get_ac_temperature_idx = MagicMock(side_effect=lambda temp: int(temp) - 13)

        client = MagicMock()
        client.supported_features = {"rear_window_heat"} if rear_heat else set()
        client.start_climate = AsyncMock()
        client.start_ac = AsyncMock()
        client.stop_ac = AsyncMock()
        client.control_rear_window_heat = AsyncMock()

        vin_info = SimpleNamespace(
            vin="VIN1", brandName="MG", modelName="Test", series="TEST"
        )
        entry = SimpleNamespace(entry_id="e1")
        entity = CLIMATE.SAICMGClimateEntity(
            coordinator, client, entry, vin_info, "VIN1"
        )
        entity.hass = MagicMock()
        return entity


class LocalControlTests(_Base):
    def test_reports_on_not_off_while_under_local_control(self):
        """The bug: the driver is running the heater and the entity said Off."""
        entity = self._entity(status=6)
        self.assertEqual(entity.hvac_mode, _HVACMode.HEAT_COOL)
        self.assertNotEqual(entity.hvac_mode, _HVACMode.OFF)

    def test_applies_to_every_scheme_not_just_one_model(self):
        for scheme in ("mode_select", "fan_speed"):
            with self.subTest(scheme=scheme):
                entity = self._entity(scheme=scheme, status=6)
                self.assertEqual(entity.hvac_mode, _HVACMode.HEAT_COOL)

    def test_command_is_not_sent_and_the_reason_is_explained(self):
        entity = self._entity(status=6)
        _run(entity.async_set_hvac_mode(_HVACMode.COOL))
        entity.coordinator.notify_climate_local_control.assert_awaited_once()
        entity._client.start_climate.assert_not_awaited()
        entity._client.start_ac.assert_not_awaited()

    def test_preset_is_not_sent_under_local_control_either(self):
        entity = self._entity(status=6)
        _run(entity.async_set_preset_mode(CLIMATE.PRESET_LOW))
        entity.coordinator.notify_climate_local_control.assert_awaited_once()
        entity._client.start_climate.assert_not_awaited()

    def test_turning_off_is_still_allowed(self):
        """Off is how a user regains remote control -- it must not be blocked."""
        entity = self._entity(status=6)
        _run(entity.async_set_hvac_mode(_HVACMode.OFF))
        entity._client.stop_ac.assert_awaited_once()
        entity.coordinator.notify_climate_local_control.assert_not_awaited()

    def test_normal_operation_is_unaffected(self):
        entity = self._entity(status=0)
        _run(entity.async_set_hvac_mode(_HVACMode.COOL))
        entity.coordinator.notify_climate_local_control.assert_not_awaited()


class PresetTests(_Base):
    def test_low_and_high_offered_on_a_car_that_heats(self):
        presets = self._entity()._attr_preset_modes
        self.assertIn(CLIMATE.PRESET_LOW, presets)
        self.assertIn(CLIMATE.PRESET_HIGH, presets)

    def test_high_withheld_from_a_car_with_no_heat_capability(self):
        presets = self._entity(heat=set())._attr_preset_modes
        self.assertIn(CLIMATE.PRESET_LOW, presets)
        self.assertNotIn(CLIMATE.PRESET_HIGH, presets)

    def test_windscreen_presets_are_gated_on_real_capability(self):
        both = self._entity()._attr_preset_modes
        self.assertIn(CLIMATE.PRESET_FRONT_WINDSCREEN, both)
        self.assertIn(CLIMATE.PRESET_REAR_WINDSCREEN, both)

        neither = self._entity(defrost=set(), rear_heat=False)._attr_preset_modes
        self.assertNotIn(CLIMATE.PRESET_FRONT_WINDSCREEN, neither)
        self.assertNotIn(CLIMATE.PRESET_REAR_WINDSCREEN, neither)

    def test_low_pins_the_setpoint_to_the_minimum(self):
        entity = self._entity()
        _run(entity.async_set_preset_mode(CLIMATE.PRESET_LOW))
        self.assertEqual(entity.coordinator.requested_target_temp, 16)

    def test_high_pins_the_setpoint_to_the_maximum(self):
        entity = self._entity()
        _run(entity.async_set_preset_mode(CLIMATE.PRESET_HIGH))
        self.assertEqual(entity.coordinator.requested_target_temp, 30)

    def test_rear_windscreen_uses_its_own_command_not_a_climate_mode(self):
        entity = self._entity()
        _run(entity.async_set_preset_mode(CLIMATE.PRESET_REAR_WINDSCREEN))
        entity._client.control_rear_window_heat.assert_awaited_once_with("VIN1", "start")
        entity._client.start_climate.assert_not_awaited()

    def test_presets_are_offered_on_every_scheme(self):
        """Previously only mode_select cars had presets at all."""
        for scheme in ("mode_select", "fan_speed"):
            with self.subTest(scheme=scheme):
                presets = self._entity(scheme=scheme)._attr_preset_modes
                self.assertIn(CLIMATE.PRESET_LOW, presets)


class AcOnModeTests(_Base):
    def test_ac_on_is_offered_on_every_scheme(self):
        for scheme in ("mode_select", "fan_speed"):
            with self.subTest(scheme=scheme):
                self.assertIn(_HVACMode.HEAT_COOL, self._entity(scheme=scheme)._attr_hvac_modes)

    def test_existing_modes_are_not_removed(self):
        """Additive by design: nobody's automations should break."""
        modes = self._entity()._attr_hvac_modes
        for expected in (_HVACMode.OFF, _HVACMode.COOL, _HVACMode.HEAT, _HVACMode.FAN_ONLY):
            self.assertIn(expected, modes)

    def test_ac_on_sends_a_command_at_the_current_setpoint(self):
        entity = self._entity()
        entity.coordinator.requested_target_temp = 24.0
        _run(entity.async_set_hvac_mode(_HVACMode.HEAT_COOL))
        self.assertTrue(
            entity._client.start_climate.await_count
            or entity._client.start_ac.await_count
        )
        self.assertEqual(entity.coordinator.requested_target_temp, 24.0)


if __name__ == "__main__":
    unittest.main()
