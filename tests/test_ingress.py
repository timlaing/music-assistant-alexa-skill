from urllib.parse import parse_qs, urlparse

from skill_deployment import DeploymentManager
from test_skill_deployment import BASE_URL, Amazon

PREFIX = "/api/hassio_ingress/test_session"


def gateway(client, path, method="get", **kwargs):
    # The real browser sends prefix-scoped cookies before HA strips PATH_INFO.
    for name in ("setup_csrf", "setup_oauth"):
        cookie = client.get_cookie(name, path=PREFIX + "/setup")
        if cookie:
            client.set_cookie(name, cookie.value, path="/setup")
    return getattr(client, method)(
        path,
        environ_overrides={"REMOTE_ADDR": "172.30.32.2", "SERVER_PORT": "8099"},
        headers={"X-Ingress-Path": PREFIX, **kwargs.pop("headers", {})},
        **kwargs,
    )


def test_ingress_pages_no_app_auth_and_urls_keep_prefix(client, monkeypatch):
    monkeypatch.setenv("HA_INGRESS_ENABLED", "true")
    monkeypatch.delenv("APP_USERNAME")
    monkeypatch.delenv("APP_PASSWORD")
    for path in ("/status", "/setup", "/setup/status", "/status/ma", "/status/alexa"):
        response = gateway(client, path)
        assert response.status_code == 200
        assert response.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert PREFIX + "/status" in gateway(client, "/setup").text
    from endpoints import devices

    monkeypatch.setattr(devices, "_seen_devices", lambda: {"device": {}})
    assert 'action="' + PREFIX + "/devices" in gateway(client, "/devices").text
    assert "fetch('" + PREFIX + "/status/ma" in gateway(client, "/status").text
    assert gateway(client, "/").headers["Location"] == PREFIX + "/status"
    for path in ("/setup", "/setup/status", "/status", "/status/api"):
        assert client.get(path).status_code == 403
    assert gateway(client, "/ma/latest-url").status_code == 403
    assert (
        client.get("/ma/latest-url").status_code == 404
    )  # no configured standalone API credentials


def test_spoofed_ingress_headers_and_wrong_port_cannot_bypass_auth(client, monkeypatch):
    monkeypatch.setenv("HA_INGRESS_ENABLED", "true")
    for source, port in [("127.0.0.1", "8099"), ("172.30.32.2", "5000")]:
        response = client.get(
            "/setup",
            headers={"X-Ingress-Path": PREFIX, "X-Forwarded-For": "172.30.32.2"},
            environ_overrides={"REMOTE_ADDR": source, "SERVER_PORT": port},
        )
        assert response.status_code == 403
    assert client.get("/alexa/intents").status_code == 401
    assert client.get("/ma/latest-url").status_code == 401


def test_oauth_public_callback_finishes_only_in_original_ingress_browser(
    client, monkeypatch, tmp_path
):
    from app import app

    monkeypatch.setenv("HA_INGRESS_ENABLED", "true")
    monkeypatch.setenv("LWA_CLIENT_ID", "client")
    monkeypatch.setenv("LWA_CLIENT_SECRET", "private-secret")
    monkeypatch.setenv(
        "LWA_REDIRECT_URI", BASE_URL + "/ma-alexa-skill/setup/oauth/callback"
    )
    fake = Amazon()
    manager = DeploymentManager(tmp_path / "deployment.json", fake)
    app.extensions["skill_deployment"] = manager
    csrf = gateway(client, "/setup/status").json["csrf"]
    headers = {"X-CSRF-Token": csrf}
    response = gateway(client, "/setup/oauth/start", "post", headers=headers, json={})
    assert response.status_code == 200
    assert PREFIX + "/setup" in response.headers["Set-Cookie"]
    state = parse_qs(urlparse(response.json["url"]).query)["state"][0]
    public = app.test_client()
    assert (
        public.get(
            "/ma-alexa-skill/setup/oauth/callback",
            query_string={"state": state, "code": "private-code"},
        ).status_code
        == 200
    )
    assert not manager.public_status()["connected"]
    assert not fake.calls  # no credential exchange on the public endpoint
    assert (
        public.get(
            "/ma-alexa-skill/setup/oauth/callback",
            query_string={"state": state, "code": "private-code"},
        ).status_code
        == 400
    )
    wrong_browser = app.test_client()
    assert not gateway(wrong_browser, "/setup/status").json["oauth_ready"]
    # Werkzeug test clients route prefix cookies by URL; model the gateway stripping it.
    cookie = client.get_cookie("setup_oauth", path=PREFIX + "/setup")
    client.set_cookie("setup_oauth", cookie.value, path="/setup")
    csrf_cookie = client.get_cookie("setup_csrf", path=PREFIX + "/setup")
    client.set_cookie("setup_csrf", csrf_cookie.value, path="/setup")
    assert gateway(client, "/setup/status").json["oauth_ready"]
    assert (
        gateway(
            client, "/setup/oauth/finish", "post", headers=headers, json={}
        ).status_code
        == 200
    )
    assert manager.public_status()["connected"]
    assert fake.calls[0][0] == "TOKEN"


def test_cached_double_slash_ingress_entry_opens_status(client, monkeypatch):
    monkeypatch.setenv("HA_INGRESS_ENABLED", "true")
    response = client.get(
        "/status",
        environ_overrides={
            "REMOTE_ADDR": "172.30.32.2",
            "SERVER_PORT": "8099",
            "PATH_INFO": "//status",
        },
        headers={"X-Ingress-Path": PREFIX},
    )
    assert response.status_code == 200
    assert "fetch('" + PREFIX + "/status/ma" in response.text
    # Normalization must not permit playback APIs through the private listener.
    blocked = client.get(
        "/ma/latest-url",
        environ_overrides={
            "REMOTE_ADDR": "172.30.32.2",
            "SERVER_PORT": "8099",
            "PATH_INFO": "//ma/latest-url",
        },
        headers={"X-Ingress-Path": PREFIX},
    )
    assert blocked.status_code == 403
    # A doubled slash and ingress headers cannot establish gateway trust.
    spoofed = client.get(
        "/status",
        environ_overrides={"SERVER_PORT": "8099", "PATH_INFO": "//status"},
        headers={"X-Ingress-Path": PREFIX, "X-Forwarded-For": "172.30.32.2"},
    )
    assert spoofed.status_code == 403
