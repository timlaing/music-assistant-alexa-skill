"""Tests for the read-only Music Assistant channel verification (V1–V6)."""

import asyncio
import json
import time
from types import SimpleNamespace

import shared_store
import verification
from endpoints import status

AUTH = {"Authorization": "Basic dGVzdC11c2VyOnRlc3QtcGFzc3dvcmQ="}


def _channel(**overrides):
    channel = {
        "configured": True,
        "server_version": "3.4.7",
        "providers_error": None,
        "alexa": {"enabled": True, "status": "loaded", "last_error": None},
        "setup_keys": [
            "alexa_language",
            "api_password",
            "api_url",
            "api_username",
            "log_level",
        ],
        "setup_error": None,
        "alexa_players": [],
    }
    channel.update(overrides)
    return channel


def _store(**overrides):
    store = {
        "streamUrl": "https://streams.example.com/music/live.mp3",
        "title": "Song",
        "artist": "Artist",
        "push_base": "http://ha.example.com:5000",
        "version": 3,
        "timestamp": time.time() - 300,
    }
    store.update(overrides)
    return store


def _fetched(base, status=None, error=None, auth=True):
    return {
        "url": f"{base.rstrip('/')}/ma/latest-url",
        "status": status,
        "error": error,
        "auth": auth,
    }


# --- V1: provider health -------------------------------------------------


def test_v1_provider_health_states():
    assert verification._v1_provider(_channel())["state"] == "pass"
    disabled = _channel(
        alexa={"enabled": False, "status": "loaded", "last_error": None}
    )
    assert verification._v1_provider(disabled)["state"] == "fail"
    errored = _channel(
        alexa={"enabled": True, "status": "loaded", "last_error": "boom"}
    )
    check = verification._v1_provider(errored)
    assert check["state"] == "fail"
    assert "boom" in check["detail"]
    wrong_status = _channel(
        alexa={"enabled": True, "status": "errored", "last_error": None}
    )
    assert verification._v1_provider(wrong_status)["state"] == "fail"
    assert verification._v1_provider(_channel(alexa=None))["state"] == "fail"
    assert verification._v1_provider({"configured": False})["state"] == "unknown"
    unreachable = verification._v1_provider(
        {"configured": True, "error": "TimeoutError: x"}
    )
    assert unreachable["state"] == "fail"
    assert (
        verification._v1_provider(_channel(providers_error="KeyError: x"))["state"]
        == "fail"
    )


# --- V2a: setup_data key presence ----------------------------------------


def test_v2a_setup_keys_states():
    assert verification._v2a_setup_keys(_channel())["state"] == "pass"
    missing = _channel(setup_keys=["api_username", "api_password"])
    check = verification._v2a_setup_keys(missing)
    assert check["state"] == "fail"
    assert "api_url" in check["detail"]
    assert verification._v2a_setup_keys(_channel(setup_keys=[]))["state"] == "fail"
    assert (
        verification._v2a_setup_keys(_channel(setup_error="RuntimeError: nope"))[
            "state"
        ]
        == "unknown"
    )
    assert verification._v2a_setup_keys(_channel(setup_keys=None))["state"] == "unknown"
    assert verification._v2a_setup_keys({"configured": False})["state"] == "unknown"
    assert (
        verification._v2a_setup_keys({"configured": True, "error": "x"})["state"]
        == "unknown"
    )


# --- V2b: observed push address ------------------------------------------


def test_v2b_push_base_states():
    check = verification._v2b_push_base(_store())
    assert check["state"] == "pass"
    assert "ha.example.com" in check["detail"]
    assert verification._v2b_push_base({"streamUrl": "x"})["state"] == "unknown"


# --- V3/V4: self-fetch reachability + auth --------------------------------


def test_v3_v4_reachable_and_auth(monkeypatch):
    monkeypatch.setattr(
        verification, "_self_fetch", lambda base: _fetched(base, status=200)
    )
    v3, v4 = verification._v3_v4_fetch(_store())
    assert (v3["state"], v4["state"]) == ("pass", "pass")

    # 404: reachable and authenticated, nothing stored yet
    monkeypatch.setattr(
        verification, "_self_fetch", lambda base: _fetched(base, status=404)
    )
    v3, v4 = verification._v3_v4_fetch(_store())
    assert (v3["state"], v4["state"]) == ("pass", "pass")
    assert "404" in v4["detail"]

    # 401: reachable but credentials rejected
    monkeypatch.setattr(
        verification, "_self_fetch", lambda base: _fetched(base, status=401)
    )
    v3, v4 = verification._v3_v4_fetch(_store())
    assert (v3["state"], v4["state"]) == ("pass", "fail")

    # transport failure: unreachable, auth cannot be judged
    monkeypatch.setattr(
        verification,
        "_self_fetch",
        lambda base: _fetched(base, error="ConnectTimeout: nope"),
    )
    v3, v4 = verification._v3_v4_fetch(_store())
    assert (v3["state"], v4["state"]) == ("fail", "unknown")
    assert "ha.example.com" in v3["detail"] or "ConnectTimeout" in v3["detail"]


def test_v3_v4_no_credentials_and_no_target(monkeypatch):
    # Endpoint answered but no basic-auth credentials exist to verify
    monkeypatch.setattr(
        verification, "_self_fetch", lambda base: _fetched(base, status=200, auth=False)
    )
    _, v4 = verification._v3_v4_fetch(_store())
    assert v4["state"] == "unknown"

    # Nothing recorded and no SKILL_HOSTNAME: nothing to fetch at all
    monkeypatch.setattr(verification, "get_setting", lambda name, default="": default)
    v3, v4 = verification._v3_v4_fetch({})
    assert (v3["state"], v4["state"]) == ("unknown", "unknown")


# --- V5: freshness ---------------------------------------------------------


def test_v5_freshness_states():
    fresh = verification._v5_freshness(_store(timestamp=time.time() - 60))
    assert fresh["state"] == "pass"
    stale = verification._v5_freshness(_store(timestamp=time.time() - 7200))
    assert stale["state"] == "warning"
    assert "hours ago" in stale["detail"]
    assert verification._v5_freshness({"streamUrl": "x"})["state"] == "unknown"


# --- V6: now-playing cross-check ------------------------------------------


def test_v6_now_playing_states():
    playing = {
        "player_id": "echo1",
        "name": "Office Echo",
        "state": "playing",
        "title": "Song",
        "artist": "Artist",
    }
    idle = {**playing, "state": "idle"}

    check = verification._v6_now_playing(_channel(alexa_players=[playing]), _store())
    assert check["state"] == "pass"
    assert "Office Echo" in check["detail"]

    other = {**playing, "title": "Different Song"}
    check = verification._v6_now_playing(_channel(alexa_players=[other]), _store())
    assert check["state"] == "fail"
    assert "Different Song" in check["detail"]

    assert (
        verification._v6_now_playing(_channel(alexa_players=[idle]), _store())["state"]
        == "unknown"
    )
    assert (
        verification._v6_now_playing(_channel(alexa_players=[]), _store())["state"]
        == "fail"
    )
    assert (
        verification._v6_now_playing(_channel(alexa_players=[playing]), {"x": 1})[
            "state"
        ]
        == "unknown"
    )
    assert verification._v6_now_playing({"configured": False}, _store())["state"] == (
        "unknown"
    )
    assert verification._v6_now_playing({"configured": True, "error": "x"}, _store())[
        "state"
    ] == ("unknown")


# --- composition -----------------------------------------------------------


def test_run_checks_composes_all_checks(monkeypatch):
    shared_store.set_ma(_store())
    monkeypatch.setattr(
        verification,
        "_read_channel",
        lambda: _channel(
            alexa_players=[
                {
                    "player_id": "e",
                    "name": "Echo",
                    "state": "playing",
                    "title": "Song",
                    "artist": "Artist",
                }
            ]
        ),
    )
    monkeypatch.setattr(
        verification, "_self_fetch", lambda base: _fetched(base, status=200)
    )
    result = verification.run_checks()
    assert [c["id"] for c in result["checks"]] == [
        "v1",
        "v2a",
        "v2b",
        "v3",
        "v4",
        "v5",
        "v6",
    ]
    assert all(c["state"] == "pass" for c in result["checks"])
    assert result["summary"]["kind"] == "good"
    assert result["summary"]["counts"]["pass"] == 7
    assert result["generated_at"]


def test_run_checks_records_read_failures(monkeypatch):
    def boom():
        raise RuntimeError("kaput")

    monkeypatch.setattr(verification, "_read_channel", boom)
    result = verification.run_checks()
    assert result["checks"][0]["id"] == "v0"
    assert result["checks"][0]["state"] == "unknown"
    assert "kaput" in result["checks"][0]["detail"]


def test_checks_html_escapes_details_and_maps_pills():
    result = {
        "checks": [
            verification._check("v1", "Alexa provider", "pass", "fine <b>safe</b>"),
            verification._check("v2a", "Setup", "fail", "bad"),
        ]
    }
    html = verification.checks_html(result)
    assert "ma-ok" in html and "ma-error" in html
    assert "Passed" in html and "Failed" in html
    assert "&lt;b&gt;safe&lt;/b&gt;" in html
    assert "<b>safe</b>" not in html


# --- endpoints / UI ---------------------------------------------------------


def test_status_verify_endpoint(client, monkeypatch):
    monkeypatch.setattr(verification, "_read_channel", lambda: _channel())
    response = client.get("/status/verify", headers=AUTH)
    assert response.status_code == 200
    data = response.get_json()
    assert [c["id"] for c in data["checks"]] == [
        "v1",
        "v2a",
        "v2b",
        "v3",
        "v4",
        "v5",
        "v6",
    ]
    assert data["summary"]["kind"] in {"good", "warning", "error", "neutral"}
    assert 'class="ma-row"' in data["checks_html"]


def test_status_page_has_run_checks_widget(client):
    body = client.get("/status", headers=AUTH).get_data(as_text=True)
    assert 'id="run-checks"' in body
    assert 'id="verify-results"' in body
    assert "'/status/verify'" in body


def test_push_url_records_push_base(client):
    payload = {
        "streamUrl": "https://streams.example.com/music/live.mp3",
        "title": "Song",
    }
    response = client.post("/ma/push-url", json=payload, headers=AUTH)
    assert response.status_code == 200
    assert shared_store.get_ma()["push_base"] == "http://localhost"


def test_diagnostics_redacts_verification_push_base(client, monkeypatch):
    shared_store.set_ma(_store(push_base="http://192.168.1.50:5000"))
    monkeypatch.setattr(verification, "_read_channel", lambda: {"configured": False})
    monkeypatch.setattr(
        verification,
        "_self_fetch",
        lambda base: _fetched(base, status=200, auth=False),
    )
    body = client.get("/status/diagnostics", headers=AUTH).get_data(as_text=True)
    data = json.loads(body)
    assert "verification" in data
    assert data["verification"]["checks"]
    assert "192.168.1.50" not in body
    assert "<host-" in body


# --- _read_ma_status bug-fix pins -------------------------------------------


def _fake_client(monkeypatch, send_command, get_provider_configs=None, get_value=None):
    """Install a fake MusicAssistantClient: server_info is an attribute set on
    connect() (not a coroutine) and players come from the players/all command
    (client.players.players is only filled by start_listening(), which the
    skill never runs)."""
    import music_assistant_client
    from unittest.mock import AsyncMock

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            self.server_info = SimpleNamespace(server_version="3.4.7")
            self.config = SimpleNamespace(
                get_provider_configs=AsyncMock(return_value=get_provider_configs or []),
                get_provider_config_value=AsyncMock(return_value=get_value),
            )
            self._send_command = send_command

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def send_command(self, command):
            return self._send_command(command)

    monkeypatch.setattr(music_assistant_client, "MusicAssistantClient", _FakeClient)


def test_read_ma_status_reads_players_from_command(monkeypatch):
    _fake_client(
        monkeypatch,
        lambda command: (
            [
                {
                    "player_id": "echo-1",
                    "name": "Office Echo",
                    "playback_state": "playing",
                    "available": True,
                    "provider": "alexa",
                }
            ]
            if command == "players/all"
            else []
        ),
    )
    result = asyncio.run(status._read_ma_status("http://ma.example.com", "token"))
    assert result["configured"] is True
    assert result["server_version"] == "3.4.7"
    assert result["players"] == [
        {
            "player_id": "echo-1",
            "name": "Office Echo",
            "state": "playing",
            "available": True,
        }
    ]
    assert result["providers"] == {"instances": [], "alexa": []}


def test_read_channel_state_parses_players_and_setup_keys(monkeypatch):
    def players(command):
        assert command == "players/all"
        return [
            {
                "player_id": "echo-1",
                "name": "Office Echo",
                "provider": "alexa",
                "playback_state": "playing",
                "available": True,
                "current_media": {"title": "Song", "artist": "Artist"},
            },
            {
                "player_id": "other-1",
                "name": "Chromecast",
                "provider": "cast",
                "playback_state": "playing",
                "available": True,
                "current_media": None,
            },
            {
                "player_id": "echo-2",
                "name": "Kitchen",
                "provider": "alexa",
                "playback_state": "idle",
                "available": True,
                "current_media": None,
            },
        ]

    _fake_client(
        monkeypatch,
        players,
        get_value={"api_url": "x", "api_username": "y", "api_password": "z"},
    )
    channel = asyncio.run(
        verification._read_channel_state("http://ma.example.com", "token")
    )
    assert channel["configured"] is True
    assert channel["server_version"] == "3.4.7"
    assert channel["setup_keys"] == ["api_password", "api_url", "api_username"]
    assert [p["player_id"] for p in channel["alexa_players"]] == ["echo-1", "echo-2"]
    assert channel["alexa_players"][0]["state"] == "playing"
    assert channel["alexa_players"][0]["title"] == "Song"
    assert channel["alexa_players"][0]["artist"] == "Artist"
    assert channel["alexa_players"][1]["state"] == "idle"
    assert channel["alexa_players"][1]["title"] is None
