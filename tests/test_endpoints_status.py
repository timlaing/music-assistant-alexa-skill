"""Tests for the status page endpoints and its ASK/deployment fragments."""

import json
from types import SimpleNamespace

import pytest
import shared_store
from endpoints import status
from skill_deployment import DeploymentError

from app import app

AUTH = {"Authorization": "Basic dGVzdC11c2VyOnRlc3QtcGFzc3dvcmQ="}


def _run(result):
    """Fake subprocess.run returning the given (returncode, stdout, stderr)."""
    returncode, stdout, stderr = result
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_configured_host_strips_scheme():
    assert (
        status._configured_host("https://skill.example.com/path") == "skill.example.com"
    )
    assert status._configured_host("skill.example.com") == "skill.example.com"
    # Non-string input raises internally and falls back to the raw value.
    assert status._configured_host(42) == 42


@pytest.mark.parametrize(
    "result,expected",
    [
        ((0, "Command executed successfully", ""), True),
        ((1, "", "Command executed successfully"), True),
        ((1, '[Error]: {"message": "nope"}', ""), False),
        ((1, "HTTP 404", ""), False),
        ((1, '{"isEnabled": true}', ""), True),
        ((1, '{"enabled": true}', ""), True),
        ((1, "nothing useful", ""), False),
    ],
)
def test_ask_testing_enabled_interprets_cli_output(monkeypatch, result, expected):
    monkeypatch.setattr(status.subprocess, "run", lambda *a, **kw: _run(result))
    assert status._ask_testing_enabled("amzn1.ask.skill.1") is expected


def test_ask_testing_enabled_handles_subprocess_failure(monkeypatch):
    def boom(*a, **kw):
        raise OSError("ask missing")

    monkeypatch.setattr(status.subprocess, "run", boom)
    assert status._ask_testing_enabled("sid") is False


def test_manifest_locales_parses_nested_json():
    manifest = json.dumps(
        {"manifest": {"publishingInformation": {"locales": {"en-US": {}, "de-DE": {}}}}}
    )
    assert status._manifest_locales(manifest) == ["en-US", "de-DE"]
    # JSON preceded by other text is found via the first "{".
    assert status._manifest_locales("noise " + manifest) == ["en-US", "de-DE"]
    assert status._manifest_locales("no json at all") == []
    assert status._manifest_locales("{broken") == []
    assert status._manifest_locales("") == []
    assert status._manifest_locales(json.dumps({"manifest": {}})) == []
    assert (
        status._manifest_locales(
            json.dumps({"manifest": {"publishingInformation": {"locales": "en-US"}}})
        )
        == []
    )


def test_manifest_locales_swallows_unexpected_errors(monkeypatch):
    # Force json.loads to blow up in the outer handler (non-Exception base).
    assert status._manifest_locales(12345) == []


def test_ask_manifest_html_branches():
    sid = "amzn1.ask.skill.abc"
    # No URL in the manifest -> yellow "endpoint not set".
    html, is_green = status._ask_manifest_html(
        sid, "no urls here", "https://cfg.example.com", True
    )
    assert not is_green
    assert "endpoint not set" in html
    # Matching host + testing enabled -> green.
    manifest = "manifest says https://cfg.example.com/skill"
    html, is_green = status._ask_manifest_html(
        sid, manifest, "https://cfg.example.com", True
    )
    assert is_green
    assert "testing enabled" in html
    assert "cfg.example.com" in html
    # Matching host, testing disabled -> yellow.
    html, is_green = status._ask_manifest_html(
        sid, manifest, "https://cfg.example.com", False
    )
    assert not is_green
    assert "testing NOT enabled" in html
    # Mismatched host -> red.
    html, is_green = status._ask_manifest_html(
        sid, "see https://other.example.com/skill", "https://cfg.example.com", True
    )
    assert not is_green
    assert "mismatch" in html
    # Bare-host configuration still compares equal to the manifest netloc.
    html, is_green = status._ask_manifest_html(sid, manifest, "cfg.example.com", True)
    assert is_green


def test_ask_manifest_html_handles_parse_failure(monkeypatch):
    def boom(*a, **kw):
        raise ValueError("bad url")

    monkeypatch.setattr(status.urllib.parse, "urlparse", boom)
    html, is_green = status._ask_manifest_html(
        "sid", "https://x.example.com", "https://x.example.com", True
    )
    assert not is_green
    assert "endpoint parse failed" in html


def test_ask_cli_status_html_without_credentials(monkeypatch):
    monkeypatch.setattr(status, "has_functional_cli_config", lambda profile: False)
    html = status._ask_cli_status_html("https://skill.example.com")
    assert "ASK CLI credentials are not configured" in html
    assert "Open Setup" in html


def test_ask_cli_status_html_without_skill(monkeypatch):
    monkeypatch.setattr(status, "has_functional_cli_config", lambda profile: True)

    def run(cmd, **kw):
        return _run((0, "no skills here", ""))

    monkeypatch.setattr(status.subprocess, "run", run)
    html = status._ask_cli_status_html("https://skill.example.com")
    assert "interaction model not found" in html


def test_ask_cli_status_html_with_found_skill(monkeypatch):
    monkeypatch.setattr(status, "has_functional_cli_config", lambda profile: True)
    sid = "amzn1.ask.skill.1234abcd"
    manifest = json.dumps(
        {
            "manifest": {
                "publishingInformation": {"locales": {"en-US": {}}},
                "apis": {
                    "custom": {"endpoint": {"uri": "https://cfg.example.com/skill"}}
                },
            }
        }
    )

    def run(cmd, **kw):
        if "list-skills-for-vendor" in cmd:
            return _run((0, json.dumps({"skills": [{"skillId": sid}]}), ""))
        if "get-skill-manifest" in cmd:
            return _run((0, manifest, ""))
        if "get-skill-enablement-status" in cmd:
            return _run((0, '{"isEnabled": true}', ""))
        raise AssertionError(cmd)

    monkeypatch.setattr(status.subprocess, "run", run)
    html = status._ask_cli_status_html("https://cfg.example.com")
    assert "endpoint matches" in html
    assert "en-US" in html
    assert "Open Setup" not in html


def test_deployment_status_html_ready(monkeypatch):
    deployment = {
        "phase": "complete",
        "message": "",
        "skill_id": "amzn1.ask.skill.ready",
        "verified_at": "2026-01-01",
    }
    instance = SimpleNamespace(
        public_status=lambda: deployment,
        state={"deployed_settings": {"endpoint": "https://cfg.example.com"}},
    )
    monkeypatch.setattr(status, "manager", lambda: instance)
    monkeypatch.setattr(
        status, "settings", lambda: {"endpoint": "https://cfg.example.com"}
    )
    html = status._deployment_status_html()
    assert "Amazon verified deployment" in html
    assert "amzn1.ask.skill.ready" in html
    assert "2026-01-01" in html


def test_deployment_status_html_not_ready_and_settings_error(monkeypatch):
    instance = SimpleNamespace(
        public_status=lambda: {"phase": "running", "message": "Deploying…"},
        state={"deployed_settings": None},
    )
    monkeypatch.setattr(status, "manager", lambda: instance)
    # Unverified deployments render no summary line on the status page.
    assert status._deployment_status_html() == ""

    # settings() raising DeploymentError must not break the fragment.
    def bad_settings():
        raise DeploymentError("Skill endpoint must be public HTTPS")

    instance.state["deployed_settings"] = {"endpoint": "https://cfg.example.com"}
    monkeypatch.setattr(status, "settings", bad_settings)
    assert status._deployment_status_html() == ""


def test_skill_row_html_states(client, monkeypatch):
    # Connected + settings match -> approved "Deployed" row.
    instance = SimpleNamespace(
        public_status=lambda: {"connected": True, "phase": "complete"},
        state={"deployed_settings": {"endpoint": "https://cfg.example.com"}},
    )
    monkeypatch.setattr(status, "manager", lambda: instance)
    monkeypatch.setattr(
        status, "settings", lambda: {"endpoint": "https://cfg.example.com"}
    )
    with app.test_request_context("/status"):
        html = status._skill_row_html()
    assert "Music Assistant" in html
    assert "Deployed" in html
    assert "Development skill · " in html
    # Settings changed after the deploy -> out of date.
    monkeypatch.setattr(status, "settings", lambda: {"endpoint": "https://other"})
    with app.test_request_context("/status"):
        assert "Out of date" in status._skill_row_html()
    # Never linked to Amazon -> not deployed.
    disconnected = SimpleNamespace(public_status=lambda: {"connected": False}, state={})
    monkeypatch.setattr(status, "manager", lambda: disconnected)
    with app.test_request_context("/status"):
        assert "Not deployed" in status._skill_row_html()


def test_skill_ask_html_branches(client, monkeypatch):
    # Connected deployment takes precedence over everything else.
    instance = SimpleNamespace(
        public_status=lambda: {"connected": True, "phase": "complete"},
        state={},
    )
    monkeypatch.setattr(status, "manager", lambda: instance)
    monkeypatch.setattr(
        status, "_deployment_status_html", lambda: "DEPLOYMENT-FRAGMENT"
    )
    with app.test_request_context("/status"):
        assert status._skill_ask_html().find("DEPLOYMENT-FRAGMENT") != -1

    # Not connected; ask CLI absent -> muted note.
    disconnected = SimpleNamespace(public_status=lambda: {"connected": False}, state={})
    monkeypatch.setattr(status, "manager", lambda: disconnected)
    monkeypatch.setattr(status.shutil, "which", lambda name: None)
    with app.test_request_context("/status"):
        assert "ask CLI not available" in status._skill_ask_html()

    # ask present but SKILL_HOSTNAME unconfigured.
    monkeypatch.setattr(status.shutil, "which", lambda name: "/usr/bin/ask")
    monkeypatch.setenv("MA_HOSTNAME", "")
    monkeypatch.delenv("SKILL_HOSTNAME", raising=False)
    with app.test_request_context("/status"):
        assert "SKILL_HOSTNAME not configured" in status._skill_ask_html()

    # Any exception becomes a muted error fragment.
    def boom():
        raise RuntimeError("kaput")

    monkeypatch.setattr(status, "manager", boom)
    with app.test_request_context("/status"):
        assert "ASK check error" in status._skill_ask_html()
        assert "kaput" in status._skill_ask_html()


def test_render_metadata_html_branches(client):
    with app.test_request_context("/status"):
        html = status._render_metadata_html(
            {"audioSources": "https://x/song.mp3", "primaryText": "Song"}
        )
        assert '<pre class="ma-code"' in html
        assert "Song" in html
        html = status._render_metadata_html({})
        assert "No track metadata received yet." in html


def test_metadata_status_html_error_branch(monkeypatch):
    from skill import data as skill_data

    def boom():
        raise RuntimeError("store gone")

    monkeypatch.setattr(skill_data, "get_info", boom)
    with app.test_request_context("/status"):
        html = status._metadata_status_html()
    assert "Error loading metadata" in html


def test_store_html_idle_and_ready(client):
    with app.test_request_context("/status"):
        idle = status._store_html({}, "Music Assistant API")
        assert "No stream has been sent yet." in idle
        assert "Waiting" in idle
        html = status._store_html({"streamUrl": "https://x/song.mp3"}, "Alexa API")
        assert "Active" in html
        # Only the summary row remains; the payload diagnostics were removed.
        assert "Payload diagnostics" not in html
        assert "https://x/song.mp3" not in html


def test_diagnostics_download_is_sanitized(client, monkeypatch):
    monkeypatch.setattr(
        status,
        "_ma_status_probe",
        lambda: {"configured": True, "server_version": "2.0.0", "players": []},
    )
    response = client.get("/status/diagnostics", headers=AUTH)
    assert response.status_code == 200
    assert response.mimetype == "application/json"
    disposition = response.headers["Content-Disposition"]
    assert disposition.startswith('attachment; filename="ma-alexa-skill-diagnostics-')
    assert disposition.endswith('.json"')
    body = response.get_data(as_text=True)
    data = json.loads(body)
    for section in (
        "generated_at",
        "app",
        "settings",
        "deployment",
        "music_assistant",
        "verification",
        "stores",
        "recent_invocations",
    ):
        assert section in data
    assert data["music_assistant"]["server_version"] == "2.0.0"
    assert data["settings"]["callback_url"].endswith(
        "/ma-alexa-skill/setup/oauth/callback"
    )
    # Secrets never appear in the bundle — only presence booleans.
    assert "test-password" not in body
    assert "api_password" not in data["settings"]["values"]
    assert "ma_api_token" not in data["settings"]["values"]
    assert isinstance(data["settings"]["secrets_set"]["api_password"], bool)
    # Domains and usernames are masked; URL paths survive for structure.
    assert "streams.example.com" not in body
    assert data["settings"]["values"]["api_username"] == "<redacted>"
    assert "test-user" not in body
    assert data["settings"]["callback_url"] == (
        "https://<host-1>/ma-alexa-skill/setup/oauth/callback"
    )
    # The footer offers the download next to "Report an issue".
    page = client.get("/status", headers=AUTH).get_data(as_text=True)
    assert 'href="/status/diagnostics"' in page


def test_diagnostics_redacts_hosts_usernames_and_ips():
    redacted = status._diagnostics_redact(
        {
            "callback": "https://ha.example.com/ma-alexa-skill/setup/oauth/callback",
            "same_host": "http://ha.example.com:8095/music/x.mp3",
            "bare": "ma.internal",
            "ip_note": "connect to 192.168.1.50 please",
            "mail": "owner@example.com",
            "api_username": "tim-user",
            "serialNumber": "C0AA11BB22",
            "userId": "amzn1.ask.account.XX",
            "deviceAccountId": "AX11",
            "deviceId": "amzn1.ask.device.DD11",
            "personId": "amzn1.ask.person.PP11",
            "version": "2.0.0",
            "stamp": "2026-10-20T22:28:28+00:00",
            "players": [{"name": "Office Echo", "state": "playing"}],
        }
    )
    body = json.dumps(redacted)
    for leaked in (
        "ha.example.com",
        "192.168.1.50",
        "owner@example.com",
        "tim-user",
        "C0AA11BB22",
        "amzn1.ask.account",
        "AX11",
        "amzn1.ask.device",
        "amzn1.ask.person",
    ):
        assert leaked not in body
    # Stable mapping: the same host collapses to one placeholder (port and
    # query dropped, path kept).
    assert redacted["callback"] == (
        "https://<host-1>/ma-alexa-skill/setup/oauth/callback"
    )
    assert redacted["same_host"] == "http://<host-1>/music/x.mp3"
    assert redacted["bare"].startswith("<host-")
    assert redacted["ip_note"] == "connect to <ip-1> please"
    assert redacted["mail"] == "<email-1>"
    assert redacted["api_username"] == "<redacted>"
    assert redacted["serialNumber"] == "<redacted>"
    assert redacted["userId"] == "<redacted>"
    assert redacted["deviceAccountId"] == "<redacted>"
    assert redacted["deviceId"] == "<redacted>"
    assert redacted["personId"] == "<redacted>"
    # Versions, timestamps and player names are not hostnames — untouched.
    assert redacted["version"] == "2.0.0"
    assert redacted["stamp"] == "2026-10-20T22:28:28+00:00"
    assert redacted["players"] == [{"name": "Office Echo", "state": "playing"}]


def test_provider_status_extracts_alexa_config():
    configs = [
        SimpleNamespace(
            domain="alexa",
            name="Alexa",
            enabled=True,
            status="loaded",
            last_error=None,
            instance_id="abc123",
            values={
                "username": SimpleNamespace(type="string", value="owner@example.com"),
                "password": SimpleNamespace(
                    type="secure_string", value="this_value_is_encrypted"
                ),
                "verbose_logging": SimpleNamespace(type="boolean", value=False),
            },
        ),
        SimpleNamespace(
            domain="spotify",
            name="Spotify",
            enabled=False,
            status="errored",
            last_error=SimpleNamespace(error_code=500, message="login failed"),
            instance_id="def456",
            values={},
        ),
    ]
    overview = status._provider_status(configs)
    assert overview["instances"][0] == {
        "domain": "alexa",
        "enabled": True,
        "status": "loaded",
        "last_error": None,
    }
    assert overview["instances"][1]["last_error"] == "login failed"
    masked = status._diagnostics_redact(overview)
    alexa = masked["alexa"][0]
    assert alexa["instance_id"] == "abc123"
    # Username and password never leave; harmless options pass through.
    assert alexa["config"]["username"] == "<redacted>"
    assert alexa["config"]["password"] == "<redacted>"
    assert alexa["config"]["verbose_logging"] is False
    assert alexa["secure_keys"] == ["password"]


def test_diagnostics_bundle_records_errors_instead_of_failing(client, monkeypatch):
    with app.test_request_context("/status"):
        data = status._diagnostics_bundle()
    # No MA credentials in the test env -> reported, not raised.
    assert data["music_assistant"] == {"configured": False}
    assert "phase" in data["deployment"]

    # A failing MA read becomes an error entry, not a failed download. The
    # entry carries the exception class name only — exception messages must
    # not reach served responses (CWE-209); the message goes to the log.
    async def boom(server_url, token):
        raise RuntimeError("MA unreachable")

    monkeypatch.setattr(status, "get_env_secret", lambda name: "set")
    monkeypatch.setattr(status, "_read_ma_status", boom)
    with app.test_request_context("/status"):
        data = status._diagnostics_bundle()
    assert data["music_assistant"] == {
        "configured": True,
        "error": "RuntimeError",
    }


def test_activity_fragment_and_count(client):
    with app.test_request_context("/status"):
        assert status._activity_fragment() == status.RECENT_ACTIVITY_EMPTY
        assert status._invocations_count() == 0
    app.config["INTENT_LOGS"] = [{"incoming": {}, "response": "{}", "ts": 1.0}]
    try:
        with app.test_request_context("/status"):
            assert status._invocations_count() == 1
            fragment = status._activity_fragment()
        assert "ma-timeline" in fragment
        assert "Alexa request" in fragment
    finally:
        app.config["INTENT_LOGS"] = []


def test_status_sub_endpoints(client):
    assert client.get("/status/ma", headers=AUTH).json["ma_api_html"]
    assert client.get("/status/alexa", headers=AUTH).json["alexa_api_html"]
    assert "metadata_html" in client.get("/status/metadata", headers=AUTH).json
    assert "count" in client.get("/status/invocations", headers=AUTH).json


def test_status_ma_and_alexa_show_pushed_streams(client):
    shared_store.set_ma({"streamUrl": "https://x/now-playing.mp3", "title": "T"})
    payload = client.get("/status/ma", headers=AUTH).json["ma_api_html"]
    assert "Active" in payload
    # Summary row only; the payload diagnostics were removed.
    assert "now-playing.mp3" not in payload
    shared_store.set_alexa({"streamUrl": "https://x/alexa.mp3"})
    payload = client.get("/status/alexa", headers=AUTH).json["alexa_api_html"]
    assert "Active" in payload
    assert "alexa.mp3" not in payload


def test_status_html_page(client):
    response = client.get("/status", headers=AUTH)
    assert response.status_code == 200
    assert response.mimetype == "text/html"
    body = response.get_data(as_text=True)
    # Approved hero + panels + readiness banner.
    assert "Service overview" in body
    assert "Your Alexa integration at a glance." in body
    assert "data-ma-refresh" in body
    assert "status-banner" in body
    assert "Connectivity" in body
    assert "Echo playback" in body
    assert "Music Assistant" in body
    assert "Checking account status" in body  # Amazon account row fills via the poller
    assert "Playback" in body
    assert "Echo Show display" in body
    assert "Recent activity" in body
    assert "ma-nav" in body
    assert '<a href="/status" aria-current="page">' in body
    for other in ("/setup", "/devices", "/invocations", "/simulator"):
        assert f'<a href="{other}" aria-current' not in body
    assert "__NAV__" not in body
    assert "__SKILL_HTML__" not in body
    assert "__MA_API_HTML__" not in body
    assert "__INVOCATIONS_HTML__" not in body


def test_test_audio_file_served(client):
    response = client.get("/test-audio.mp3")
    assert response.status_code == 200
    assert response.mimetype == "audio/mpeg"
    assert len(response.data) > 1000


def test_status_html_falls_back_when_template_missing(client, monkeypatch):
    def boom(*a, **kw):
        raise OSError("template gone")

    monkeypatch.setattr(status, "render_template", boom)
    response = client.get("/status", headers=AUTH)
    assert response.status_code == 200
    assert "Service Status" in response.get_data(as_text=True)


@pytest.mark.parametrize(
    "selector",
    [
        {"query_string": {"format": "json"}},
        {"headers": {**AUTH, "Accept": "application/json"}},
    ],
)
def test_status_json_variants(client, selector):
    headers = selector.get("headers", AUTH)
    response = client.get(
        "/status",
        headers=headers,
        query_string=selector.get("query_string"),
    )
    assert response.status_code == 200
    payload = response.json
    assert "Music Assistant" in payload["skill_html"]
    assert set(payload) == {
        "skill_html",
        "skill_ask_html",
        "ma_api_html",
        "alexa_api_html",
        "metadata_html",
        "invocations_html",
        "created",
    }


def test_status_api_and_ask_fragments(client):
    payload = client.get("/status/api", headers=AUTH).json
    assert "skill_ask_html" in payload
    assert "ma_api_html" in payload
    ask = client.get("/status/ask", headers=AUTH).json
    assert set(ask) == {"skill_ask_html"}


def test_status_urls_reports_reachability_snapshot(client, monkeypatch):
    snapshot = {
        "checked_at": 123.0,
        "checking": False,
        "checks": [
            {
                "label": "Stream host",
                "status": "reachable",
                "message": "HTTPS reachable",
                "url": "https://streams.example.com",
            }
        ],
        "perspective": "Checked from this add-on",
    }
    monkeypatch.setattr("reachability.snapshot", lambda: snapshot)
    monkeypatch.setattr("reachability.html", lambda result: "<div>urls-fragment</div>")
    payload = client.get("/status/urls", headers=AUTH).json
    assert payload["checked_at"] == 123.0
    assert payload["checks"][0]["label"] == "Stream host"
    assert payload["urls_html"] == "<div>urls-fragment</div>"
    # Readiness banner fields for the client-side poller.
    assert payload["summary_kind"] == "warning"
    assert payload["summary_title"]
    assert payload["summary_description"]
    assert payload["summary_icon"].startswith("<svg")
    assert "Stream host: reachable" in payload["conn_diag_html"]


def test_compute_metadata_html_rewrites_image_hosts(monkeypatch):
    from skill import data as skill_data

    monkeypatch.setattr(
        skill_data,
        "get_info",
        lambda: {
            "audioSources": "https://streams.example.com/music/song.mp3",
            "coverImageSource": "http://192.168.1.10:8095/imageproxy/art.jpg",
            "backgroundImageSource": "http://192.168.1.10:8095/imageproxy/bg.jpg",
            "primaryText": "Song",
        },
    )
    with app.test_request_context("/status"):
        html = status._compute_metadata_html()
    assert "Song" in html
    assert "streams.example.com/music/imageproxy/art.jpg" in html
    assert "192.168.1.10" not in html


def test_compute_metadata_html_swallows_hostname_errors(client, monkeypatch):
    # get_ma_hostname raising for both call styles -> original URLs shown.
    from skill import util as skill_util

    def boom(raise_on_http_scheme=True):
        raise RuntimeError("bad host")

    monkeypatch.setattr(skill_util, "get_ma_hostname", boom)
    with app.test_request_context("/status"):
        html = status._compute_metadata_html()
    assert '<pre class="ma-code"' in html
