import json

import pytest
import requests
from reachability import Reachability, probe, targets
from shared_store import set_ma


class Response:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self.body = body if body is not None else {"status": "ok"}

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
    result = probe("Skill", "https://example.com/", "skill", Session(error))
    assert result["status"] == "failed" and message in result["message"]


def test_wrong_route_and_audio_failure_are_not_green():
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
    monkeypatch.setenv("MA_HOSTNAME", "https://streams.example.com/music")
    set_ma({"url": "http://ma.local:8097/flow/track.mp3"})
    assert targets()[-1][1] == "https://streams.example.com/music/flow/track.mp3"


def test_checks_cached_and_status_does_not_block(monkeypatch):
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
    assert result["checked_at"] and len(result["checks"]) == 2
    old_thread = checker.thread
    checker.snapshot()
    assert checker.thread is old_thread


def test_status_html_preserves_markup_and_escapes_values():
    from reachability import html

    rendered = html(
        {
            "checks": [{"label": "<script>", "status": "failed", "message": "Failed"}],
            "checking": False,
            "perspective": "<img src=x>",
        }
    )
    assert '<div class="muted">' in rendered
    assert "&lt;script&gt;" in rendered and "&lt;img" in rendered
    assert "&lt;div" not in rendered


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
