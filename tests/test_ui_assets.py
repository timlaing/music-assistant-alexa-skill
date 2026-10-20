"""Packaged UI assets: auth, ingress trust, content and traversal behaviour."""

import base64
import hashlib
from pathlib import Path

AUTH = {
    "Authorization": "Basic " + base64.b64encode(b"test-user:test-password").decode()
}
PREFIX = "/api/hassio_ingress/test_session"
REPO_ROOT = Path(__file__).resolve().parents[1]


def gateway(client, path):
    return client.get(
        path,
        environ_overrides={"REMOTE_ADDR": "172.30.32.2", "SERVER_PORT": "8099"},
        headers={"X-Ingress-Path": PREFIX},
    )


def test_assets_require_basic_auth_standalone(client, monkeypatch):
    assert client.get("/assets/ui.css").status_code == 401
    monkeypatch.delenv("APP_USERNAME")
    monkeypatch.delenv("APP_PASSWORD")
    response = client.get("/assets/ui.css")
    assert response.status_code == 200
    assert response.mimetype == "text/css"


def test_asset_contents_match_packaged_files(client):
    stylesheet = client.get("/assets/ui.css", headers=AUTH)
    assert stylesheet.status_code == 200
    assert stylesheet.mimetype == "text/css"
    assert stylesheet.data == (REPO_ROOT / "assets" / "ui.css").read_bytes()

    script = client.get("/assets/ui-shell.js", headers=AUTH)
    assert script.status_code == 200
    assert script.mimetype == "text/javascript"

    icon = client.get("/assets/app-icon.png", headers=AUTH)
    assert icon.status_code == 200
    assert icon.mimetype == "image/png"
    assert hashlib.sha256(icon.data).hexdigest() == (
        "bc0b4ea5e29b02413c12777b20e6ee9ba1b36adb8a1139b6e7ff256d7eb7fdb8"
    )


def test_asset_traversal_is_not_served(client):
    assert client.get("/assets/..%2fapp.py", headers=AUTH).status_code == 404
    assert client.get("/assets/sub/../ui.css", headers=AUTH).status_code in (200, 404)
    assert client.get("/assets/missing.css", headers=AUTH).status_code == 404


def test_assets_revalidate_on_each_use(client):
    response = client.get("/assets/ui.css", headers=AUTH)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-cache"
    assert "Last-Modified" in response.headers
    # Conditional revalidation must yield 304 for an unchanged asset so a
    # deployed update is picked up on the next page load, not a day later.
    cached = client.get(
        "/assets/ui.css",
        headers={**AUTH, "If-Modified-Since": response.headers["Last-Modified"]},
    )
    assert cached.status_code == 304


def test_assets_allowed_through_ingress_but_not_bypassed(client, monkeypatch):
    monkeypatch.setenv("HA_INGRESS_ENABLED", "true")
    # Untrusted direct access is refused just like the pages themselves.
    assert client.get("/assets/ui.css").status_code == 403
    response = gateway(client, "/assets/ui.css")
    assert response.status_code == 200
    assert response.mimetype == "text/css"
