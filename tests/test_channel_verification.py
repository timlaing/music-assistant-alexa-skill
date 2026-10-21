"""Tests for the read-only Music Assistant channel verification (V1–V11)."""

import asyncio
import json
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

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
    base = _store()["push_base"]
    check = verification._v2b_push_base(_store())
    assert check["state"] == "pass"
    assert base in check["detail"]  # the detail quotes the recorded base
    assert urlsplit(base).hostname == "ha.example.com"  # parsed, not substring
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
    failed_url = f"{_store()['push_base'].rstrip('/')}/ma/latest-url"
    assert failed_url in v3["detail"] or "ConnectTimeout" in v3["detail"]


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


# --- V7: last skill invocation ---------------------------------------------


def test_v7_invocations_states():
    fresh = {"incoming": {}, "ts": time.time() - 120}
    assert verification._v7_invocations([fresh])["state"] == "pass"
    stale = {"incoming": {}, "ts": time.time() - 3 * 86400}
    warning = verification._v7_invocations([stale])
    assert warning["state"] == "warning"
    assert "days ago" in warning["detail"]
    # Alexa request timestamp fallback (ISO Z string)
    iso = {"incoming": {"request": {"timestamp": "2026-10-21T12:00:00Z"}}}
    assert verification._v7_invocations([iso])["state"] in ("pass", "warning")
    assert verification._v7_invocations([])["state"] == "unknown"
    assert (
        verification._v7_invocations([{"incoming": {}, "ts": "junk"}])["state"]
        == "unknown"
    )
    # Out-of-order completion: the newest request finished FIRST (appended at
    # index 0) — selection must use the timestamp, not logs[-1].
    out_of_order = [
        {"incoming": {}, "ts": time.time() - 60},
        {"incoming": {}, "ts": time.time() - 3 * 86400},
    ]
    assert verification._v7_invocations(out_of_order)["state"] == "pass"


# --- V8: stream host vs configured MA hostname ------------------------------


def test_v8_stream_host_states(monkeypatch):
    monkeypatch.setattr(
        verification,
        "get_setting",
        lambda name, default="": "https://streams.example.com/music",
    )
    match = verification._v8_stream_host(
        {"streamUrl": "https://streams.example.com/music/live.mp3"}
    )
    assert match["state"] == "pass"
    cdn_url = "https://cdn.example.com/x.mp3"
    mismatch = verification._v8_stream_host({"streamUrl": cdn_url})
    assert mismatch["state"] == "fail"
    assert urlsplit(cdn_url).hostname in mismatch["detail"]  # parsed, not substring
    # MA_HOSTNAME empty: pushes are stored without validation
    monkeypatch.setattr(verification, "get_setting", lambda name, default="": "")
    unvalidated = verification._v8_stream_host(
        {"streamUrl": "https://streams.example.com/live.mp3"}
    )
    assert unvalidated["state"] == "warning"
    assert verification._v8_stream_host({})["state"] == "unknown"
    # MA_HOSTNAME present but not a valid public base (http, not https)
    monkeypatch.setattr(
        verification,
        "get_setting",
        lambda name, default="": "http://streams.example.com",
    )
    assert (
        verification._v8_stream_host({"streamUrl": "https://x.example.com/a"})["state"]
        == "unknown"
    )


# --- V9: player inventory ----------------------------------------------------


def test_v9_players_states():
    assert verification._v9_players({"configured": False})["state"] == "unknown"
    assert verification._v9_players({"configured": True, "error": "x"})["state"] == (
        "unknown"
    )
    assert verification._v9_players(_channel(alexa_players=[]))["state"] == "fail"
    players = [
        {"name": "Office Echo", "state": "idle", "available": True},
        {"name": "Kitchen", "state": "playing", "available": True},
        {"name": "Bedroom", "state": "idle", "available": False},
    ]
    info = verification._v9_players(_channel(alexa_players=players))
    assert info["state"] == "info"
    assert "3 registered" in info["detail"]
    assert "2 idle, 1 playing" in info["detail"]
    assert "1 unavailable" in info["detail"]
    # Long fleets are capped in the listing
    many = [{"name": f"Echo {i}", "state": "idle", "available": True} for i in range(9)]
    capped = verification._v9_players(_channel(alexa_players=many))
    assert "+3 more" in capped["detail"]


# --- V10: Amazon account link -------------------------------------------------


def _oauth_env(monkeypatch):
    """config_key()/oauth_config() need all three LWA values."""
    monkeypatch.setenv("LWA_CLIENT_ID", "demo-client-id")
    monkeypatch.setenv("LWA_CLIENT_SECRET", "demo-client-secret")
    monkeypatch.setenv(
        "LWA_REDIRECT_URI",
        "https://skills.example.com/ma-alexa-skill/setup/oauth/callback",
    )


def _link_state(**overrides):
    from skill_deployment import config_key

    state = {
        "client_key": config_key(),
        "tokens": {
            "access_token": "sekrit-access-token",
            "refresh_token": "r",
            "expires": time.time() + 7200,
        },
    }
    state.update(overrides)
    return state


def test_v10_alexa_link_states(monkeypatch):
    _oauth_env(monkeypatch)
    assert verification._v10_alexa_link(None)["state"] == "unknown"
    assert verification._v10_alexa_link({})["state"] == "unknown"
    access_only = {"tokens": {"access_token": "a"}}
    assert verification._v10_alexa_link(access_only)["state"] == "warning"
    good = verification._v10_alexa_link(_link_state())
    assert good["state"] == "pass"
    assert "not revalidated" in good["detail"]  # honest: no Amazon call made
    assert "sekrit" not in good["detail"]  # token values never carried out
    expired = verification._v10_alexa_link(
        _link_state(tokens={"access_token": "a", "refresh_token": "r", "expires": 1.0})
    )
    assert expired["state"] == "pass"  # refresh token present; renews on use
    wrong_client = verification._v10_alexa_link(_link_state(client_key="other"))
    assert wrong_client["state"] == "warning"


# --- V11: reachability fold-in ------------------------------------------------


def test_v11_reachability_states():
    assert verification._v11_reachability(None)["state"] == "unknown"
    checking = {"checked_at": None, "checking": True, "checks": []}
    assert verification._v11_reachability(checking)["state"] == "unknown"
    empty = {"checked_at": None, "checking": False, "checks": []}
    assert verification._v11_reachability(empty)["state"] == "unknown"
    good = {
        "checked_at": time.time(),
        "checking": False,
        "checks": [
            {"label": "Skill endpoint", "status": "reachable", "message": "ok"},
            {"label": "Stream host", "status": "reachable", "message": "ok"},
            {"label": "Public audio delivery", "status": "reachable", "message": "ok"},
        ],
    }
    check = verification._v11_reachability(good)
    assert check["state"] == "pass"
    bad = {
        "checked_at": time.time(),
        "checking": False,
        "checks": [
            {"label": "Skill endpoint", "status": "failed", "message": "DNS"},
            {"label": "Public audio delivery", "status": "reachable", "message": "ok"},
        ],
    }
    assert verification._v11_reachability(bad)["state"] == "fail"


# --- composition -----------------------------------------------------------


def test_run_checks_composes_all_checks(monkeypatch):
    from skill_deployment import config_key

    _oauth_env(monkeypatch)
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
                    "available": True,
                    "powered": True,
                    "title": "Song",
                    "artist": "Artist",
                }
            ]
        ),
    )
    monkeypatch.setattr(
        verification, "_self_fetch", lambda base: _fetched(base, status=200)
    )
    monkeypatch.setattr(
        verification, "_intent_logs", lambda: [{"incoming": {}, "ts": time.time()}]
    )
    monkeypatch.setattr(
        verification,
        "_deployment_state",
        lambda: {
            "client_key": config_key(),
            "tokens": {
                "access_token": "a",
                "refresh_token": "r",
                "expires": time.time() + 3600,
            },
        },
    )
    monkeypatch.setattr(
        verification,
        "_reachability_snapshot",
        lambda: {
            "checked_at": time.time(),
            "checking": False,
            "checks": [
                {"label": "Skill endpoint", "status": "reachable", "message": "ok"},
                {"label": "Stream host", "status": "reachable", "message": "ok"},
                {
                    "label": "Public audio delivery",
                    "status": "reachable",
                    "message": "ok",
                },
            ],
        },
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
        "v7",
        "v8",
        "v9",
        "v10",
        "v11",
    ]
    assert all(c["state"] in ("pass", "info") for c in result["checks"])
    assert result["summary"]["kind"] == "good"
    assert result["summary"]["counts"]["pass"] == 11
    assert result["summary"]["counts"]["info"] == 1
    assert result["generated_at"]
    # F: the bundle carries the player inventory (states + availability).
    assert result["players"] == [
        {
            "player_id": "e",
            "name": "Echo",
            "state": "playing",
            "available": True,
            "powered": True,
            "title": "Song",
            "artist": "Artist",
        }
    ]


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
        "v7",
        "v8",
        "v9",
        "v10",
        "v11",
    ]
    # The fixture has no alexa players (v6+v9 fail) and the conftest
    # self-fetch stub blocks v3 — pin the exact outcome so a silent
    # all-pass regression cannot slip through.
    assert data["summary"]["kind"] == "error"
    assert data["summary"]["counts"]["fail"] == 3  # v3 fetch, v6+v9 fleet
    assert 'class="ma-row"' in data["checks_html"]
    assert "players" in data


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
    assert "players" in data["verification"]
    assert "192.168.1.50" not in body
    assert "<host-" in body


# --- _read_ma_status bug-fix pins -------------------------------------------


def _fake_client(monkeypatch, send_command, get_provider_configs=None, get_value=None):
    """Install a fake MusicAssistantClient: server_info is an attribute set on
    connect() (not a coroutine) and players come from the players/all command
    (client.players.players is only filled by start_listening(), which the
    skill never runs). Returns the fake class so tests can assert call args."""
    import music_assistant_client
    from unittest.mock import AsyncMock

    class _FakeClient:
        instances = []

        def __init__(self, *args, **kwargs):
            _FakeClient.instances.append(self)
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
    return _FakeClient


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
                "powered": True,
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
                "available": False,
                "powered": False,
                "current_media": None,
            },
        ]

    fake_cls = _fake_client(
        monkeypatch,
        players,
        get_provider_configs=[
            # The provider instance ID can differ from its domain — the setup
            # lookup must use this discovered ID, not a hard-coded "alexa".
            SimpleNamespace(domain="alexa", instance_id="abc123"),
        ],
        get_value={"api_url": "x", "api_username": "y", "api_password": "z"},
    )
    channel = asyncio.run(
        verification._read_channel_state("http://ma.example.com", "token")
    )
    assert channel["configured"] is True
    assert channel["server_version"] == "3.4.7"
    assert channel["setup_keys"] == ["api_password", "api_url", "api_username"]
    lookup = fake_cls.instances[-1].config.get_provider_config_value
    assert lookup.await_args.kwargs == {
        "instance_id": "abc123",
        "key": "setup_data",
        "default": None,
    }
    assert [p["player_id"] for p in channel["alexa_players"]] == ["echo-1", "echo-2"]
    assert channel["alexa_players"][0]["state"] == "playing"
    assert channel["alexa_players"][0]["available"] is True
    assert channel["alexa_players"][0]["title"] == "Song"
    assert channel["alexa_players"][0]["artist"] == "Artist"
    assert channel["alexa_players"][1]["state"] == "idle"
    assert channel["alexa_players"][1]["available"] is False
    assert channel["alexa_players"][1]["title"] is None
