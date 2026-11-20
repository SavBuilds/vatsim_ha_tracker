"""Stand-ins for Home Assistant, aiohttp and voluptuous, enough to import the integration and
drive its setup on a plain PC. They are written from memory of the Home Assistant API. Passing
with these shows the integration is consistent with itself, NOT that it is correct against a
real Home Assistant. Only loading it in Home Assistant shows that."""
import asyncio
import enum
import sys
import types


def _module(name, **members):
    module = types.ModuleType(name)
    module.__dict__.update(members)
    sys.modules[name] = module
    parent, _, child = name.rpartition(".")
    if parent:
        setattr(sys.modules[parent], child, module)
    return module


class Entity:
    hass = None
    _attr_should_poll = True

    def __init__(self):
        pass

    async def async_added_to_hass(self):
        pass

    def async_on_remove(self, func):
        self.__dict__.setdefault("_removers", []).append(func)

    def async_write_ha_state(self):
        assert asyncio.get_running_loop() is self.hass.loop, "state written off the loop"
        self.__dict__["writes"] = self.__dict__.get("writes", 0) + 1


class RestoreEntity(Entity):
    last_state = None

    async def async_get_last_state(self):
        return self.last_state


class ConfigFlow:
    def __init_subclass__(cls, domain=None, **kwargs):
        super().__init_subclass__(**kwargs)
        cls.domain = domain


class ConfigEntry:
    def __init__(self, entry_id, data, options=None):
        self.entry_id, self.data, self.options = entry_id, data, options or {}
        self.runtime_data = None
        self.unload_callbacks = []

    def async_on_unload(self, func):
        self.unload_callbacks.append(func)

    def add_update_listener(self, listener):
        self.update_listener = listener
        return lambda: None


class _Dispatcher:
    def __init__(self):
        self.targets = {}

    def connect(self, hass, signal, target):
        self.targets.setdefault(signal, []).append(target)
        return lambda: self.targets[signal].remove(target)

    def send(self, hass, signal, *args):
        for target in list(self.targets.get(signal, ())):
            target(*args)


class Platform(str, enum.Enum):
    BINARY_SENSOR = "binary_sensor"
    DEVICE_TRACKER = "device_tracker"
    SENSOR = "sensor"
    TEXT = "text"


def install():
    if "homeassistant" in sys.modules:
        return sys.modules["homeassistant"].dispatcher_stub
    dispatcher = _Dispatcher()
    _module("aiohttp", ClientTimeout=lambda total=None: ("timeout", total))
    marker = lambda *a, **k: (a, tuple(sorted(k)))  # noqa: E731
    _module("voluptuous", Schema=lambda schema: schema, Required=marker, Optional=marker, All=marker,
            Coerce=marker, Range=marker)
    ha = _module("homeassistant")
    ha.dispatcher_stub = dispatcher
    _module("homeassistant.config_entries", ConfigEntry=ConfigEntry, ConfigFlow=ConfigFlow,
            ConfigFlowResult=dict, OptionsFlow=type("OptionsFlow", (), {}))
    _module("homeassistant.const", Platform=Platform, EVENT_HOMEASSISTANT_STOP="homeassistant_stop",
            STATE_UNKNOWN="unknown", STATE_UNAVAILABLE="unavailable",
            EntityCategory=enum.Enum("EntityCategory", "CONFIG DIAGNOSTIC"))
    _module("homeassistant.core", HomeAssistant=object, Event=object, callback=lambda f: f)
    _module("homeassistant.exceptions", ConfigEntryError=type("ConfigEntryError", (Exception,), {}))
    _module("homeassistant.helpers")
    _module("homeassistant.helpers.aiohttp_client", async_get_clientsession=lambda hass: hass.session)
    _module("homeassistant.helpers.dispatcher", async_dispatcher_connect=dispatcher.connect,
            async_dispatcher_send=dispatcher.send)
    _module("homeassistant.helpers.device_registry", DeviceInfo=dict,
            DeviceEntryType=enum.Enum("DeviceEntryType", "SERVICE"))
    _module("homeassistant.helpers.entity", Entity=Entity)
    _module("homeassistant.helpers.entity_platform", AddEntitiesCallback=object)
    _module("homeassistant.helpers.restore_state", RestoreEntity=RestoreEntity)
    _module("homeassistant.components")
    _module("homeassistant.components.sensor", SensorEntity=type("SensorEntity", (Entity,), {}))
    _module("homeassistant.components.binary_sensor",
            BinarySensorEntity=type("BinarySensorEntity", (Entity,), {}),
            BinarySensorDeviceClass=enum.Enum("BinarySensorDeviceClass", "CONNECTIVITY"))
    # Home Assistant 2026.11 warns that the config_entry submodule alias goes away in 2027.6.
    _module("homeassistant.components.device_tracker", SourceType=enum.Enum("SourceType", "GPS"),
            TrackerEntity=type("TrackerEntity", (Entity,), {}))
    _module("homeassistant.components.text", TextEntity=type("TextEntity", (Entity,), {}),
            TextMode=enum.Enum("TextMode", "TEXT"))
    return dispatcher
