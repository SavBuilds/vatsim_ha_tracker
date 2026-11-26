"""Stand-ins for Home Assistant, aiohttp and voluptuous, enough to import the integration and
drive its setup on a plain PC. They are written from memory of the Home Assistant API. Passing
with these shows the integration is consistent with itself, NOT that it is correct against a
real Home Assistant. Only loading it in Home Assistant shows that."""
import asyncio
import enum
import functools
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


def mark_callback(func):
    """Home Assistant's @callback: it only marks the function."""
    setattr(func, "_hass_callback", True)
    return func


def runs_on_the_loop(target):
    """Home Assistant runs a job on the event loop only if it is a coroutine function or is
    marked @callback, looking through functools.partial. Anything else goes to a worker thread."""
    while isinstance(target, functools.partial):
        target = target.func
    return asyncio.iscoroutinefunction(target) or getattr(target, "_hass_callback", False)


class _Dispatcher:
    def __init__(self):
        self.targets = {}

    def connect(self, hass, signal, target):
        assert runs_on_the_loop(target), f"{target!r} would run in a worker thread"
        self.targets.setdefault(signal, []).append(target)
        return lambda: self.targets[signal].remove(target)

    def send(self, hass, signal, *args):
        for target in list(self.targets.get(signal, ())):
            target(*args)


class Platform(str, enum.Enum):
    BINARY_SENSOR = "binary_sensor"
    BUTTON = "button"
    DEVICE_TRACKER = "device_tracker"
    SELECT = "select"
    SENSOR = "sensor"
    SWITCH = "switch"
    TEXT = "text"


class OptionsFlow:
    """The three results an options flow can return, as plain dicts. config_entry and hass are
    set by the test, as Home Assistant sets them."""

    def async_show_menu(self, *, step_id=None, menu_options, sort=False, description_placeholders=None):
        return {"type": "menu", "step_id": step_id, "menu_options": list(menu_options)}

    def async_show_form(self, *, step_id=None, data_schema=None, errors=None, description_placeholders=None,
                        last_step=None, preview=None):
        return {"type": "form", "step_id": step_id, "data_schema": data_schema, "errors": errors or {}}

    def async_create_entry(self, *, title=None, data=None, description=None, description_placeholders=None):
        return {"type": "create_entry", "data": data}


class State:
    def __init__(self, entity_id, state, name=None):
        self.entity_id, self.state = entity_id, state
        self.name = name or entity_id.partition(".")[2].replace("_", " ").title()


class States:
    def __init__(self):
        self.all = {}

    def set(self, entity_id, state, name=None):
        self.all[entity_id] = State(entity_id, state, name)

    def get(self, entity_id):
        return self.all.get(entity_id)

    def async_all(self, domain_filter=None):
        return [s for s in self.all.values() if domain_filter is None or s.entity_id.split(".")[0] == domain_filter]


class Services:
    """Records calls. A (domain, service) put in .failing raises, as a missing or broken action would."""

    def __init__(self, notify=("persistent_notification", "send_message", "notify")):
        self.calls, self.failing, self.notify = [], {}, list(notify)

    async def async_call(self, domain, service, service_data=None, blocking=False, context=None, target=None,
                         return_response=False):
        self.calls.append((domain, service, dict(service_data or {})))
        if (domain, service) in self.failing:
            raise self.failing[(domain, service)]

    def async_services_for_domain(self, domain):
        return {name: object() for name in self.notify} if domain == "notify" else {}


class RegistryEntry:
    def __init__(self, entity_id, unique_id):
        self.entity_id, self.unique_id, self.domain = entity_id, unique_id, entity_id.split(".")[0]


class EntityRegistry:
    def __init__(self):
        self.entries, self.removed = {}, []

    def async_remove(self, entity_id):
        self.removed.append(entity_id)
        self.entries.pop(entity_id, None)


def install():
    if "homeassistant" in sys.modules:
        return sys.modules["homeassistant"].dispatcher_stub
    dispatcher = _Dispatcher()
    _module("aiohttp", ClientTimeout=lambda total=None: ("timeout", total))
    marker = lambda *a, **k: (a, tuple(sorted(k)))  # noqa: E731

    class Key(str):
        """A schema key that keeps its default, so a test can read what a form would show."""
        def __new__(cls, name, default=None, description=None):
            key = super().__new__(cls, name)
            key.default, key.description = default, description
            return key
    _module("voluptuous", Schema=lambda schema: schema, Required=Key, Optional=Key, All=marker,
            Coerce=marker, Range=marker)
    ha = _module("homeassistant")
    ha.dispatcher_stub = dispatcher
    _module("homeassistant.config_entries", ConfigEntry=ConfigEntry, ConfigFlow=ConfigFlow,
            ConfigFlowResult=dict, OptionsFlow=OptionsFlow)
    _module("homeassistant.const", Platform=Platform, EVENT_HOMEASSISTANT_STOP="homeassistant_stop",
            STATE_UNKNOWN="unknown", STATE_UNAVAILABLE="unavailable",
            EntityCategory=enum.Enum("EntityCategory", "CONFIG DIAGNOSTIC"))
    _module("homeassistant.core", HomeAssistant=object, Event=object, callback=mark_callback)
    _module("homeassistant.exceptions", ConfigEntryError=type("ConfigEntryError", (Exception,), {}))
    _module("homeassistant.helpers")
    _module("homeassistant.helpers.aiohttp_client", async_get_clientsession=lambda hass: hass.session)
    _module("homeassistant.helpers.dispatcher", async_dispatcher_connect=dispatcher.connect,
            async_dispatcher_send=dispatcher.send)
    _module("homeassistant.helpers.device_registry", DeviceInfo=dict,
            DeviceEntryType=enum.Enum("DeviceEntryType", "SERVICE"))
    _module("homeassistant.helpers.entity", Entity=Entity)

    # The keyword list is Home Assistant's own (helpers/issue_registry.py, 2026.10): a wrong or
    # missing argument fails here as it would there.
    def create_issue(hass, domain, issue_id, *, breaks_in_ha_version=None, data=None, is_fixable,
                     is_persistent=False, issue_domain=None, learn_more_url=None, severity,
                     translation_key, translation_placeholders=None):
        assert asyncio.get_running_loop() is hass.loop, "issue raised off the loop"
        assert isinstance(is_fixable, bool) and isinstance(is_persistent, bool)
        assert isinstance(severity, sys.modules["homeassistant.helpers.issue_registry"].IssueSeverity)
        assert isinstance(translation_key, str) and translation_key
        placeholders = translation_placeholders
        assert placeholders is None or all(isinstance(v, str) for v in placeholders.values())
        hass.issue_events.append(("update" if (domain, issue_id) in hass.issues else "create", issue_id))
        hass.issues[(domain, issue_id)] = {
            "is_fixable": is_fixable, "is_persistent": is_persistent, "severity": severity,
            "translation_key": translation_key, "translation_placeholders": translation_placeholders,
            "learn_more_url": learn_more_url}

    def delete_issue(hass, domain, issue_id):
        assert asyncio.get_running_loop() is hass.loop, "issue cleared off the loop"
        if hass.issues.pop((domain, issue_id), None) is not None:
            hass.issue_events.append(("remove", issue_id))

    _module("homeassistant.helpers.issue_registry", async_create_issue=create_issue,
            async_delete_issue=delete_issue, IssueSeverity=enum.Enum("IssueSeverity", "CRITICAL ERROR WARNING"))

    def track_interval(hass, action, interval):
        assert runs_on_the_loop(action), f"{action!r} would run in a worker thread"
        hass.intervals.append((action, interval))
        return lambda: hass.intervals.remove((action, interval))

    def call_later(hass, delay, action):
        assert runs_on_the_loop(action), f"{action!r} would run in a worker thread"
        entry = (delay, action)
        hass.later.append(entry)
        return lambda: hass.later.remove(entry) if entry in hass.later else None

    _module("homeassistant.helpers.event", async_track_time_interval=track_interval, async_call_later=call_later)

    # Selector configs are TypedDicts in Home Assistant: a wrong key is refused there, and here.
    def typed(name, *keys):
        def make(**config):
            unknown = set(config) - set(keys)
            assert not unknown, f"{name} has no {sorted(unknown)}"
            return {"selector": name, **config}
        return make

    _module("homeassistant.helpers.selector",
            EntitySelectorConfig=typed("entity", "exclude_entities", "include_entities", "multiple", "reorder",
                                       "filter", "integration", "domain", "device_class", "read_only"),
            SelectSelectorConfig=typed("select", "options", "multiple", "custom_value", "mode",
                                       "translation_key", "sort", "read_only"),
            SelectOptionDict=lambda *, value, label: {"value": value, "label": label},
            SelectSelectorMode=enum.Enum("SelectSelectorMode", "LIST DROPDOWN"),
            EntitySelector=lambda config=None: config, SelectSelector=lambda config=None: config,
            BooleanSelector=lambda config=None: {"selector": "boolean"})
    _module("homeassistant.helpers.entity_registry",
            async_get=lambda hass: hass.entity_registry,
            async_entries_for_config_entry=lambda registry, entry_id: [
                e for e in registry.entries.values() if e.unique_id.startswith(entry_id + "_")])
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
    _module("homeassistant.components.select", SelectEntity=type("SelectEntity", (Entity,), {}))
    _module("homeassistant.components.switch", SwitchEntity=type("SwitchEntity", (Entity,), {}))
    _module("homeassistant.components.button", ButtonEntity=type("ButtonEntity", (Entity,), {}))
    return dispatcher
