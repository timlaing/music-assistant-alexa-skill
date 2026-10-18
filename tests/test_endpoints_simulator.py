"""Tests for the Alexa request simulator endpoints (endpoints/simulator.py)."""

import json
from unittest.mock import MagicMock

import requests
from endpoints import simulator

AUTH = {"Authorization": "Basic dGVzdC11c2VyOnRlc3QtcGFzc3dvcmQ="}


class FakeResponse:
    def __init__(
        self,
        status_code=200,
        content=b'{"outputSpeech": {"text": "ok"}}',
        ok=True,
        json_data=None,
    ):
        self.status_code = status_code
        self.content = content
        self.ok = ok
        self._json = json_data or {}

    def json(self):
        return self._json


def test_simulator_index_injects_skill_hostname(client, monkeypatch):
    monkeypatch.setenv("SKILL_HOSTNAME", "https://skill.example.com")
    response = client.get("/simulator", headers=AUTH)
    assert response.status_code == 200
    assert response.mimetype == "text/html"
    body = response.get_data(as_text=True)
    assert '"https://skill.example.com"' in body
    assert "__SKILL_HOSTNAME__" not in body


def test_simulator_api_lists_model_intents(client):
    payload = client.get("/simulator/api", headers=AUTH).json
    assert "intents" in payload
    assert "skill_hostname" in payload
    intent_names = {intent["name"] for intent in payload["intents"]}
    assert "AMAZON.HelpIntent" in intent_names or payload["intents"] == []


def test_simulator_payload_requires_intent(client):
    response = client.get("/simulator/payload", headers=AUTH)
    assert response.status_code == 400
    assert "missing intent parameter" in response.json["error"]


def test_simulator_payload_builds_intent_request(client):
    payload = client.get("/simulator/payload?intent=PlayAudio", headers=AUTH).json
    assert payload["request"]["type"] == "IntentRequest"
    assert payload["request"]["intent"] == {"name": "PlayAudio", "slots": {}}
    assert payload["session"]["new"] is True


def test_build_payload_without_override():
    payload, error = simulator._build_payload("LaunchIt", None)
    assert error is None
    assert payload["request"]["intent"]["name"] == "LaunchIt"
    assert payload["context"] == {}


def test_build_payload_with_valid_and_invalid_override(client):
    with app_request(client):
        payload, error = simulator._build_payload("X", '{"custom": true}')
        assert error is None and payload == {"custom": True}
        payload, error = simulator._build_payload("X", "{not json")
        assert payload is None
        _, status = error
        assert status == 400
        assert "invalid override_payload JSON" in error[0].json["error"]


def test_resolve_target_requires_skill_hostname(client, monkeypatch):
    # SKILL_HOSTNAME falls back to deriving an endpoint from MA_HOSTNAME,
    # so both must be unset to reach the "not configured" branch.
    monkeypatch.delenv("SKILL_HOSTNAME", raising=False)
    monkeypatch.setenv("MA_HOSTNAME", "")
    with app_request(client):
        target, _headers, _verify, error = simulator._resolve_target("hostname")
    assert target is None and error[1] == 400
    assert "SKILL_HOSTNAME not configured" in error[0].json["error"]


def app_request(client):
    return client.application.test_request_context("/")


def test_resolve_target_hostname_resolves_via_doh(client, monkeypatch):
    monkeypatch.setenv("SKILL_HOSTNAME", "https://skill.example.com:8443")
    monkeypatch.setattr(simulator, "_resolve_doh", lambda host: "203.0.113.9")
    with app_request(client):
        target, headers, verify, error = simulator._resolve_target("hostname")
    assert error is None
    assert target == "https://203.0.113.9:8443/"
    assert headers["Host"] == "skill.example.com"
    assert headers["Content-Type"] == "application/json"
    assert verify is False


def test_resolve_target_hostname_without_scheme(client, monkeypatch):
    monkeypatch.setenv("SKILL_HOSTNAME", "skill.example.com")
    monkeypatch.setattr(simulator, "_resolve_doh", lambda host: "203.0.113.9")
    with app_request(client):
        target, _headers, _verify, error = simulator._resolve_target("hostname")
    assert error is None
    assert target == "https://203.0.113.9/"


def test_resolve_target_hostname_dns_failure(client, monkeypatch):
    monkeypatch.setenv("SKILL_HOSTNAME", "https://skill.example.com")
    monkeypatch.setattr(simulator, "_resolve_doh", lambda host: None)
    with app_request(client):
        _target, _headers, _verify, error = simulator._resolve_target("hostname")
    assert error[1] == 500
    assert "DNS resolution failed" in error[0].json["error"]


def test_resolve_target_local_posts_to_self(client):
    with client.application.test_request_context("/simulator/send"):
        target, _headers, verify, error = simulator._resolve_target("local")
    assert error is None
    assert target.startswith("http")
    assert target.endswith("/")
    assert verify is True


def test_resolve_doh_parses_answers_and_handles_errors(monkeypatch):
    monkeypatch.setattr(
        simulator.requests,
        "get",
        lambda *a, **kw: FakeResponse(json_data={"Answer": [{"data": "198.51.100.4"}]}),
    )
    assert simulator._resolve_doh("host.example.com") == "198.51.100.4"
    # Non-string answer data is skipped -> None
    monkeypatch.setattr(
        simulator.requests,
        "get",
        lambda *a, **kw: FakeResponse(json_data={"Answer": [{"data": 42}]}),
    )
    assert simulator._resolve_doh("host.example.com") is None
    # Failed HTTP / exceptions -> None
    monkeypatch.setattr(
        simulator.requests, "get", lambda *a, **kw: FakeResponse(ok=False)
    )
    assert simulator._resolve_doh("host.example.com") is None

    def boom(*a, **kw):
        raise requests.RequestException("nope")

    monkeypatch.setattr(simulator.requests, "get", boom)
    assert simulator._resolve_doh("host.example.com") is None


def test_simulator_auth_uses_configured_credentials():
    assert simulator._simulator_auth() == ("test-user", "test-password")


def test_simulator_auth_without_credentials(monkeypatch):
    monkeypatch.delenv("APP_USERNAME", raising=False)
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    from app_settings import store

    store().values = None
    assert simulator._simulator_auth() is None


def test_simulator_send_requires_intent(client):
    response = client.post("/simulator/send", headers=AUTH, json={"use": "local"})
    assert response.status_code == 400
    assert response.json["error"] == "missing intent"


def test_simulator_send_local_posts_and_wraps_response(client, monkeypatch):
    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return FakeResponse(status_code=200, content=b'{"response": {"ok": true}}')

    monkeypatch.setattr(simulator.requests, "post", fake_post)
    response = client.post(
        "/simulator/send",
        headers=AUTH,
        json={"intent": "PlayAudio", "use": "local"},
    )
    assert response.status_code == 200
    assert response.json["status_code"] == 200
    assert '"response"' in response.json["body"]
    # Payload was a constructed IntentRequest for the requested intent.
    assert captured["json"]["request"]["intent"]["name"] == "PlayAudio"
    # Simulator signature headers are attached so dispatch does not 400.
    assert captured["headers"]["Signature"] == "dummy-signature"
    assert "X-Simulator-CertUrl" in captured["headers"]
    assert captured["auth"] == ("test-user", "test-password")
    assert captured["verify"] is True


def test_simulator_send_with_override_payload(client, monkeypatch):
    captured = {}

    def fake_post(url, **kwargs):
        captured["json"] = kwargs["json"]
        return FakeResponse()

    monkeypatch.setattr(simulator.requests, "post", fake_post)
    response = client.post(
        "/simulator/send",
        headers=AUTH,
        json={
            "intent": "ignored",
            "use": "local",
            "override_payload": {"request": {"type": "IntentRequest"}},
        },
    )
    assert response.status_code == 200
    assert captured["json"] == {"request": {"type": "IntentRequest"}}


def test_simulator_send_hostname_targets_resolved_ip(client, monkeypatch):
    monkeypatch.setenv("SKILL_HOSTNAME", "https://skill.example.com")
    monkeypatch.setattr(simulator, "_resolve_doh", lambda host: "203.0.113.9")
    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["verify"] = kwargs["verify"]
        return FakeResponse(status_code=200, content=b"{}")

    monkeypatch.setattr(simulator.requests, "post", fake_post)
    response = client.post(
        "/simulator/send", headers=AUTH, json={"intent": "PlayAudio", "use": "hostname"}
    )
    assert response.status_code == 200
    assert captured["url"] == "https://203.0.113.9/"
    assert captured["verify"] is False


def test_simulator_send_ssl_error_retries_with_hostname_sni(client, monkeypatch):
    monkeypatch.setenv("SKILL_HOSTNAME", "https://skill.example.com")
    monkeypatch.setattr(simulator, "_resolve_doh", lambda host: "203.0.113.9")
    calls = []

    def fake_post(url, **kwargs):
        calls.append(url)
        if url.startswith("https://203.0.113.9"):
            raise requests.exceptions.SSLError("bad cert")
        return FakeResponse(status_code=200, content=b"{}")

    monkeypatch.setattr(simulator.requests, "post", fake_post)
    response = client.post(
        "/simulator/send", headers=AUTH, json={"intent": "PlayAudio", "use": "hostname"}
    )
    assert response.status_code == 200
    assert response.json["note"] == "retried using hostname for SNI"
    assert calls == ["https://203.0.113.9/", "https://skill.example.com/"]


def test_simulator_send_ssl_error_without_retry_returns_500(client, monkeypatch):
    def fake_post(url, **kwargs):
        raise requests.exceptions.SSLError("bad cert")

    monkeypatch.setattr(simulator.requests, "post", fake_post)
    response = client.post(
        "/simulator/send", headers=AUTH, json={"intent": "PlayAudio", "use": "local"}
    )
    assert response.status_code == 500
    assert "bad cert" in response.json["error"]


def test_simulator_send_generic_error_returns_500(client, monkeypatch):
    def fake_post(url, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(simulator.requests, "post", fake_post)
    response = client.post(
        "/simulator/send", headers=AUTH, json={"intent": "PlayAudio", "use": "local"}
    )
    assert response.status_code == 500
    assert response.json["error"] == "boom"


def test_post_json_decodes_bytes_and_handles_decode_failure(client, monkeypatch):
    with client.application.test_request_context("/"):
        monkeypatch.setattr(
            simulator.requests,
            "post",
            lambda *a, **kw: FakeResponse(content=b"\xff\xfe!"),
        )
        envelope = simulator._post_json("http://x/", {}, {}, None, True)
        assert envelope.status_code == 200
        assert isinstance(envelope.json["body"], str)


def test_retry_with_hostname_skips_non_hostname_targets(client, monkeypatch):
    monkeypatch.setattr(
        simulator.requests,
        "post",
        lambda *a, **kw: MagicMock(side_effect=AssertionError),
    )
    with client.application.test_request_context("/"):
        assert (
            simulator._retry_with_hostname("local", "http", "h", {}, {}, None) is None
        )
        assert (
            simulator._retry_with_hostname("hostname", None, None, {}, {}, None) is None
        )
        assert (
            simulator._retry_with_hostname("hostname", "https", None, {}, {}, None)
            is None
        )


def test_retry_with_hostname_returns_none_on_exception(client, monkeypatch):
    def fake_post(url, **kwargs):
        raise RuntimeError("down")

    monkeypatch.setattr(simulator.requests, "post", fake_post)
    with client.application.test_request_context("/"):
        assert (
            simulator._retry_with_hostname(
                "hostname", "https", "skill.example.com", {}, {}, None
            )
            is None
        )


def test_load_model_intents_parses_en_us_model():
    intents = simulator._load_model_intents()
    assert isinstance(intents, list)
    if intents:
        assert {"name", "samples"} <= set(intents[0])


def test_load_model_intents_returns_empty_on_error(monkeypatch):
    class BrokenModelPath:
        def __truediv__(self, other):
            return self

        @property
        def parent(self):
            return self

        def exists(self):
            return True

        def read_text(self, *args, **kwargs):
            raise OSError("unreadable")

        def glob(self, pattern):
            return iter(())

    monkeypatch.setattr(simulator, "Path", lambda *a: BrokenModelPath())
    assert simulator._load_model_intents() == []


def test_sample_payload_is_not_mutated_by_route(client):
    before = json.dumps(simulator.SAMPLE_PAYLOAD, sort_keys=True)
    response = client.get("/simulator/payload?intent=PlayAudio", headers=AUTH)
    assert response.status_code == 200
    assert response.json["request"]["intent"]["name"] == "PlayAudio"
    # The route deep-copies SAMPLE_PAYLOAD, so the module constant is untouched.
    assert json.dumps(simulator.SAMPLE_PAYLOAD, sort_keys=True) == before
