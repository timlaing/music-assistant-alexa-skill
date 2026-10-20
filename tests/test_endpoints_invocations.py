"""Tests for the invocation log UI endpoints (endpoints/invocations.py)."""

import pytest
from endpoints import invocations

from app import app

AUTH = {"Authorization": "Basic dGVzdC11c2VyOnRlc3QtcGFzc3dvcmQ="}


@pytest.fixture
def logs():
    """Swap in a fresh intent log for the duration of a test."""
    previous = app.config.get("INTENT_LOGS", [])
    app.config["INTENT_LOGS"] = []
    yield app.config["INTENT_LOGS"]
    app.config["INTENT_LOGS"] = previous


def test_empty_invocations_page(client, logs):
    response = client.get("/invocations", headers=AUTH)
    assert response.status_code == 200
    assert response.mimetype == "text/html"
    assert "No invocations logged" in response.get_data(as_text=True)


def test_invocations_page_renders_newest_first(client, logs):
    logs.append(
        {
            "incoming": {
                "request": {"type": "IntentRequest", "intent": {"name": "PlayAudio"}}
            },
            "response": '{"outputSpeech": {"type": "PlainText", "text": "ok"}}',
            "ts": 1700000000.5,
        }
    )
    logs.append(
        {
            "incoming": {"request": {"type": "LaunchRequest"}},
            "response": {"shouldEndSession": True},
            "ts": 1700000100.0,
        }
    )
    body = client.get("/invocations", headers=AUTH).get_data(as_text=True)
    # Entry 2 is the newest (rendered first); intent name and request type shown.
    assert "Invocation 2" in body
    assert "Invocation 1" in body
    assert body.index("Invocation 2") < body.index("Invocation 1")
    assert "PlayAudio" in body
    assert "LaunchRequest" in body
    assert "shouldEndSession" in body


def test_invocations_page_handles_missing_fields(client, logs):
    logs.append({"incoming": None, "response": None, "ts": None})
    logs.append({"incoming": "not-json", "response": "", "ts": "not-a-number"})
    body = client.get("/invocations", headers=AUTH).get_data(as_text=True)
    assert "(no payload)" in body
    assert "(no response)" in body
    assert "(no timestamp)" in body
    assert "(no request name)" in body
    assert "not-json" in body


def test_status_invocations_count_endpoint(client, logs):
    from endpoints import status

    assert client.get("/status/invocations", headers=AUTH).json == {
        "count": 0,
        "invocations_html": status.RECENT_ACTIVITY_EMPTY,
    }
    logs.append({"incoming": {}, "response": "{}", "ts": 1.0})
    payload = client.get("/status/invocations", headers=AUTH).json
    assert payload["count"] == 1
    assert "ma-timeline" in payload["invocations_html"]
    assert "Alexa request" in payload["invocations_html"]


def test_render_entry_prefers_time_then_incoming_timestamp():
    entry = {"incoming": {"request": {"timestamp": "2026-01-02T03:04:05Z"}}}
    assert "03:04:05" in invocations._render_entry(1, entry)
    entry = {"incoming": {"request": {}}, "ts": 1700000000.0}
    assert "(no request name)" in invocations._render_entry(1, entry)


def test_render_entry_supports_payload_key_alias_and_json_string_response():
    entry = {
        "payload": {"request": {"type": "SessionEndedRequest"}},
        "response_body": '{"ok": true}',
        "time": "2026-01-02T03:04:05+00:00",
    }
    body = invocations._render_entry(7, entry)
    assert "Invocation 7" in body
    assert "SessionEndedRequest" in body
    # The JSON-string response body is pretty-printed (and HTML-escaped).
    assert "ok" in body
    assert "true" in body


def test_format_time_shapes():
    from datetime import datetime

    assert invocations._format_time(None) == "(no timestamp)"
    assert invocations._format_time("2026-01-02T03:04:05Z") == datetime(
        2026, 1, 2, 3, 4, 5
    ).astimezone().strftime("%H:%M:%S")
    # Naive ISO strings are treated as local time.
    assert invocations._format_time("2026-01-02T03:04:05") == "03:04:05"
    # Unparseable strings fall back to the escaped raw value.
    assert invocations._format_time("not-a-time") == "not-a-time"
    # Non-numeric timestamps render via str().
    assert invocations._format_time(object()) is not None


def test_format_value_shapes():
    assert invocations._format_value(None, "E") == "E"
    assert invocations._format_value("  ", "E") == "E"
    assert invocations._format_value("null", "E") == "E"
    assert invocations._format_value('{"a": 1}', "E") == '{\n  "a": 1\n}'
    assert invocations._format_value("plain text", "E") == "plain text"
    assert invocations._format_value({"b": 2}, "E") == '{\n  "b": 2\n}'
    # Unserializable objects fall back to str().
    assert invocations._format_value(object(), "E")


def test_entry_helpers_on_non_dict_inputs():
    assert invocations._entry_value({}, "a", "b") is None
    assert invocations._entry_value({"b": 2}, "a", "b") == 2
    assert invocations._entry_timestamp({"incoming": {}}, "payload-str") is None
    assert invocations._entry_name("not-a-dict") is None
    assert invocations._entry_name({"request": {"intent": "not-a-dict"}}) is None
