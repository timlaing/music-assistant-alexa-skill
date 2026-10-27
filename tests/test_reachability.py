import ipaddress
import json
import socket
import time

import pytest
import reachability
import requests
from reachability import Reachability, probe, targets
from shared_store import set_ma


@pytest.fixture(autouse=True)
def fixed_dns(monkeypatch):
    """Resolve every test host to one public address — no live DNS.

    probe() preflight-resolves each target and pins its request to that
    answer, so the tests must own the lookup instead of depending on the
    network being reachable.
    """
    answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
    monkeypatch.setattr(
        reachability.socket, "getaddrinfo", lambda *_args, **_kwargs: answer
    )


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
        self.mounts = []

    def mount(self, prefix, adapter):
        self.mounts.append((prefix, adapter))

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
    split_horizon = dict(
        base,
        checks=[
            {
                "label": "Skill endpoint",
                "status": "warning",
                "message": "ok",
                "note": "Resolves internally (LAN) — this check does not test the public/Echo path",
            },
            {"label": "Public audio delivery", "status": "reachable", "message": "ok"},
        ],
    )
    assert summary(split_horizon)["kind"] == "error"  # never a false green
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


# --- resolution views: pushed-URL guard + split-horizon note (issue #49) -----


def _audio_probe_with_views(monkeypatch, local_view, public_view, kind="audio"):
    """Probe a target with stubbed local and DoH resolution."""
    import reachability

    monkeypatch.setattr(reachability, "_resolved_addresses", lambda _host: local_view)
    monkeypatch.setattr(reachability, "_public_addresses", lambda _host: public_view)
    session = Session(Response())
    result = probe(
        "Current audio", "https://streams.example/flow/track.mp3", kind, session
    )
    return result, session


def test_audio_probe_allows_split_dns_public_view(monkeypatch):
    """A privately-resolving host with a public DNS view still gets probed."""
    result, session = _audio_probe_with_views(
        monkeypatch,
        {ipaddress.ip_address("10.0.0.5")},
        {ipaddress.ip_address("93.184.216.34")},
    )
    assert session.calls
    assert session.calls[0][0] == "HEAD"
    # Issue #49: the probe only exercised the LAN rewrite, so it must not
    # report a plain pass.
    assert result["status"] == "warning"
    assert result["note"] == (
        "Resolves internally (LAN) — this check does not test the public/Echo path"
    )


def test_skill_probe_warns_when_only_the_local_view_is_private(monkeypatch):
    """The split-horizon note applies to every kind, not just stream URLs."""
    result, session = _audio_probe_with_views(
        monkeypatch,
        {ipaddress.ip_address("192.168.1.10")},
        {ipaddress.ip_address("93.184.216.34")},
        kind="skill",
    )
    assert result["status"] == "warning"
    assert "Resolves internally (LAN)" in result["note"]
    assert session.calls  # probing continues — only the plain pass is denied


def test_public_local_resolution_passes_plainly_without_doh(monkeypatch):
    """A host that resolves publicly locally is a plain pass; no DoH query."""
    import reachability

    def _no_doh(_host):
        pytest.fail("public local resolution must never trigger a DoH query")

    monkeypatch.setattr(
        reachability,
        "_resolved_addresses",
        lambda _host: {ipaddress.ip_address("93.184.216.34")},
    )
    monkeypatch.setattr(reachability, "_public_addresses", _no_doh)
    result = probe("Skill", "https://example.com/", "skill", Session(Response()))
    assert result["status"] == "reachable"
    assert "note" not in result


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
    assert "note" not in result
    assert session.calls


def test_split_horizon_note_renders_as_warning_not_green():
    """The note renders in the row and diagnostics; the pill is not green."""
    from reachability import diagnostics, html

    result = {
        "checks": [
            {
                "label": "Skill endpoint",
                "status": "warning",
                "message": "HTTPS reachable; add-on health confirmed",
                "note": "Resolves internally (LAN) — this check does not test the public/Echo path",
                "url": "https://example.com/ma-alexa-skill/health",
            }
        ],
        "checking": False,
        "perspective": "p",
    }
    rendered = html(result)
    assert "ma-state ma-warning" in rendered  # yellow pill, not success
    assert "ma-state ma-ok" not in rendered
    assert "Resolves internally (LAN)" in rendered
    assert "[Resolves internally (LAN)" in diagnostics(result)


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


def test_doh_malformed_payload_is_treated_as_resolver_failure(monkeypatch):
    """A non-object JSON answer fails that resolver and falls back."""
    import reachability

    class DohResponse:
        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    class DohSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def get(self, endpoint, params=None, **_kwargs):
            if endpoint == reachability._DOH_ENDPOINTS[0]:
                return DohResponse(["not", "an", "object"])  # malformed
            answers = [{"data": "93.184.216.34"}] if params["type"] == "A" else []
            return DohResponse({"Status": 0, "Answer": answers})

    monkeypatch.setattr(reachability.requests, "Session", lambda: DohSession())
    # The first resolver answers with malformed JSON; the probe must treat it
    # as a failure and use the second resolver's valid answer instead of
    # raising AttributeError and aborting the check run.
    assert reachability._public_addresses("streams.example") == {
        ipaddress.ip_address("93.184.216.34")
    }


def test_doh_malformed_answer_is_treated_as_resolver_failure(monkeypatch):
    """A non-list Answer field fails that resolver and falls back."""
    import reachability

    class DohResponse:
        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    class DohSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def get(self, endpoint, params=None, **_kwargs):
            if endpoint == reachability._DOH_ENDPOINTS[0]:
                return DohResponse({"Status": 0, "Answer": "not-a-list"})
            answers = [{"data": "93.184.216.34"}] if params["type"] == "A" else []
            return DohResponse({"Status": 0, "Answer": answers})

    monkeypatch.setattr(reachability.requests, "Session", lambda: DohSession())
    assert reachability._public_addresses("streams.example") == {
        ipaddress.ip_address("93.184.216.34")
    }


def test_lan_only_names_never_reach_public_resolvers(monkeypatch):
    """An internal-only hostname is not sent to Cloudflare/Google DoH."""
    import reachability

    def _no_doh(_host):
        pytest.fail("LAN-only names must never trigger a DoH query")

    private = {ipaddress.ip_address("192.168.1.10")}
    monkeypatch.setattr(reachability, "_public_addresses", _no_doh)
    monkeypatch.setattr(reachability, "_resolved_addresses", lambda _host: private)
    # .local/.lan/single-label names and private IP literals all skip DoH and
    # record an empty public view (a definitive "none"), never None.
    for hostname in (
        "homeassistant.local",
        "music-assistant",
        "haos.lan.",
        "192.168.1.50",
    ):
        local, public = reachability._resolution_views(hostname)
        assert local == private
        assert public == set()  # no DoH query; a definitive "no public view"


def test_resolution_views_are_cached_per_run(monkeypatch):
    """Checks sharing a hostname resolve it once, not once each."""
    import reachability

    local_lookups = []
    doh_lookups = []

    def _local(host):
        local_lookups.append(host)
        return {ipaddress.ip_address("10.0.0.5")}

    def _doh(host):
        doh_lookups.append(host)
        return {ipaddress.ip_address("93.184.216.34")}

    monkeypatch.setattr(reachability, "_resolved_addresses", _local)
    monkeypatch.setattr(reachability, "_public_addresses", _doh)
    cache = {}
    first = reachability._resolution_views("streams.example", cache)
    second = reachability._resolution_views("streams.example", cache)
    assert first == second
    assert doh_lookups == ["streams.example"]  # one DoH query
    assert local_lookups == ["streams.example"]  # and one local lookup


def test_audio_probe_blocks_lan_only_host_without_doh(monkeypatch):
    """A LAN-only audio host is blocked without ever querying public DNS."""
    import reachability

    def _no_doh(_host):
        pytest.fail("LAN-only names must never trigger a DoH query")

    monkeypatch.setattr(
        reachability,
        "_resolved_addresses",
        lambda _host: {ipaddress.ip_address("192.168.1.10")},
    )
    monkeypatch.setattr(reachability, "_public_addresses", _no_doh)
    session = Session(Response())
    result = probe("Current audio", "https://hass.local/track.mp3", "audio", session)
    assert result["status"] == "warning"
    assert "private or internal" in result["message"]
    assert session.calls == []  # never probed, and no DoH query


def test_resolved_addresses_bounds_a_stalled_resolver(monkeypatch):
    """A stalled system resolver times out instead of hanging the check."""
    import reachability

    def _stall(*_args, **_kwargs):
        time.sleep(reachability._DNS_TIMEOUT_SECONDS + 0.3)
        raise OSError("simulated resolver stall")

    monkeypatch.setattr(reachability, "_DNS_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(reachability.socket, "getaddrinfo", _stall)
    monkeypatch.setattr(reachability, "_dns_inflight", {})
    # A timeout is distinct from "does not resolve": the probe must fail the
    # check rather than fire a second DNS lookup (issue #49 review).
    assert (
        reachability._resolved_addresses("example.com") is reachability._DNS_TIMED_OUT
    )


def test_summary_flags_internal_resolution_on_secondary_checks():
    """Skill + audio green cannot mask an internally-resolving stream host."""
    from reachability import summary

    base = {"checked_at": 1.0, "checking": False, "perspective": "p"}
    note = "Resolves internally (LAN) — this check does not test the public/Echo path"
    result = dict(
        base,
        checks=[
            {"label": "Skill endpoint", "status": "reachable", "message": "ok"},
            {"label": "Public audio delivery", "status": "reachable", "message": "ok"},
            {
                "label": "Stream host",
                "status": "warning",
                "message": "ok",
                "note": note,
                # A confirmed pass that only resolves internally: the note
                # branch reports the unverified public path (issue #49 review).
                "downgraded": True,
            },
        ],
    )
    state = summary(result)
    assert state["kind"] == "warning"  # not a false "good"
    assert "resolve internally" in state["title"]


def test_testaudio_player_survives_split_horizon_downgrade():
    """A downgraded split-horizon test tone keeps its player."""
    from reachability import html

    note = "Resolves internally (LAN) — this check does not test the public/Echo path"
    # A genuine split-horizon pass: the bytes were confirmed, then probe()
    # downgraded the check to a warning and set downgraded=True.
    downgraded = {
        "label": "Public audio delivery",
        "kind": "testaudio",
        "status": "warning",
        "message": "ok",
        "note": note,
        "url": "https://example.com/test-audio.mp3",
        "downgraded": True,
    }
    rendered = html({"checks": [downgraded], "checking": False, "perspective": "p"})
    assert "<audio controls" in rendered  # confirmed audio keeps its player

    # An ordinary warning from a non-test-audio check never verified the
    # bytes, so it gets no player — this must fail if html() starts rendering
    # a player for every warning (issue #48/#49 review).
    non_audio = dict(downgraded, kind=None, downgraded=None, message="not audio")
    assert "<audio controls" not in html(
        {"checks": [non_audio], "checking": False, "perspective": "p"}
    )


# --- review follow-up: DNS timeout, LAN-only green, player, summary ---------


def test_probe_fails_fast_when_local_dns_times_out(monkeypatch):
    """A stalled local resolver fails the check without a second DNS lookup."""
    import reachability

    monkeypatch.setattr(
        reachability, "_resolved_addresses", lambda _host: reachability._DNS_TIMED_OUT
    )
    session = Session(Response())
    result = probe("Skill", "https://example.com/", "skill", session)
    assert result["status"] == "failed"
    assert "timed out" in result["message"]
    assert session.calls == []  # never sends a request that would re-resolve


def test_stalled_resolver_worker_is_reused_not_leaked(monkeypatch):
    """A stalled resolver reuses one worker instead of leaking one per run."""
    import threading as _threading

    import reachability

    created = []
    real_thread = _threading.Thread
    release = _threading.Event()  # holds the worker blocked until we're done

    def _counting_thread(*args, **kwargs):
        thread = real_thread(*args, **kwargs)
        created.append(thread)
        return thread

    def _stall(*_args, **_kwargs):
        release.wait()  # stay blocked until the test releases us (no timeout)
        raise OSError("simulated resolver stall")

    monkeypatch.setattr(reachability, "_DNS_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(reachability.socket, "getaddrinfo", _stall)
    monkeypatch.setattr(reachability, "_dns_inflight", {})
    monkeypatch.setattr(reachability.threading, "Thread", _counting_thread)
    try:
        assert (
            reachability._resolved_addresses("stalled.example")
            is reachability._DNS_TIMED_OUT
        )
        assert (
            reachability._resolved_addresses("stalled.example")
            is reachability._DNS_TIMED_OUT
        )
        # Two lookups, one worker: a stalled resolver is not leaked per run.
        assert len(created) == 1
    finally:
        release.set()  # let the blocked worker finish cleanly
        reachability._dns_inflight.clear()


def test_could_have_public_view_rejects_reserved_names():
    """IANA-reserved/special-use names never reach a public resolver."""
    import reachability

    # ".example" is deliberately absent: the test suite uses it as the mock
    # public namespace (e.g. streams.example).
    for hostname in ("host.test", "foo.onion", "x.invalid"):
        assert reachability._could_have_public_view(hostname) is False


def test_could_have_public_view_rejects_legacy_ip_forms():
    """Legacy IPv4 spellings resolvers accept never reach DoH."""
    import reachability

    for hostname in ("127.1", "10.0.0.1", "192.168.1.50%eth0"):
        assert reachability._could_have_public_view(hostname) is False


def test_skill_probe_flags_empty_public_view(monkeypatch):
    """A skill host with an empty public view is flagged internal, not green."""
    result, session = _audio_probe_with_views(
        monkeypatch,
        {ipaddress.ip_address("10.0.0.5")},
        set(),
        kind="skill",
    )
    assert session.calls  # still probed, but never a plain green pass
    assert result["status"] == "warning"
    assert "Resolves internally (LAN)" in result["note"]


def test_summary_surfaces_failed_check_alongside_reachable_skill_audio():
    """A failed secondary check is not masked by skill+audio being green."""
    from reachability import summary

    base = {"checked_at": 1.0, "checking": False, "perspective": "p"}
    note = "Resolves internally (LAN) — this check does not test the public/Echo path"
    result = dict(
        base,
        checks=[
            {"label": "Skill endpoint", "status": "reachable", "message": "ok"},
            {"label": "Public audio delivery", "status": "reachable", "message": "ok"},
            {
                "label": "Stream host",
                "status": "failed",
                "message": "DNS",
                "note": note,
            },
        ],
    )
    state = summary(result)
    assert state["kind"] == "warning"
    assert "1 of 3" in state["description"]
    assert "failed" in state["description"]
    assert "resolve internally" not in state["title"]


def test_testaudio_non_audio_warning_gets_no_player(monkeypatch):
    """A non-audio warning with a split-horizon note renders no player."""
    import reachability
    from reachability import html

    monkeypatch.setattr(
        reachability,
        "_resolved_addresses",
        lambda _host: {ipaddress.ip_address("10.0.0.5")},
    )
    monkeypatch.setattr(
        reachability,
        "_public_addresses",
        lambda _host: {ipaddress.ip_address("93.184.216.34")},
    )
    session = Session(Response(headers={"Content-Type": "text/html"}))
    result = probe(
        "Public audio delivery", "https://example.com/", "testaudio", session
    )
    assert result["status"] == "warning"  # reachable but not audio
    assert result.get("note")  # split-horizon note present
    assert "downgraded" not in result  # audio never confirmed
    rendered = html({"checks": [result], "checking": False, "perspective": "p"})
    assert "<audio controls" not in rendered


def test_summary_treats_downgraded_split_horizon_as_warning_not_error():
    """A split-horizon pass on skill/test-tone is a warning, not a red error."""
    from reachability import summary

    base = {"checked_at": 1.0, "checking": False, "perspective": "p"}
    note = "Resolves internally (LAN) — this check does not test the public/Echo path"
    result = dict(
        base,
        checks=[
            {
                "label": "Skill endpoint",
                "status": "warning",
                "message": "ok",
                "note": note,
                "downgraded": True,
            },
            {
                "label": "Public audio delivery",
                "status": "warning",
                "message": "ok",
                "note": note,
                "downgraded": True,
            },
        ],
    )
    state = summary(result)
    assert state["kind"] == "warning"  # not a red "error"
    assert "resolve internally" in state["title"]


# --- request pinning + bounded DNS workers (issue #49 review) ---------------


def test_pinned_adapter_targets_the_preflight_address():
    """The pool connects to the validated address; SNI/cert keep the host."""
    from reachability import _PinnedAdapter

    request = requests.Request("HEAD", "https://example.com/x").prepare()
    adapter = _PinnedAdapter("example.com", ipaddress.ip_address("93.184.216.34"))
    try:
        host_params, pool_kwargs = adapter.build_connection_pool_key_attributes(
            request, True
        )
    finally:
        adapter.close()
    assert host_params["host"] == "93.184.216.34"
    assert pool_kwargs["server_hostname"] == "example.com"
    assert pool_kwargs["assert_hostname"] == "example.com"


def test_probe_fails_over_to_the_next_validated_address(monkeypatch):
    """A dead first address falls back instead of re-resolving the host."""
    monkeypatch.setattr(
        reachability,
        "_resolved_addresses",
        lambda _host: {
            ipaddress.ip_address("8.8.8.8"),
            ipaddress.ip_address("93.184.216.34"),
        },
    )

    class FlakySession(Session):
        def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            if len(self.calls) == 1:
                raise requests.ConnectionError()
            return self.response

    session = FlakySession(Response())
    result = probe("Skill", "https://example.com/", "skill", session)
    assert result["status"] == "reachable"
    # 8.8.8.8 sorts first and fails; the next validated address is tried —
    # each attempt pinned, none of them re-resolving the host name.
    assert len(session.calls) == 2
    assert len(session.mounts) == 2


def test_probe_does_not_re_resolve_an_unresolved_host(monkeypatch):
    """A preflight miss skips the request instead of repeating the lookup."""
    monkeypatch.setattr(reachability, "_resolved_addresses", lambda _host: None)
    session = Session(Response())
    result = probe("Skill", "https://example.com/", "skill", session)
    assert result["status"] == "failed"
    assert "DNS" in result["message"]
    assert session.calls == []  # no request, so no second DNS lookup
    assert session.mounts == []


def test_distinct_stalled_hostnames_stop_at_the_worker_cap(monkeypatch):
    """The resolver-worker pool is bounded, not one blocked thread per name."""
    import threading as _threading

    created = []
    real_thread = _threading.Thread
    release = _threading.Event()

    def _counting_thread(*args, **kwargs):
        thread = real_thread(*args, **kwargs)
        created.append(thread)
        return thread

    def _stall(*_args, **_kwargs):
        release.wait()
        raise OSError("simulated resolver stall")

    monkeypatch.setattr(reachability, "_DNS_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(reachability.socket, "getaddrinfo", _stall)
    monkeypatch.setattr(reachability, "_dns_inflight", {})
    monkeypatch.setattr(reachability, "_MAX_DNS_WORKERS", 1)
    monkeypatch.setattr(reachability.threading, "Thread", _counting_thread)
    try:
        assert (
            reachability._resolved_addresses("one.example")
            is reachability._DNS_TIMED_OUT
        )
        # A full pool is reported distinctly from one stalled answer
        # (issue #49 review), and still never spawns an extra worker.
        assert (
            reachability._resolved_addresses("two.example")
            is reachability._DNS_UNAVAILABLE
        )
        assert len(created) == 1  # the cap held: no second blocked worker
    finally:
        release.set()
        for worker in created:
            worker.join(2)
        reachability._dns_inflight.clear()


def test_resolver_worker_clears_its_entry_when_it_finishes(monkeypatch):
    """A worker that outlives its caller still drops its in-flight entry."""
    import threading as _threading

    release = _threading.Event()

    def _stall(*_args, **_kwargs):
        release.wait()
        raise OSError("simulated resolver stall")

    monkeypatch.setattr(reachability, "_DNS_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(reachability.socket, "getaddrinfo", _stall)
    monkeypatch.setattr(reachability, "_dns_inflight", {})
    try:
        assert (
            reachability._resolved_addresses("late.example")
            is reachability._DNS_TIMED_OUT
        )
        assert reachability._dns_inflight  # stalled: kept for reuse while alive
        release.set()
        deadline = time.time() + 2
        while reachability._dns_inflight and time.time() < deadline:
            time.sleep(0.01)
        assert not reachability._dns_inflight  # self-cleaned after the timeout
    finally:
        release.set()


def test_summary_treats_plain_warning_as_needing_attention():
    """Skill + audio green cannot mask a secondary warning without a note."""
    from reachability import summary

    base = {"checked_at": 1.0, "checking": False, "perspective": "p"}
    result = dict(
        base,
        checks=[
            {"label": "Skill endpoint", "status": "reachable", "message": "ok"},
            {"label": "Public audio delivery", "status": "reachable", "message": "ok"},
            {
                "label": "Current audio",
                "status": "warning",
                "message": (
                    "Stream URL resolves only to private or internal addresses; "
                    "Alexa cannot fetch it from the internet"
                ),
            },
        ],
    )
    state = summary(result)
    assert state["kind"] == "warning"  # not a false "good"
    assert "1 of 3" in state["description"]
    assert "warning" in state["description"]


def test_summary_keeps_a_noted_warning_in_the_attention_branch():
    """A noted endpoint problem (e.g. HTTP 404) must not hide behind the note."""
    from reachability import summary

    base = {"checked_at": 1.0, "checking": False, "perspective": "p"}
    note = "Resolves internally (LAN) — this check does not test the public/Echo path"
    result = dict(
        base,
        checks=[
            {"label": "Skill endpoint", "status": "reachable", "message": "ok"},
            {"label": "Public audio delivery", "status": "reachable", "message": "ok"},
            {
                "label": "Stream host",
                "status": "warning",
                "message": "HTTPS reachable (HTTP 404); test an actual audio URL",
                "note": note,
                # Not downgraded: the endpoint itself warned, so the banner
                # must surface it instead of the generic note (issue #49 review).
            },
        ],
    )
    state = summary(result)
    assert state["kind"] == "warning"  # not a false "good"
    assert "need attention" in state["title"]  # not only "resolve internally"
    assert "1 of 3" in state["description"]


def test_failover_never_starts_an_attempt_without_budget(monkeypatch):
    """No address may start a fresh timeout once the budget is spent."""
    monkeypatch.setattr(reachability, "_FAILOVER_BUDGET_SECONDS", 0.0)
    monkeypatch.setattr(
        reachability,
        "_resolved_addresses",
        lambda _host: {
            ipaddress.ip_address("8.8.8.8"),
            ipaddress.ip_address("93.184.216.34"),
            ipaddress.ip_address("1.1.1.1"),
        },
    )

    class DeadSession(Session):
        def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            raise requests.ConnectionError()

    session = DeadSession(Response())
    result = probe("Skill", "https://example.com/", "skill", session)
    assert result["status"] == "failed"
    assert "timed out" in result["message"]
    # The budget is already spent, so not even the first validated address
    # gets a request (issue #53 review).
    assert session.calls == []


class SteppingTime:
    """Stand-in for reachability's ``time`` module with a deterministic clock.

    Each ``monotonic()`` call returns the current mark and advances it by
    ``step``, so deadline arithmetic in the failover loop is exact instead
    of depending on how fast the test process runs. ``time.time()`` keeps
    answering with the real clock for the snapshot paths.
    """

    def __init__(self, step, start=1000.0):
        self.now = start
        self.step = step

    def monotonic(self):
        current = self.now
        self.now += self.step
        return current

    def time(self):
        return time.time()


def test_failover_stops_once_a_failure_spends_the_budget(monkeypatch):
    """The first failure can spend the budget: later addresses never start."""
    monkeypatch.setattr(
        reachability,
        "_resolved_addresses",
        lambda _host: {
            ipaddress.ip_address("8.8.8.8"),
            ipaddress.ip_address("93.184.216.34"),
            ipaddress.ip_address("1.1.1.1"),
        },
    )
    # Each clock call advances 6s, so the 10s budget still covers the first
    # attempt but is spent once that attempt fails — without relying on the
    # real elapsed time of the test process (issue #53 review).
    monkeypatch.setattr(reachability, "time", SteppingTime(step=6.0))

    class DeadSession(Session):
        def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            raise requests.ConnectionError()

    session = DeadSession(Response())
    result = probe("Skill", "https://example.com/", "skill", session)
    assert result["status"] == "failed"
    # The budget is spent after the first failure, so the two remaining
    # validated addresses never get their own fresh timeout (issue #49 review).
    assert len(session.calls) == 1


def test_attempt_timeouts_fit_the_remaining_failover_budget(monkeypatch):
    """A fresh (3, 5) per address is scaled down to the budget left."""
    monkeypatch.setattr(reachability, "_FAILOVER_BUDGET_SECONDS", 4.0)
    session = Session(Response())
    result = probe("Skill", "https://example.com/", "skill", session)
    assert result["status"] == "reachable"
    connect, read = session.calls[0][2]["timeout"]
    # Fresh (3, 5) timeouts total 8s and could overrun a 4s budget on their
    # own, so both are scaled to what is left: connect + read never exceeds
    # it, and neither is the unbounded fresh timeout (issue #53 review).
    assert connect + read <= 4.0
    assert connect < 3.0 and read < 5.0


def test_health_read_stops_at_the_deadline_before_the_next_chunk():
    """No blocking read starts once the shared deadline has passed."""

    class CountingResponse(Response):
        def __init__(self):
            super().__init__()
            self.consumed = 0

        def iter_content(self, _size):
            for _ in range(100):
                self.consumed += 1
                yield b"x"

    late = CountingResponse()
    content = reachability._read_health_content(late, deadline=time.monotonic() - 1)
    assert content == b""
    assert late.consumed == 0  # the generator was never asked for a chunk
    # Without a deadline the body is read exactly as before (up to the cap).
    fresh = CountingResponse()
    assert reachability._read_health_content(fresh) == b"x" * 100
    assert fresh.consumed == 100


def test_trickling_response_stops_at_the_shared_deadline(monkeypatch):
    """A body that keeps dripping cannot outlive the failover budget."""

    class TrickleResponse(Response):
        def __init__(self):
            super().__init__()
            self.consumed = 0

        def iter_content(self, _size):
            while True:
                self.consumed += 1
                if self.consumed > 100:
                    raise AssertionError("body read outlived the shared deadline")
                yield b"x"

    monkeypatch.setattr(reachability, "time", SteppingTime(step=3.0))
    session = Session(TrickleResponse())
    result = probe(
        "Public audio delivery", "https://example.com/", "testaudio", session
    )
    # The check commits with the bytes it had instead of streaming forever:
    # the reader stops at the deadline (issue #53 review).
    assert result["status"] == "reachable"
    assert 0 < session.response.consumed <= 5


def test_stalled_pool_reports_unavailable_and_reclaims_capacity(monkeypatch):
    """A full pool reports distinctly and recovers without an add-on restart."""
    import threading as _threading

    created = []
    real_thread = _threading.Thread
    release = _threading.Event()

    def _counting_thread(*args, **kwargs):
        thread = real_thread(*args, **kwargs)
        created.append(thread)
        return thread

    def _stall(*_args, **_kwargs):
        release.wait()
        raise OSError("simulated resolver stall")

    monkeypatch.setattr(reachability, "_DNS_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(reachability.socket, "getaddrinfo", _stall)
    monkeypatch.setattr(reachability, "_dns_inflight", {})
    monkeypatch.setattr(reachability, "_MAX_DNS_WORKERS", 1)
    monkeypatch.setattr(reachability.threading, "Thread", _counting_thread)
    try:
        assert (
            reachability._resolved_addresses("one.example")
            is reachability._DNS_TIMED_OUT
        )
        # The pool is full with a young entry: a distinct failure, not a
        # misleading per-host timeout (issue #49 review).
        assert (
            reachability._resolved_addresses("two.example")
            is reachability._DNS_UNAVAILABLE
        )
        assert len(created) == 1  # no extra blocked worker while full
        # Age the stalled entry past the reaping bound: the slot is reclaimed
        # and a fresh lookup runs — no add-on restart needed.
        name, (stalled, found, started) = next(iter(reachability._dns_inflight.items()))
        reachability._dns_inflight[name] = (
            stalled,
            found,
            started - reachability._DNS_WORKER_MAX_AGE_SECONDS - 1,
        )
        assert (
            reachability._resolved_addresses("three.example")
            is reachability._DNS_TIMED_OUT
        )
        assert len(created) == 2  # capacity recovered: a new worker started
    finally:
        release.set()  # let the blocked workers finish cleanly
        for worker in created:
            worker.join(2)
        reachability._dns_inflight.clear()


def test_refused_worker_start_fails_the_check_without_aborting(monkeypatch):
    """A refused resolver thread is a bounded failure, not a crashed run."""
    monkeypatch.setattr(reachability, "_dns_inflight", {})

    class Refused(reachability.threading.Thread):
        def start(self):
            raise RuntimeError("simulated thread-start refusal")

    monkeypatch.setattr(reachability.threading, "Thread", Refused)
    try:
        assert (
            reachability._resolved_addresses("refused.example")
            is reachability._DNS_UNAVAILABLE
        )
        # The in-flight entry was dropped: no unstarted worker lingers
        # (issue #49 review).
        assert reachability._dns_inflight == {}
    finally:
        reachability._dns_inflight.clear()


def test_probe_reports_an_unavailable_resolver_without_requesting(monkeypatch):
    """An unstartable lookup fails the check with its own message."""
    monkeypatch.setattr(
        reachability,
        "_resolved_addresses",
        lambda _host: reachability._DNS_UNAVAILABLE,
    )
    session = Session(Response())
    result = probe("Skill", "https://example.com/", "skill", session)
    assert result["status"] == "failed"
    assert "unavailable" in result["message"]  # distinct from a timeout
    assert session.calls == []  # never sends a request that would re-resolve
