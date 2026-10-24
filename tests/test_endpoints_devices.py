"""Tests for the device-mapping page (endpoints/devices.py)."""

import time

import pytest
from endpoints import devices
from skill import device_mapping

from app import app

AUTH = {"Authorization": "Basic dGVzdC11c2VyOnRlc3QtcGFzc3dvcmQ="}


@pytest.fixture
def logs():
    previous = app.config.get("INTENT_LOGS", [])
    app.config["INTENT_LOGS"] = []
    yield app.config["INTENT_LOGS"]
    app.config["INTENT_LOGS"] = previous


def _payload(device_id, intent="AMAZON.NextIntent"):
    return {
        "context": {"System": {"device": {"deviceId": device_id}}},
        "request": {"type": "IntentRequest", "intent": {"name": intent}},
    }


def test_devices_page_empty(client):
    response = client.get("/devices", headers=AUTH)
    assert response.status_code == 200
    assert response.mimetype == "text/html"
    assert "No devices seen yet" in response.get_data(as_text=True)


def test_devices_page_fallback_keeps_mapping_forms(client, logs, monkeypatch):
    logs.append({"incoming": _payload("device-123"), "ts": time.time()})

    def broken_template(*args, **kwargs):
        raise RuntimeError("template unavailable")

    monkeypatch.setattr(devices, "render_template", broken_template)
    response = client.get("/devices", headers=AUTH)
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "<form method='POST' action='/devices'" in body
    assert "name='device_id' value='device-123'" in body
    assert "name='player_id'" in body


def test_devices_page_lists_seen_and_mapped_devices(client, logs):
    device_mapping.set_player_for_device("amzn1.ask.device.abcdef123456", "Studio")
    logs.append(
        {"incoming": _payload("amzn1.ask.device.abcdef123456"), "ts": time.time()}
    )
    body = client.get("/devices", headers=AUTH).get_data(as_text=True)
    assert "abcdef123456" in body  # last 12 chars of the device id
    assert "Studio" in body
    assert "AMAZON.NextIntent" in body
    assert "not seen this session" not in body


def test_devices_page_marks_unseen_mapped_device(client):
    device_mapping.set_player_for_device("amzn1.ask.device.onlymapped", "Kitchen")
    body = client.get("/devices", headers=AUTH).get_data(as_text=True)
    assert "onlymapped" in body
    assert "(not seen this session)" in body
    assert "(unknown)" in body


def test_devices_page_orders_by_last_seen(client, logs):
    logs.append({"incoming": _payload("device-old"), "ts": 1000.0})
    logs.append({"incoming": _payload("device-new"), "ts": 2000.0})
    body = client.get("/devices", headers=AUTH).get_data(as_text=True)
    assert body.index("device-new") < body.index("device-old")


def test_devices_save_and_clear_mapping(client):
    response = client.post(
        "/devices",
        headers=AUTH,
        data={"device_id": "device-7", "player_id": "Office"},
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/devices")
    assert device_mapping.get_player_for_device("device-7") == "Office"
    # Clearing the player id removes the mapping.
    client.post(
        "/devices", headers=AUTH, data={"device_id": "device-7", "player_id": ""}
    )
    assert device_mapping.get_player_for_device("device-7") is None


def test_devices_save_ignores_blank_device_id(client):
    client.post("/devices", headers=AUTH, data={"device_id": "  ", "player_id": "X"})
    assert device_mapping.load_mapping() == {}


def test_seen_devices_ignores_incomplete_entries(logs):
    logs.append({"incoming": None, "ts": 1.0})
    logs.append({"incoming": {"context": {}}, "ts": 2.0})
    logs.append(
        {
            "incoming": {
                "context": {"System": {"device": {"deviceId": "dev-a"}}},
                "request": {"type": "LaunchRequest"},
            },
            "ts": 3.0,
        }
    )
    with app.test_request_context("/"):
        seen = devices._seen_devices()
    assert list(seen) == ["dev-a"]
    assert seen["dev-a"]["last_request"] == "LaunchRequest"
    # A newer ts for the same device replaces the older record.
    logs.append({"incoming": _payload("dev-a", "AMAZON.HelpIntent"), "ts": 4.0})
    with app.test_request_context("/"):
        seen = devices._seen_devices()
    assert seen["dev-a"]["last_request"] == "AMAZON.HelpIntent"


def test_extract_helpers_and_format_ts():
    assert devices._extract_device_id({}) is None
    assert devices._extract_device_id({"context": "not-a-dict"}) is None
    assert (
        devices._extract_intent_name({"request": {"type": "SessionEndedRequest"}})
        == "SessionEndedRequest"
    )
    assert devices._extract_intent_name({"request": {"intent": "not-a-dict"}}) is None
    assert devices._extract_intent_name("not-a-dict") is None
    assert devices._format_ts(None) == "(unknown)"
    assert devices._format_ts("not-a-number") == "not-a-number"
    assert ":" in devices._format_ts(time.time())


def test_extract_device_id_with_broken_context(logs):
    # context exists but sub-objects are not dicts: AttributeError -> None
    assert devices._extract_device_id({"context": {"System": 5}}) is None


MA_PLAYERS = [
    {"player_id": "ma-kitchen", "name": "Kitchen"},
    {"player_id": "ma-studio", "name": "Studio speaker"},
]


@pytest.fixture
def ma_players(monkeypatch):
    """Music Assistant reachable: its player list is available to the page."""
    monkeypatch.setattr(devices.ma_control, "list_ma_players", lambda: MA_PLAYERS)
    return MA_PLAYERS


def test_devices_page_offers_player_dropdown(client, ma_players, logs):
    device_mapping.set_player_for_device("device-a", "ma-kitchen")
    logs.append({"incoming": _payload("device-a"), "ts": time.time()})
    body = client.get("/devices", headers=AUTH).get_data(as_text=True)
    assert '<select name="player_id">' in body
    assert '<option value="ma-kitchen" selected>Kitchen</option>' in body
    assert '<option value="ma-studio">Studio speaker</option>' in body
    # The card names the player, and the raw id input is gone.
    assert "Controls" in body and "Kitchen" in body
    assert 'placeholder="MA player_id"' not in body


def test_devices_page_keeps_unlisted_mapping_selected(client, ma_players):
    device_mapping.set_player_for_device("device-b", "ma-gone")
    body = client.get("/devices", headers=AUTH).get_data(as_text=True)
    assert (
        '<option value="ma-gone" selected>ma-gone (not in Music Assistant right now)'
        "</option>" in body
    )
    # The card still reports what the Echo controls instead of a bare id.
    assert "Controls" in body and "ma-gone" in body


def test_devices_page_falls_back_to_text_input_without_players(client):
    device_mapping.set_player_for_device("device-c", "Studio")
    body = client.get("/devices", headers=AUTH).get_data(as_text=True)
    assert '<input type="text" name="player_id" value="Studio"' in body
    assert '<select name="player_id">' not in body
    assert "unavailable" in body


def test_devices_page_truncates_the_device_id_line(client, logs):
    device_id = "amzn1.ask.device." + "a" * 64
    logs.append({"incoming": _payload(device_id), "ts": time.time()})
    body = client.get("/devices", headers=AUTH).get_data(as_text=True)
    assert 'class="ma-small ma-muted ma-deviceline"' in body
    # The full id stays reachable: hover title plus the details expandable.
    assert f'title="Device {device_id}"' in body
    assert f"Device identifier: {device_id}" in body


def test_devices_save_accepts_a_listed_player(client, ma_players):
    response = client.post(
        "/devices",
        headers=AUTH,
        data={"device_id": "device-d", "player_id": "ma-studio"},
    )
    assert response.status_code == 302
    assert device_mapping.get_player_for_device("device-d") == "ma-studio"
    body = client.get("/devices", headers=AUTH).get_data(as_text=True)
    assert "Controls" in body and "Studio speaker" in body


def test_devices_save_rejects_a_player_name(client, ma_players):
    device_mapping.set_player_for_device("device-e", "ma-kitchen")
    response = client.post(
        "/devices", headers=AUTH, data={"device_id": "device-e", "player_id": "Kitchen"}
    )
    assert response.status_code == 302
    assert "error=unknown-player" in response.headers["Location"]
    # Nothing was written: the previous mapping survives and a new device is
    # never given a name.
    assert device_mapping.get_player_for_device("device-e") == "ma-kitchen"
    body = client.get("/devices?error=unknown-player", headers=AUTH).get_data(
        as_text=True
    )
    assert "was left unchanged" in body


def test_devices_save_clears_mapping_from_dropdown(client, ma_players):
    device_mapping.set_player_for_device("device-f", "ma-kitchen")
    client.post(
        "/devices", headers=AUTH, data={"device_id": "device-f", "player_id": ""}
    )
    assert device_mapping.get_player_for_device("device-f") is None


def test_devices_save_without_player_list_still_saves(client):
    # Music Assistant unreachable: the free-text fallback keeps the old
    # behaviour of accepting whatever id was sent.
    client.post(
        "/devices", headers=AUTH, data={"device_id": "device-g", "player_id": "Studio"}
    )
    assert device_mapping.get_player_for_device("device-g") == "Studio"
