"""Unit tests for the config flow (setup + options)."""

from types import SimpleNamespace as NS

import pytest

from conftest import FakeHass, FakeServices, make_entry

import custom_components.water_leak_meter.config_flow as flow
from custom_components.water_leak_meter.const import (
    CONF_NOTIFY_DATA,
    CONF_NOTIFY_SERVICE,
    CONF_PULSE_FT3,
    CONF_QUIET_MIN,
    CONF_WATER_METER,
    DEFAULT_NOTIFY_SERVICE,
    DOMAIN,
    TELEGRAM_BOT_SERVICE,
)


def _hass_with(services: dict[str, dict[str, bool]]) -> FakeHass:
    hass = FakeHass()
    hass.services = FakeServices(services)
    return hass


# --- selection helpers ------------------------------------------------------

def test_notify_services_filters_ambiguous():
    hass = _hass_with({"notify": {"telegram": True, "mobile_app_iphone": True, "notify": True, "send_message": True}})
    assert flow._notify_services(hass) == ["notify.mobile_app_iphone", "notify.telegram"]


def test_notify_services_includes_telegram_bot():
    hass = _hass_with({"notify": {"phone": True}, "telegram_bot": {"send_message": True}})
    assert flow._notify_services(hass) == ["notify.phone", "telegram_bot.send_message"]


def test_notify_services_empty():
    assert flow._notify_services(_hass_with({})) == []


def test_notify_default_preserves_explicit_value():
    hass = _hass_with({"notify": {"phone": True}})
    assert flow._notify_default(hass, "notify.old") == "notify.old"
    assert flow._notify_default(hass, "") == ""


def test_notify_default_prefers_telegram():
    hass = _hass_with({"notify": {"phone": True, "telegram": True}})
    assert flow._notify_default(hass) == DEFAULT_NOTIFY_SERVICE


def test_notify_default_falls_back_to_telegram_bot():
    hass = _hass_with({"notify": {"phone": True}, "telegram_bot": {"send_message": True}})
    assert flow._notify_default(hass) == TELEGRAM_BOT_SERVICE


def test_notify_default_first_service_or_empty():
    hass = _hass_with({"notify": {"phone": True}})
    assert flow._notify_default(hass) == "notify.phone"
    assert flow._notify_default(_hass_with({})) == ""


def test_notify_selector_options():
    hass = _hass_with({"notify": {"phone": True}})
    sel = flow._notify_selector(hass)
    assert sel.config.options == ["notify.phone"]
    assert sel.config.mode == "dropdown"
    assert sel.config.custom_value is True


def test_number_selector_config():
    sel = flow._number_selector(5, 240, "min", 1)
    c = sel.config
    assert (c.min, c.max, c.step, c.unit_of_measurement, c.mode) == (5, 240, 1, "min", "box")


# --- schemas ---------------------------------------------------------------

def test_user_schema_keys_and_selectors():
    hass = _hass_with({"notify": {"telegram": True}})
    schema = flow._user_schema(hass).schema
    for key in (CONF_WATER_METER, CONF_QUIET_MIN, CONF_NOTIFY_DATA):
        assert key in schema
    assert schema[CONF_PULSE_FT3].config.step == 0.1
    assert schema[CONF_PULSE_FT3].config.unit_of_measurement == "ft³"


def test_option_schema_from_empty_options():
    schema = flow._option_schema(_hass_with({}), {}).schema
    assert CONF_WATER_METER in schema
    assert CONF_NOTIFY_DATA in schema
    assert schema[CONF_QUIET_MIN].config.min == 5


def test_option_schema_uses_persisted_options():
    hass = _hass_with({"notify": {"telegram": True}})
    schema = flow._option_schema(
        hass,
        {
            "water_meter": "sensor.water",
            "quiet_min": "30",
            "limit_min": "60",
            "pulse_ft3": "1.5",
            "stale_min": "0",
            "notify_service": "notify.telegram",
        },
    ).schema
    assert schema[CONF_QUIET_MIN].config.min == 5  # selector unchanged
    assert schema[CONF_NOTIFY_SERVICE] is not None


# --- _validate --------------------------------------------------------------

async def test_validate_branches():
    ok = _hass_with({"notify": {"telegram": True}, "telegram_bot": {"send_message": True}})
    assert await flow._validate(ok, {"notify_service": ""}) is None
    assert await flow._validate(ok, {"notify_service": "notify.telegram"}) is None
    assert await flow._validate(ok, {"notify_service": "telegram_bot.send_message"}) is None
    assert await flow._validate(ok, {}) is None
    assert await flow._validate(ok, {"notify_service": "notify.ghost"}) == "invalid_notify"
    assert await flow._validate(ok, {"notify_service": "telegram"}) == "invalid_notify"
    assert await flow._validate(ok, {"notify_service": "other.service"}) == "invalid_notify"
    assert await flow._validate(ok, {"notify_service": "telegram_bot.send_photo"}) == "invalid_notify"
    no_bot = _hass_with({"notify": {"telegram": True}})
    assert await flow._validate(no_bot, {"notify_service": "telegram_bot.send_message"}) == "invalid_notify"
    assert await flow._validate(ok, {"notify_service": "notify.telegram", "notify_data": '{"chat_id": 9}'}) is None
    assert await flow._validate(ok, {"notify_service": "notify.telegram", "notify_data": "oops"}) == "invalid_notify_data"
    assert await flow._validate(ok, {"notify_service": "notify.telegram", "notify_data": "[1]"}) == "invalid_notify_data"


# --- config flow -----------------------------------------------------------

def test_config_flow_version():
    assert flow.WaterLeakConfigFlow.VERSION == 1


async def test_config_flow_initial_form():
    cfg = flow.WaterLeakConfigFlow()
    cfg.hass = _hass_with({})
    result = await cfg.async_step_user()
    assert result["type"] == "form"
    assert result["step_id"] == "user"
    assert result["errors"] == {}


async def test_config_flow_creates_entry():
    cfg = flow.WaterLeakConfigFlow()
    cfg.hass = _hass_with({"notify": {"telegram": True}})
    cfg._async_current_entries = lambda: [make_entry(water_meter="sensor.other")]
    user_input = {"water_meter": "sensor.water", "notify_service": "notify.telegram"}
    result = await cfg.async_step_user(user_input)
    assert result["type"] == "create_entry"
    assert result["data"] == {}
    assert result["options"] == user_input
    assert result["title"] == "Water Leak Detection for Meters"


async def test_config_flow_aborts_on_duplicate_meter():
    cfg = flow.WaterLeakConfigFlow()
    cfg.hass = _hass_with({})
    cfg._async_current_entries = lambda: [make_entry(water_meter="sensor.water")]
    user_input = {"water_meter": "sensor.water", "notify_service": ""}
    result = await cfg.async_step_user(user_input)
    assert result == {"type": "abort", "reason": "already_configured"}


async def test_config_flow_reports_notify_errors():
    cfg = flow.WaterLeakConfigFlow()
    cfg.hass = _hass_with({})
    cfg._async_current_entries = lambda: []
    result = await cfg.async_step_user({"water_meter": "sensor.water", "notify_service": "notify.ghost"})
    assert result["type"] == "form"
    assert result["errors"] == {CONF_NOTIFY_SERVICE: "invalid_notify"}
    result = await cfg.async_step_user(
        {"water_meter": "sensor.water", "notify_service": "", "notify_data": "nope"}
    )
    assert result["type"] == "form"
    assert result["errors"] == {CONF_NOTIFY_DATA: "invalid_notify_data"}


def test_async_get_options_flow():
    cfg = flow.WaterLeakConfigFlow()
    entry = make_entry()
    handler = cfg.async_get_options_flow(entry)
    assert isinstance(handler, flow.WaterLeakOptionsFlowHandler)
    assert handler._entry is entry


# --- options flow ----------------------------------------------------------

def _options_handler(hass: FakeHass, entry=None, **opts):
    entry = entry or make_entry(**opts)
    h = flow.WaterLeakOptionsFlowHandler(entry)
    h.hass = hass
    return h, entry


async def test_options_flow_init_menu():
    h, _ = _options_handler(FakeHass())
    result = await h.async_step_init()
    assert result["type"] == "menu"
    assert result["step_id"] == "init"
    assert result["menu_options"] == flow._OPTIONS_MENU
    assert result["menu_options"]["simulate_leak"] == "Simulate a leak"


def _sim_hub(**attrs):
    defaults = dict(quiet_min=5, limit_min=120, activity=40.0, leak_active=False, suppressed=False)
    defaults.update(attrs)

    async def _simulate_leak():
        defaults["sim_called"] = True
        return True

    hub = NS(**defaults)
    hub._simulate_leak = _simulate_leak
    return hub


async def test_simulate_menu_flow(hass_with_hub):
    h, hub = hass_with_hub
    res = await h.async_step_simulate_leak(None)
    assert res["type"] == "menu" and res["step_id"] == "init"
    msg = res["menu_options"]["init"]
    assert set(res["menu_options"]) == {"init"}
    assert "40" in msg and "120" in msg and "5" in msg
    assert "Leak Detected" in msg and "untouched" in msg
    assert hub._simulate_leak is not None
    # submitting again returns the ordinary options menu
    back = await h.async_step_simulate_leak({"submitted": True})
    assert set(back["menu_options"]) == set(flow._OPTIONS_MENU)


async def test_simulate_hub_missing():
    h, _ = _options_handler(FakeHass())
    res = await h.async_step_simulate_leak(None)
    assert "isn't loaded" in res["menu_options"]["init"]


async def test_simulate_refuses_when_suppressed():
    h, _ = _options_handler(FakeHass())
    h.hass.data[DOMAIN] = {"e1": _sim_hub(suppressed=True)}
    res = await h.async_step_simulate_leak(None)
    assert "suppressed" in res["menu_options"]["init"].lower()


async def test_simulate_refuses_when_already_leaking():
    h, _ = _options_handler(FakeHass())
    h.hass.data[DOMAIN] = {"e1": _sim_hub(leak_active=True)}
    res = await h.async_step_simulate_leak(None)
    assert "already active" in res["menu_options"]["init"]


async def test_simulate_returns_false_message():
    hub = _sim_hub()

    async def _simulate_leak():
        hub.sim_called = True
        return False

    hub._simulate_leak = _simulate_leak
    h, _ = _options_handler(FakeHass())
    h.hass.data[DOMAIN] = {"e1": hub}
    res = await h.async_step_simulate_leak(None)
    assert "didn't cross" in res["menu_options"]["init"]


async def test_simulate_exception_handled():
    hub = _sim_hub()

    async def _simulate_leak():
        raise RuntimeError("boom")

    hub._simulate_leak = _simulate_leak
    h, _ = _options_handler(FakeHass())
    h.hass.data[DOMAIN] = {"e1": hub}
    res = await h.async_step_simulate_leak(None)
    assert "didn't cross" in res["menu_options"]["init"]  # logged, never raised


def test_menu_message_helper():
    h, _ = _options_handler(FakeHass())
    res = h._menu_message("hello")
    assert res == {"type": "menu", "step_id": "init", "menu_options": {"init": "hello"}}


async def test_form_step_creates_entry():
    h, entry = _options_handler(_hass_with({"notify": {"telegram": True}}), notify_service="notify.telegram")
    user_input = {"water_meter": "sensor.new", "notify_service": "notify.telegram"}
    res = await h.async_step_form(user_input)
    assert res["type"] == "create_entry"
    assert res["data"] == user_input


async def test_form_step_shows_initial_form():
    h, _ = _options_handler(_hass_with({}))
    res = await h.async_step_form()
    assert res["type"] == "form" and res["step_id"] == "form"
    assert res["data_schema"] is not None


async def test_form_step_reports_errors():
    h, _ = _options_handler(_hass_with({}))
    res = await h.async_step_form({"water_meter": "sensor.x", "notify_service": "notify.ghost"})
    assert res["type"] == "form"
    assert res["errors"] == {CONF_NOTIFY_SERVICE: "invalid_notify"}
    res = await h.async_step_form({"water_meter": "sensor.x", "notify_service": "", "notify_data": "no"})
    assert res["type"] == "form"
    assert res["errors"] == {CONF_NOTIFY_DATA: "invalid_notify_data"}


class _TestHub:
    def __init__(self, notify_service, sent=None):
        self.notify_service = notify_service
        self.sent = sent if sent is not None else []

    async def _notify(self, title, message, notification_id):
        self.sent.append((notification_id, title))


async def test_send_test_happy_path():
    sent = []
    h, _ = _options_handler(FakeHass())
    h.hass.data[DOMAIN] = {"e1": _TestHub("notify.telegram", sent)}
    res = await h.async_step_send_test(None)
    assert res["type"] == "form"
    assert res["description_placeholders"] == {"notify": "notify.telegram"}
    assert sent == [("water_leak_test", "Test notification")]


async def test_send_test_telegram_bot_path():
    sent = []
    h, _ = _options_handler(FakeHass())
    h.hass.data[DOMAIN] = {"e1": _TestHub("telegram_bot.send_message", sent)}
    res = await h.async_step_send_test(None)
    assert res["description_placeholders"] == {"notify": "telegram_bot.send_message"}
    assert sent == [("water_leak_test", "Test notification")]


async def test_send_test_hub_missing():
    h, _ = _options_handler(FakeHass())
    res = await h.async_step_send_test(None)
    assert res["errors"] == {"base": "hub_unavailable"}


async def test_send_test_no_service():
    h, _ = _options_handler(FakeHass())
    h.hass.data[DOMAIN] = {"e1": _TestHub("")}
    res = await h.async_step_send_test(None)
    assert res["errors"] == {"base": "no_notify_service"}


async def test_send_test_ambiguous_services():
    for ambiguous in ("notify.notify", "notify.send_message"):
        h, _ = _options_handler(FakeHass())
        h.hass.data[DOMAIN] = {"e1": _TestHub(ambiguous)}
        res = await h.async_step_send_test(None)
        assert res["errors"] == {"base": "notify_ambiguous"}


async def test_send_test_submit_returns_menu():
    h, _ = _options_handler(FakeHass())
    res = await h.async_step_send_test({"submitted": True})
    assert set(res["menu_options"]) == set(flow._OPTIONS_MENU)


@pytest.fixture
def hass_with_hub():
    h, _ = _options_handler(FakeHass())
    hub = _sim_hub()
    h.hass.data[DOMAIN] = {"e1": hub}
    return h, hub