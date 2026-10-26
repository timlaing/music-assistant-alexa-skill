import ipaddress
import json

import pytest
import requests
from reachability import Reachability, probe, targets
from shared_store import set_ma


class Response:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self.body = body if body is not None else {"status": "ok"}
        self.headers = (
            headers if headers is not None else {"Content-Type": "audio/mpeg"}
        )

    def iter_content(self, _size):
        yield json.dumps(self.body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass


class Session:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def test_skill_probe_checks_service_health_and_proxy_prefix():
    """Verify skill probe checks service health and proxy prefix."""
    session = Session(Response())
    result = probe("Skill", "https://example.com/ma-alexa-skill/", "skill", session)
    assert result["status"] == "reachable"
    assert session.calls[0][1] == "https://example.com/ma-alexa-skill/health"
    assert session.calls[0][2]["allow_redirects"] is False
    assert "headers" not in session.calls[0][2]


@pytest.mark.parametrize(
    "error,message",
    [
        (requests.exceptions.SSLError(), "TLS"),
        (requests.Timeout(), "timed out"),
        (requests.ConnectionError(), "DNS"),
    ],
)
def test_network_errors_reported_without_exception_text(error, message):
    """Verify network errors reported without exception text."""
    result = probe("Skill", "https://example.com/", "skill", Session(error))
    assert result["status"] == "failed"
    assert message in result["message"]


def test_wrong_route_and_audio_failure_are_not_green():
    """Verify wrong route and audio failure are not green."""
    assert (
        probe(
            "Skill",
            "https://example.com/",
            "skill",
            Session(Response(body={"other": True})),
        )["status"]
        == "warning"
    )
    assert (
        probe(
            "Audio",
            "https://example.com/track?secret=token",
            "audio",
            Session(Response(404)),
        )["status"]
        == "failed"
    )
    assert (
        probe("Host", "https://example.com/", "host", Session(Response(404)))["status"]
        == "warning"
    )
    assert (
        "secret"
        not in probe(
            "Audio",
            "https://example.com/track?secret=token",
            "audio",
            Session(Response()),
        )["url"]
    )


def test_actual_stream_url_uses_configured_public_base(monkeypatch):
    """Verify actual stream url uses configured public base."""
    monkeypatch.setenv("MA_HOSTNAME", "https://streams.example.com/music")
    set_ma({"url": "http://ma.local:8097/flow/track.mp3"})
    assert targets()[-1][1] == "https://streams.example.com/music/flow/track.mp3"


def test_current_audio_target_reads_stream_url_key(monkeypatch):
    """Verify the streamUrl store key drives the current-audio target."""
    monkeypatch.setenv("MA_HOSTNAME", "https://streams.example.com/music")
    set_ma(
        {
            "streamUrl": "http://ma.local:8097/flow/track.mp3",
            "url": "http://ma.local:8097/older/track.mp3",
        }
    )
    assert (
        "Current audio",
        "https://streams.example.com/music/flow/track.mp3",
        "audio",
    ) in targets()


def test_testaudio_target_follows_skill_host(monkeypatch):
    """Verify testaudio target follows skill host."""
    monkeypatch.setenv("SKILL_HOSTNAME", "https://example.com/ma-alexa-skill")
    assert (
        "Public audio delivery",
        "https://example.com/ma-alexa-skill",
        "testaudio",
    ) in targets()


def test_testaudio_probe_checks_content_type():
    """Verify testaudio probe checks content type."""
    ok = probe(
        "Public audio delivery",
        "https://example.com/ma-alexa-skill/",
        "testaudio",
        Session(Response()),
    )
    assert ok["status"] == "reachable"
    assert ok["kind"] == "testaudio"
    assert ok["url"] == "https://example.com/ma-alexa-skill/test-audio.mp3"
    wrong_type = probe(
        "Public audio delivery",
        "https://example.com/",
        "testaudio",
        Session(Response(headers={"Content-Type": "text/html"})),
    )
    assert wrong_type["status"] == "warning"
    missing = probe(
        "Public audio delivery",
        "https://example.com/",
        "testaudio",
        Session(Response(404)),
    )
    assert missing["status"] == "failed"


def test_testaudio_html_embeds_player():
    """Verify testaudio html embeds player."""
    from reachability import html

    rendered = html(
        {
            "checks": [
                {
                    "label": "Public audio delivery",
                    "kind": "testaudio",
                    "status": "reachable",
                    "message": "ok",
                    "url": "https://example.com/test-audio.mp3",
                }
            ],
            "checking": False,
            "perspective": "p",
        }
    )
    assert "<audio controls" in rendered
    assert 'src="https://example.com/test-audio.mp3"' in rendered


def test_testaudio_probe_streams_get_and_requires_data():
    """Verify testaudio probe streams get and requires data."""
    session = Session(Response())
    ok = probe("Public audio delivery", "https://example.com/", "testaudio", session)
    assert session.calls[0][0] == "GET"
    assert ok["status"] == "reachable"

    class EmptyAudio(Response):
        def iter_content(self, _size):
            return iter(())

    empty = probe(
        "Public audio delivery",
        "https://example.com/",
        "testaudio",
        Session(EmptyAudio()),
    )
    assert empty["status"] == "failed"

    no_content = probe(
        "Public audio delivery",
        "https://example.com/",
        "testaudio",
        Session(Response(204)),
    )
    assert no_content["status"] == "failed"

    audio_session = Session(Response())
    probe(
        "Current audio",
        "https://streams.example.com/track.mp3",
        "audio",
        audio_session,
    )
    assert audio_session.calls[0][0] == "HEAD"


def test_checks_cached_and_status_does_not_block(monkeypatch):
    """Verify checks cached and status does not block."""
    import reachability

    class FakeSession(Session):
        def __init__(self):
            super().__init__(Response())

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

    monkeypatch.setattr(reachability.requests, "Session", FakeSession)
    monkeypatch.setenv("SKILL_HOSTNAME", "https://example.com/")
    checker = Reachability()
    checker.snapshot()
    checker.thread.join(2)
    result = checker.snapshot()
    assert result["checked_at"]
    assert len(result["checks"]) == 3
    old_thread = checker.thread
    checker.snapshot()
    assert checker.thread is old_thread


def test_status_html_preserves_markup_and_escapes_values():
    """Verify status html preserves markup and escapes values."""
    from markupsafe import escape

    from reachability import diagnostics, html

    result = {
        "checks": [{"label": "<script>", "status": "failed", "message": "Failed"}],
        "checking": False,
        "perspective": "<img src=x>",
    }
    rendered = html(result)
    assert "&lt;script&gt;" in rendered  # row title is escaped
    assert "&lt;div" not in rendered  # markup stays intact
    diag = str(escape(diagnostics(result)))
    assert "&lt;img" in diag  # perspective is escaped at render time
    assert "&lt;div" not in diag


def test_summary_readiness_states():
    """Verify summary readiness states."""
    from reachability import summary

    base = {"checked_at": 1.0, "checking": False, "checks": [], "perspective": "p"}
    ready = dict(
        base,
        checks=[
            {"label": "Skill endpoint", "status": "reachable", "message": "ok"},
            {"label": "Public audio delivery", "status": "reachable", "message": "ok"},
        ],
    )
    state = summary(ready)
    assert state["kind"] == "good"
    assert state["title"] == "Service is ready for an Echo test"
    failing = dict(
        base,
        checks=[
            {"label": "Skill endpoint", "status": "failed", "message": "DNS"},
            {"label": "Public audio delivery", "status": "reachable", "message": "ok"},
        ],
    )
    assert summary(failing)["kind"] == "error"
    assert "1 of 2" in summary(failing)["description"]
    unconfigured = dict(
        base,
        checks=[
            {"label": "Skill endpoint", "status": "unconfigured", "message": "n/a"},
            {
                "label": "Public audio delivery",
                "status": "unconfigured",
                "message": "n/a",
            },
        ],
    )
    assert summary(unconfigured)["kind"] == "warning"
    checking = dict(base, checked_at=None, checking=True, checks=[])
    assert summary(checking)["kind"] == "neutral"


@pytest.mark.parametrize(
    "stream,url",
    [
        ("http://streams.example.com", "http://ma.local/track"),
        ("https://streams.example.com", "file:///private/track"),
        ("https://streams.example.com", {"invalid": "type"}),
    ],
)
def test_invalid_rewrite_still_reports_failed_audio_check(
    client, monkeypatch, stream, url
):
    """Verify invalid rewrite still reports failed audio check."""
    import reachability
    from app import app
    from test_skill_deployment import AUTH

    class FakeSession(Session):
        def __init__(self):
            super().__init__(Response())

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

    monkeypatch.setenv("MA_HOSTNAME", stream)
    set_ma({"url": url})
    monkeypatch.setattr(reachability.requests, "Session", FakeSession)
    app.extensions["reachability"] = Reachability()
    assert client.get("/status/urls", headers=AUTH).status_code == 200
    app.extensions["reachability"].thread.join(2)
    response = client.get("/status/urls", headers=AUTH)
    assert response.status_code == 200
    audio = response.json["checks"][-1]
    assert audio["label"] == "Current audio"
    assert audio["status"] == "failed"
    assert "Invalid HTTPS URL" in audio["message"]


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("host", "reachable"),
        ("skill", "failed"),
        ("audio", "failed"),
        ("testaudio", "failed"),
    ],
)
def test_method_not_allowed_is_success_only_for_stream_host(kind, expected):
    """Verify method not allowed is success only for stream host."""
    session = Session(Response(405))
    result = probe("Check", "https://example.com/", kind, session)
    assert result["status"] == expected
    assert result["http_status"] == 405
    if kind == "host":
        assert session.calls[0][0] == "HEAD"
        assert "HEAD not supported" in result["message"]


# --- pushed stream URL destination guard -----------------------------------


def _audio_probe_with_views(monkeypatch, local_view, public_view):
    """Probe a pushed stream URL with stubbed local and DoH resolution."""
    import reachability

    monkeypatch.setattr(reachability, "_resolved_addresses", lambda _host: local_view)
    monkeypatch.setattr(reachability, "_public_addresses", lambda _host: public_view)
    session = Session(Response())
    result = probe(
        "Current audio", "https://streams.example/flow/track.mp3", "audio", session
    )
    return result, session


def test_audio_probe_allows_split_dns_public_view(monkeypatch):
    """A privately-resolving host with a public DNS view still gets probed."""
    result, session = _audio_probe_with_views(
        monkeypatch,
        {ipaddress.ip_address("10.0.0.5")},
        {ipaddress.ip_address("93.184.216.34")},
    )
    assert result["status"] == "reachable"
    assert session.calls and session.calls[0][0] == "HEAD"


def test_audio_probe_refuses_internal_only_stream_url(monkeypatch):
    """A host private on both views never receives a request from this add-on."""
    result, session = _audio_probe_with_views(
        monkeypatch, {ipaddress.ip_address("10.0.0.5")}, set()
    )
    assert result["status"] == "warning"
    assert "private or internal" in result["message"]
    assert session.calls == []


def test_audio_probe_proceeds_when_doh_unavailable(monkeypatch):
    """An unreachable resolver degrades to probing, never to blocking."""
    result, session = _audio_probe_with_views(
        monkeypatch, {ipaddress.ip_address("10.0.0.5")}, None
    )
    assert result["status"] == "reachable"
    assert session.calls


def test_public_addresses_reads_doh_answers_and_falls_back(monkeypatch):
    """DoH answers are parsed per record type; a failing resolver moves on."""
    import reachability

    queries = []

    class DohResponse:
        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    class DohSession:
        def __init__(self, fail):
            self.fail = fail

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def get(self, endpoint, params=None, **_kwargs):
            queries.append((endpoint, params["type"]))
            if self.fail:
                raise requests.ConnectionError()
            answers = (
                [{"data": "93.184.216.34"}]
                if params["type"] == "A"
                else [{"data": "2606:2800:220:1:248:1893:25c8:1946"}]
            )
            return DohResponse({"Status": 0, "Answer": answers})

    monkeypatch.setattr(reachability.requests, "Session", lambda: DohSession(fail=True))
    assert reachability._public_addresses("streams.example") is None
    assert queries[-1][0] == reachability._DOH_ENDPOINTS[-1]  # fell back to Google

    monkeypatch.setattr(reachability.requests, "Session", lambda: DohSession(False))
    assert reachability._public_addresses("streams.example") == {
        ipaddress.ip_address("93.184.216.34"),
        ipaddress.ip_address("2606:2800:220:1:248:1893:25c8:1946"),
    }
