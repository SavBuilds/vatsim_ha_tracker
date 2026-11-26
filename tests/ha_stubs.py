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
        self.background_tasks = set()

    def async_on_unload(self, func):
        self.unload_callbacks.append(func)

    def async_create_background_task(self, hass, target, name, eager_start=True):
        # Tracked by hass.tasks as well, so a test can wait for it with hass.settle().
        task = hass.async_create_task(target, name)
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)
        return task

    async def run_unload(self):
        """What Home Assistant does after async_unload_entry (config_entries.py,
        _async_process_on_unload, 2026.11.3): the unload callbacks, last added first, then
        every background task of the entry is cancelled."""
        while self.unload_callbacks:
            job = self.unload_callbacks.pop()()
            if asyncio.iscoroutine(job):
                await job
        tasks = list(self.background_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

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


class ServiceNotFound(Exception):
    """homeassistant.exceptions.ServiceNotFound: raised for an action that is not registered."""

    def __init__(self, domain, service):
        super().__init__(f"Action {domain}.{service} not found")


class ServiceDataInvalid(Exception):
    """What Home Assistant raises when the data does not pass the action's schema."""


_TARGET = {"entity_id", "device_id", "area_id", "floor_id", "label_id"}
# (required, optional) keys, from the schemas in Home Assistant 2026.11.3: notify/const.py
# NOTIFY_SERVICE_SCHEMA (legacy notify actions), notify/services.py (send_message, an entity
# action), tts/services.py (speak, an entity action), persistent_notification/services.py.
_SCHEMAS = {
    ("notify", "send_message"): ({"message"}, {"title"} | _TARGET),
    ("tts", "speak"): ({"media_player_entity_id", "message"}, {"cache", "language", "options"} | _TARGET),
    ("persistent_notification", "create"): ({"message"}, {"title", "notification_id"}),
    ("persistent_notification", "dismiss"): ({"notification_id"}, set()),
}
_LEGACY_NOTIFY = ({"message"}, {"title", "target", "data"})


class Services:
    """Records calls. An action that is not registered or data its schema would refuse is
    refused, as Home Assistant refuses it, and not recorded. A (domain, service) put in
    .failing is recorded and then raises, as an action that fails while it runs would."""

    def __init__(self, notify=("persistent_notification", "send_message", "notify")):
        self.calls, self.failing, self.notify = [], {}, list(notify)

    def _schema(self, domain, service):
        if domain == "notify" and service in self.notify:
            return _SCHEMAS.get((domain, service), _LEGACY_NOTIFY)
        return _SCHEMAS.get((domain, service))

    async def async_call(self, domain, service, service_data=None, blocking=False, context=None, target=None,
                         return_response=False):
        schema = self._schema(domain, service)
        if schema is None:
            raise ServiceNotFound(domain, service)
        data = dict(service_data or {})
        required, optional = schema
        if not required <= set(data) or set(data) - required - optional:
            raise ServiceDataInvalid(f"{domain}.{service}: {sorted(data)}")
        for key in ("message", "title"):
            if key in data and not isinstance(data[key], str):
                raise ServiceDataInvalid(f"{domain}.{service}: {key} is not text")
        self.calls.append((domain, service, data))
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
    _module("homeassistant.exceptions", ConfigEntryError=type("ConfigEntryError", (Exception,), {}),
            ServiceNotFound=ServiceNotFound)
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

    # helpers/start.py: run now if Home Assistant has started, else when it has.
    def at_started(hass, action):
        assert runs_on_the_loop(action), f"{action!r} would run in a worker thread"
        if hass.started:
            action(hass)
            return lambda: None
        hass.at_started.append(action)
        return lambda: hass.at_started.remove(action) if action in hass.at_started else None

    _module("homeassistant.helpers.start", async_at_started=at_started)

    class Store:
        """helpers/storage.py Store, kept in hass.storage so a second reporter on the same hass
        (a reload or a restart) reads what the first wrote. async_delay_save writes at once."""

        def __init__(self, hass, version, key, private=False, *, atomic_writes=False, encoder=None,
                     minor_version=1, read_only=False, serialize_in_event_loop=True, max_readable_version=None):
            assert isinstance(version, int) and isinstance(key, str) and key
            self.hass, self.key = hass, key

        async def async_load(self):
            if self.key in self.hass.storage_broken:
                raise ValueError("unreadable")
            return self.hass.storage.get(self.key)

        async def async_save(self, data):
            self.hass.storage[self.key] = data

        def async_delay_save(self, data_func, delay=0):
            self.hass.storage[self.key] = data_func()

        async def async_remove(self):
            self.hass.storage.pop(self.key, None)

    _module("homeassistant.helpers.storage", Store=Store)

    select_modes = enum.Enum("SelectSelectorMode", {"LIST": "list", "DROPDOWN": "dropdown"})

    # Selector configs are TypedDicts in Home Assistant: a wrong key is refused there, and here.
    # A select's mode is checked against the enum, as its schema does (helpers/selector.py).
    def typed(name, *keys):
        def make(**config):
            unknown = set(config) - set(keys)
            assert not unknown, f"{name} has no {sorted(unknown)}"
            if "mode" in config and name == "select":
                mode = config["mode"]
                assert mode in select_modes or mode in [m.value for m in select_modes], f"no select mode {mode!r}"
            return {"selector": name, **config}
        return make

    _module("homeassistant.helpers.selector",
            EntitySelectorConfig=typed("entity", "exclude_entities", "include_entities", "multiple", "reorder",
                                       "filter", "integration", "domain", "device_class", "read_only"),
            SelectSelectorConfig=typed("select", "options", "multiple", "custom_value", "mode",
                                       "translation_key", "sort", "read_only"),
            SelectOptionDict=lambda *, value, label: {"value": value, "label": label},
            SelectSelectorMode=select_modes,
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
