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
    """Verify empty invocations page."""
    response = client.get("/invocations", headers=AUTH)
    assert response.status_code == 200
    assert response.mimetype == "text/html"
    assert "No invocations logged" in response.get_data(as_text=True)


def test_invocations_page_renders_newest_first(client, logs):
    """Verify invocations page renders newest first."""
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
    """Verify invocations page handles missing fields."""
    logs.append({"incoming": None, "response": None, "ts": None})
    logs.append({"incoming": "not-json", "response": "", "ts": "not-a-number"})
    body = client.get("/invocations", headers=AUTH).get_data(as_text=True)
    assert "(no payload)" in body
    assert "(no response)" in body
    assert "(no timestamp)" in body
    assert "(no request name)" in body
    assert "not-json" in body


def test_status_invocations_count_endpoint(client, logs):
    """Verify status invocations count endpoint."""
    from endpoints import status

    empty = client.get("/status/invocations", headers=AUTH).json
    assert "Not tested" in empty.pop("echo_playback_html")
    assert empty == {
        "count": 0,
        "invocations_html": status.RECENT_ACTIVITY_EMPTY,
    }
    logs.append({"incoming": {}, "response": "{}", "ts": 1.0})
    payload = client.get("/status/invocations", headers=AUTH).json
    assert payload["count"] == 1
    assert "ma-timeline" in payload["invocations_html"]
    assert "Alexa request" in payload["invocations_html"]


def test_render_entry_prefers_time_then_incoming_timestamp():
    """Verify render entry prefers time then incoming timestamp."""
    entry = {"incoming": {"request": {"timestamp": "2026-01-02T03:04:05Z"}}}
    assert "03:04:05" in invocations._render_entry(1, entry)
    entry = {"incoming": {"request": {}}, "ts": 1700000000.0}
    assert "(no request name)" in invocations._render_entry(1, entry)


def test_render_entry_supports_payload_key_alias_and_json_string_response():
    """Verify render entry supports payload key alias and json string response."""
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
    """Verify format time shapes."""
    from datetime import datetime, timezone

    assert invocations._format_time(None) == "(no timestamp)"
    # _format_time parses Z as UTC, so the expectation must be UTC too — a
    # naive datetime would be interpreted as local time on non-UTC hosts.
    assert invocations._format_time("2026-01-02T03:04:05Z") == datetime(
        2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc
    ).astimezone().strftime("%H:%M:%S")
    # Naive ISO strings are treated as local time.
    assert invocations._format_time("2026-01-02T03:04:05") == "03:04:05"
    # Unparseable strings fall back to the escaped raw value.
    assert invocations._format_time("not-a-time") == "not-a-time"
    # Non-numeric timestamps render via str().
    assert invocations._format_time(object()) is not None


def test_format_value_shapes():
    """Verify format value shapes."""
    assert invocations._format_value(None, "E") == "E"
    assert invocations._format_value("  ", "E") == "E"
    assert invocations._format_value("null", "E") == "E"
    assert invocations._format_value('{"a": 1}', "E") == '{\n  "a": 1\n}'
    assert invocations._format_value("plain text", "E") == "plain text"
    assert invocations._format_value({"b": 2}, "E") == '{\n  "b": 2\n}'
    # Unserializable objects fall back to str().
    assert invocations._format_value(object(), "E")


def test_entry_helpers_on_non_dict_inputs():
    """Verify entry helpers on non dict inputs."""
    assert invocations._entry_value({}, "a", "b") is None
    assert invocations._entry_value({"b": 2}, "a", "b") == 2
    assert invocations._entry_timestamp({"incoming": {}}, "payload-str") is None
    assert invocations._entry_name("not-a-dict") is None
    assert invocations._entry_name({"request": {"intent": "not-a-dict"}}) is None


@pytest.mark.parametrize(
    ("status", "label", "state"),
    [
        (200, "HTTP 200", " ma-ok"),
        (204, "HTTP 204", " ma-ok"),
        (302, "HTTP 302", " ma-warning"),
        (400, "HTTP 400", " ma-error"),
        (500, "HTTP 500", " ma-error"),
        (None, "Not recorded", ""),
        (True, "Not recorded", ""),
        ("<script>", "Not recorded", ""),
        (999, "Not recorded", ""),
    ],
)
def test_invocations_show_recorded_http_status_on_desktop_and_mobile(
    client, logs, status, label, state
):
    """Verify invocations show recorded http status on desktop and mobile."""
    logs.append({"incoming": {}, "response_status": status})
    body = client.get("/invocations", headers=AUTH).get_data(as_text=True)
    assert "<th>HTTP status</th>" in body
    badge = (
        f"<span class='ma-state{state}'>"
        f"<span class='ma-dot' aria-hidden='true'></span>{label}</span>"
    )
    table = body.split("<table", 1)[1].split("</table>", 1)[0]
    card = body.split("<article", 1)[1].split("</article>", 1)[0]
    assert table.count(badge) == 1
    assert card.count(badge) == 1
    assert "Unknown</span>" not in body


@pytest.mark.parametrize(
    ("event", "label", "state"),
    [
        ("PlaybackStarted", "Playback confirmed", "ma-ok"),
        ("PlaybackFinished", "Playback finished", "ma-ok"),
        ("PlaybackStopped", "Playback stopped", "ma-state"),
        ("PlaybackFailed", "Playback failed", "ma-error"),
    ],
)
def test_echo_playback_reports_device_event(client, logs, event, label, state):
    """Verify echo playback reports device event."""
    logs.append(
        {
            "incoming": {
                "request": {"type": "AudioPlayer." + event},
                "context": {"System": {"device": {"deviceId": "device-123456"}}},
            },
            "ts": 1700000000,
            "response_status": 200,
        }
    )
    fragment = client.get("/status/invocations", headers=AUTH).json[
        "echo_playback_html"
    ]
    assert label in fragment
    assert state in fragment
    assert "123456" in fragment
    assert "Last reported event" in fragment
    assert label in client.get("/status", headers=AUTH).get_data(as_text=True)


def test_echo_playback_ignores_simulator_and_unrelated_requests(client, logs):
    """Verify echo playback ignores simulator and unrelated requests."""
    logs.extend(
        [
            {"incoming": {"request": {"type": "IntentRequest"}}},
            {"incoming": {"request": {"type": "AudioPlayer.PlaybackStarted"}}},
            {"incoming": None},
            {
                "incoming": {
                    "request": {"type": "AudioPlayer.PlaybackStarted"},
                    "context": {"System": {"device": {"deviceId": "sim-device"}}},
                },
                "simulator": True,
                "response_status": 200,
            },
        ]
    )
    fragment = client.get("/status/invocations", headers=AUTH).json[
        "echo_playback_html"
    ]
    assert "Not tested" in fragment


def test_echo_playback_uses_event_time_not_completion_order(client, logs):
    """Verify echo playback uses event time not completion order."""
    for event, ts in [("PlaybackFailed", 200), ("PlaybackStarted", 100)]:
        logs.append(
            {
                "incoming": {
                    "request": {"type": "AudioPlayer." + event},
                    "context": {"System": {"device": {"deviceId": "device-123456"}}},
                },
                "ts": ts,
                "response_status": 200,
            }
        )
    fragment = client.get("/status/invocations", headers=AUTH).json[
        "echo_playback_html"
    ]
    assert "Playback failed" in fragment


@pytest.mark.parametrize("code", [None, 400, 401, 403, 500])
def test_echo_playback_ignores_rejected_callbacks(client, logs, code):
    """Verify echo playback ignores rejected callbacks."""
    logs.append(
        {
            "incoming": {
                "request": {"type": "AudioPlayer.PlaybackStarted"},
                "context": {"System": {"device": {"deviceId": "device-123456"}}},
            },
            "response_status": code,
        }
    )
    assert (
        "Not tested"
        in client.get("/status/invocations", headers=AUTH).json["echo_playback_html"]
    )


@pytest.mark.parametrize("entry", [None, "invalid", 42, []])
def test_malformed_invocations_render_without_crashing(client, logs, entry):
    """Verify malformed invocations render without crashing."""
    logs.append(entry)
    response = client.get("/invocations", headers=AUTH)
    assert response.status_code == 200
    assert "Not recorded" in response.get_data(as_text=True)
    assert "Not recorded" in invocations._response_status_html(entry)


@pytest.mark.parametrize("timestamp", [None, "invalid", "NaN", "Infinity"])
def test_playback_event_time_falls_back_to_receipt(timestamp):
    """Verify playback event time falls back to receipt."""
    from endpoints.status import _playback_event_time

    assert _playback_event_time({"timestamp": timestamp}, {"ts": 200}) == 200
    assert _playback_event_time({"timestamp": timestamp}, {}) is None


def test_delayed_playback_callback_does_not_replace_newer_event(client, logs):
    """Verify delayed playback callback does not replace newer event."""
    for event, timestamp, receipt in [
        ("PlaybackStarted", "2026-10-24T12:01:00Z", 200),
        ("PlaybackFailed", "2026-10-24T12:00:00Z", 300),
    ]:
        logs.append(
            {
                "incoming": {
                    "request": {"type": "AudioPlayer." + event, "timestamp": timestamp},
                    "context": {"System": {"device": {"deviceId": "device-123456"}}},
                },
                "response_status": 200,
                "ts": receipt,
            }
        )
    fragment = client.get("/status/invocations", headers=AUTH).json[
        "echo_playback_html"
    ]
    assert "Playback confirmed" in fragment
    assert "Playback failed" not in fragment
