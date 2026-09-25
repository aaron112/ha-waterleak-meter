"""Shared stubs and fixtures for the Water Leak Detection unit tests.

The integration is written against Home Assistant's ``homeassistant``
package. These tests run without HA installed, so the ``homeassistant.*``
namespace packages the integration imports are stubbed here, before any
``custom_components`` module is imported. The fakes mirror exactly the call
signatures the component uses; anything the component never touches stays
undefined so a future change that depends on new HA surface fails loudly.
"""

from __future__ import annotations

import sys
import types
from datetime import datetime, timezone
from typing import Any, Callable

import pytest


def _make_pkg(name: str, attrs: dict[str, Any] | None = None) -> types.ModuleType:
    pkg = types.ModuleType(name)
    pkg.__path__ = []  # namespace package marker
    if attrs:
        for key, value in attrs.items():
            setattr(pkg, key, value)
    sys.modules[name] = pkg
    return pkg


def _install_ha_stubs() -> None:
    """Populate sys.modules with faithful stubs before the component loads."""
    _make_pkg("homeassistant")
    _make_pkg("homeassistant.components")
    _make_pkg("homeassistant.util")
    _make_pkg("homeassistant.helpers")
    _make_pkg("homeassistant.helpers.event")

    core = _make_pkg("homeassistant.core")
    core.HomeAssistant = object
    core.Event = object
    core.callback = lambda f: f

    pn = _make_pkg("homeassistant.components.persistent_notification")

    dt_util = _make_pkg("homeassistant.util.dt")
    dt_util.utc_from_timestamp = lambda ts: datetime.fromtimestamp(ts, tz=timezone.utc)

    _make_pkg("homeassistant.data_entry_flow", {"FlowResult": object})

    storage = _make_pkg("homeassistant.helpers.storage")

    class Store:
        def __init__(self, hass: Any, version: int, key: str) -> None:
            self.hass = hass
            self.version = version
            self.key = key
            self.data: dict[str, Any] = {}

        async def async_load(self) -> dict[str, Any]:
            return dict(self.data)

        async def async_save(self, data: dict[str, Any]) -> None:
            self.data = dict(data)

    storage.Store = Store

    global StoreStub
    StoreStub = storage.Store  # re-exported so tests can subclass the stub

    # Components: binary_sensor / sensor / switch provide marker base classes
    # and device-class constants only.
    binmod = _make_pkg("homeassistant.components.binary_sensor")

    class BinarySensorDeviceClass:
        PROBLEM = "problem"

    class BinarySensorEntity:
        pass

    binmod.BinarySensorDeviceClass = BinarySensorDeviceClass
    binmod.BinarySensorEntity = BinarySensorEntity

    sensmod = _make_pkg("homeassistant.components.sensor")

    class SensorDeviceClass:
        DURATION = "duration"
        TIMESTAMP = "timestamp"

    class SensorStateClass:
        MEASUREMENT = "measurement"

    class SensorEntity:
        pass

    sensmod.SensorDeviceClass = SensorDeviceClass
    sensmod.SensorStateClass = SensorStateClass
    sensmod.SensorEntity = SensorEntity

    switchmod = _make_pkg("homeassistant.components.switch")

    class SwitchEntity:
        pass

    switchmod.SwitchEntity = SwitchEntity

    # config_entries: minimal flow bases + ConfigEntry marker.
    entries = _make_pkg("homeassistant.config_entries")
    entries.ConfigEntry = object

    class _Flow:
        domain: str
        hass: Any = None

        def __init_subclass__(cls, **kwargs: Any) -> None:
            super().__init_subclass__()
            if "domain" in kwargs:
                cls.domain = kwargs["domain"]

        def async_abort(self, **kw: Any) -> dict[str, Any]:
            return {"type": "abort", **kw}

        def async_create_entry(self, **kw: Any) -> dict[str, Any]:
            return {"type": "create_entry", **kw}

        def async_show_form(self, **kw: Any) -> dict[str, Any]:
            return {"type": "form", **kw}

        def async_show_menu(self, **kw: Any) -> dict[str, Any]:
            return {"type": "menu", "step_id": None, **kw}

        def _async_current_entries(self) -> list[Any]:
            raise NotImplementedError  # overridden per-test

    entries.ConfigFlow = _Flow
    entries.OptionsFlow = _Flow

    # helpers
    dr = _make_pkg("homeassistant.helpers.device_registry")

    class DeviceInfo:
        def __init__(self, **kwargs: Any) -> None:
            self.info = kwargs

    dr.DeviceInfo = DeviceInfo

    ent = _make_pkg("homeassistant.helpers.entity")

    class Entity:
        _attr_has_entity_name = False
        _attr_should_poll = True
        _writes = 0

        async def async_added_to_hass(self) -> None:
            pass

        def async_write_ha_state(self) -> None:
            self.__dict__["_writes"] = self.__dict__.get("_writes", 0) + 1

    ent.Entity = Entity

    import voluptuous as vol

    sel = _make_pkg("homeassistant.helpers.selector")

    class _Selector:
        def __init__(self, config: Any) -> None:
            self.config = config

        def __repr__(self) -> str:
            return f"selector({self.config!r})"

    class _Config:
        def __init__(self, **kwargs: Any) -> None:
            for key, value in kwargs.items():
                setattr(self, key, value)

    for _name in ("NumberSelector", "SelectSelector", "EntitySelector", "TextSelector"):
        setattr(sel, _name, _Selector)
    for _name in (
        "NumberSelectorConfig",
        "SelectSelectorConfig",
        "EntitySelectorConfig",
        "TextSelectorConfig",
    ):
        setattr(sel, _name, _Config)


_INSTALLED = False


def _ensure_stubs() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _make_pkg("voluptuous")
    import voluptuous as vol

    class _KeySpec:
        """vol.Required/Optional stand-in that keeps the default visible.

        The real library returns a schema key carrying its default; the flow
        manager applies defaults during validation. Tests assert on them
        (``schema.defaults``), so a regression that drops a default fails.
        """

        __slots__ = ("key", "required", "has_default", "default")

        def __init__(self, key, required, has_default, default) -> None:
            self.key = key
            self.required = required
            self.has_default = has_default
            self.default = default

        def __eq__(self, other: object) -> bool:
            if isinstance(other, _KeySpec):
                return self.key == other.key
            return self.key == other

        def __hash__(self) -> int:
            return hash(self.key)

        def __repr__(self) -> str:
            return f"vol_key({self.key!r})"

    _MISSING = object()

    class _SchemaStub:
        def __init__(self, schema: dict[Any, Any]) -> None:
            cleaned: dict[Any, Any] = {}
            defaults: dict[Any, Any] = {}
            for spec, config in schema.items():
                cleaned[spec.key] = config
                if spec.has_default:
                    defaults[spec.key] = spec.default
            self.schema = cleaned
            self.defaults = defaults

    vol.Schema = _SchemaStub
    vol.Required = lambda key, **kw: _KeySpec(
        key, True, "default" in kw, kw.get("default", _MISSING)
    )
    vol.Optional = lambda key, **kw: _KeySpec(
        key, False, "default" in kw, kw.get("default", _MISSING)
    )
    vol.Coerce = lambda f: f
    _install_ha_stubs()
    _INSTALLED = True


_ensure_stubs()


# --- shared runtime registries the component binds into ---
class TimerHandle:
    def __init__(self, delay: float, action: Callable[..., Any]) -> None:
        self.delay = delay
        self.action = action
        self.cancelled = False
        self.fired = False

    def cancel(self) -> None:
        self.cancelled = True


class TimerRegistry:
    """async_call_later stand-in; timers are fired explicitly by tests."""

    def __init__(self) -> None:
        self.handles: list[TimerHandle] = []

    def __call__(self, hass: Any, delay: float, action: Callable[..., Any]) -> Any:
        handle = TimerHandle(delay, action)
        self.handles.append(handle)
        return handle.cancel

    def __len__(self) -> int:
        return len(self.handles)

    def clear(self) -> None:
        self.handles.clear()

    def last(self) -> TimerHandle:
        return self.handles[-1]

    async def fire(self, handle: TimerHandle) -> TimerHandle:
        handle.fired = True
        await handle.action(datetime.now(timezone.utc))
        return handle

    async def fire_last(self) -> TimerHandle:
        return await self.fire(self.handles[-1])


class TrackRegistry:
    """async_track_state_change_event stand-in."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, tuple[str, ...], Any]] = []
        self.unsubscribed = 0

    def track(self, hass: Any, entity_ids: list[str], handler: Any) -> Callable[[], None]:
        self.calls.append((hass, tuple(entity_ids), handler))
        return self._unsub

    def _unsub(self) -> None:
        self.unsubscribed += 1

    def clear(self) -> None:
        self.calls.clear()
        self.unsubscribed = 0

    def last_handler(self) -> Any:
        return self.calls[-1][2]


timers = TimerRegistry()
tracker = TrackRegistry()
event_helpers = sys.modules["homeassistant.helpers.event"]
event_helpers.async_call_later = timers
event_helpers.async_track_state_change_event = tracker.track

notifications: list[tuple[str, str, str | None]] = []
_pn = sys.modules["homeassistant.components.persistent_notification"]


def _notify_fallback(hass: Any, message: str, title: str, notification_id: str | None = None) -> None:
    notifications.append((message, title, notification_id))


_pn.async_create = _notify_fallback


# --- fakes for tests to wire into the hub / flows ---
class FakeState:
    def __init__(self, state: str, name: str | None = None) -> None:
        self.state = state
        # Real HA State objects carry the friendly name; notifications use it
        # in place of the entity id.
        self.name = name


class StateRegistry:
    def __init__(self) -> None:
        self._states: dict[str, FakeState] = {}

    def set(self, entity_id: str, state: str | None, name: str | None = None) -> None:
        self._states[entity_id] = FakeState(state, name)

    def get(self, entity_id: str) -> FakeState | None:
        return self._states.get(entity_id)


class FakeServices:
    def __init__(self, services: dict[str, dict[str, bool]] | None = None) -> None:
        self._services = services or {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def set(self, domain: str, name: str) -> None:
        self._services.setdefault(domain, {})[name] = True

    def async_services(self) -> dict[str, dict[str, bool]]:
        return {domain: dict(names) for domain, names in self._services.items()}

    def has_service(self, domain: str, name: str) -> bool:
        return name in self._services.get(domain, {})

    async def async_call(self, domain: str, service: str, service_data: dict[str, Any]) -> None:
        self.calls.append((domain, service, service_data))


class ConfigEntries:
    def __init__(self) -> None:
        self.forwarded: list[Any] = []
        self.unloaded: list[Any] = []
        self.reloaded: list[str] = []
        self.update_listeners: list[Callable[[Any, Any], Any]] = []

    async def async_forward_entry_setups(self, entry: Any, platforms: list[str]) -> None:
        self.forwarded.append((entry, platforms))

    async def async_unload_platforms(self, entry: Any, platforms: list[str]) -> bool:
        self.unloaded.append((entry, platforms))
        return True

    async def async_reload(self, entry_id: str) -> None:
        self.reloaded.append(entry_id)


class FakeHass:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.bus = FakeBus()
        self.services = FakeServices()
        self.states = StateRegistry()
        self.config_entries = ConfigEntries()


class FakeBus:
    def __init__(self) -> None:
        self.fired: list[tuple[str, dict[str, Any] | None]] = []

    def async_fire(self, event_type: str, event_data: dict[str, Any] | None = None) -> None:
        self.fired.append((event_type, event_data))


def make_entry(entry_id: str = "e1", **options: Any) -> Any:
    listeners: list[Callable[[Any, Any], Any]] = []
    on_unload: list[Callable[[], Any]] = []

    # Defaults mirror the hub's intended behavior so tests stay focused:
    # the water-meter entity is set and notifications actually go out.
    final_options = {"water_meter": "sensor.meter", "notify_service": "notify.telegram"}
    final_options.update(options)

    def _add_listener(cb: Callable[[Any, Any], Any]) -> Any:
        listeners.append(cb)

        def _unreg() -> None:
            listeners.remove(cb)

        return _unreg

    def _on_unload(cb: Callable[[], Any]) -> None:
        on_unload.append(cb)

    return type(
        "Entry",
        (object,),
        {
            "entry_id": entry_id,
            "options": final_options,
            "listeners": listeners,
            "on_unload": on_unload,
            "add_update_listener": staticmethod(_add_listener),
            "async_on_unload": staticmethod(_on_unload),
        },
    )()


def make_hub(hass: FakeHass | None = None, **options: Any) -> Any:
    """Build a WaterLeakHub with a fresh fake hass/entry. Loads no state."""
    from custom_components.water_leak_meter import WaterLeakHub

    if hass is None:
        hass = FakeHass()
    hub = WaterLeakHub(hass, make_entry(**options))
    return hub


# --- fixtures --------------------------------------------------------------
@pytest.fixture(autouse=True)
def _reset_shared_state():
    timers.clear()
    tracker.clear()
    notifications.clear()
    yield


@pytest.fixture
def wl():
    from custom_components import water_leak_meter

    return water_leak_meter


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now


@pytest.fixture
def clock(wl, monkeypatch):
    c = Clock()
    monkeypatch.setattr(wl, "time", c)
    return c