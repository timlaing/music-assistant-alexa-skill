"""Exercise Amazon protocol contracts, retry safety and authenticated setup routes."""

import copy
import json
import time
from urllib.parse import parse_qs, urlparse

import pytest
import requests
from skill_deployment import (
    API,
    APP_DIR,
    DeploymentError,
    DeploymentManager,
    config_key,
    configure_package,
    pack,
    package_fingerprint,
    settings,
    unpack_package,
)

SKILL = "amzn1.ask.skill.personal"
OTHER = "amzn1.ask.skill.same-name"
AUTH = {"Authorization": "Basic dGVzdC11c2VyOnRlc3QtcGFzc3dvcmQ="}
BASE_URL = "https://alexa.example.com"


class Response:
    def __init__(self, body=None, status=200, location=None, content=b""):
        self.body, self.status_code, self.content = body, status, content
        self.headers = {"Location": location} if location else {}

    def json(self):
        return copy.deepcopy(self.body)

    def iter_content(self, size):
        yield self.content

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class Amazon:
    def __init__(self):
        self.calls = []
        self.created = 0
        self.imported = False
        self.model_status = "SUCCEEDED"
        self.import_status = "SUCCEEDED"
        self.files = {
            "skill-package/skill.json": (APP_DIR / "skill.json").read_bytes(),
            "skill-package/interactionModels/custom/en-GB.json": (
                APP_DIR / "models/en-GB.json"
            ).read_bytes(),
            "skill-package/interactionModels/custom/de-DE.json": (
                APP_DIR / "models/de-DE.json"
            ).read_bytes(),
            "skill-package/assets/preserve.txt": b"unrelated asset",
        }
        mf = json.loads(self.files["skill-package/skill.json"])
        mf["manifest"]["publishingInformation"]["locales"]["en-GB"]["name"] = (
            "My personal radio"
        )
        mf["manifest"]["apis"]["custom"]["regions"] = {
            "EU": {"endpoint": {"uri": "https://old.example.com"}}
        }
        mf["manifest"]["permissions"] = [
            {"name": "alexa::devices:all:address:country_and_postal_code:read"}
        ]
        self.files["skill-package/skill.json"] = json.dumps(mf).encode()

    def post(self, url, **kwargs):
        self.calls.append(("TOKEN", url, kwargs))
        return Response(
            {
                "access_token": "private-access",
                "refresh_token": "private-refresh",
                "expires_in": 3600,
            }
        )

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.startswith("https://bucket.s3.amazonaws.com/"):
            assert "Authorization" not in kwargs.get("headers", {})
            if method == "PUT":
                self.uploaded, _ = unpack_package(kwargs["data"])
                return Response(status=200)
            return Response(content=pack(self.files))
        assert url.startswith(API)
        path = url[len(API) :]
        if path == "/v1/vendors":
            return Response({"vendors": [{"id": "vendor", "name": "Owner"}]})
        if path == "/v1/skills" and method == "GET":
            return Response(
                {
                    "skills": [
                        {
                            "skillId": value,
                            "nameByLocale": {"en-GB": "Same name"},
                            "stage": "development",
                            "apis": ["custom"],
                        }
                        for value in (SKILL, OTHER)
                    ]
                }
            )
        if path.endswith("/exports"):
            return Response(status=202, location="/v1/skills/exports/export-1")
        if "/exports/" in path:
            return Response(
                {
                    "status": "SUCCEEDED",
                    "skill": {"location": "https://bucket.s3.amazonaws.com/export"},
                }
            )
        if path == "/v1/skills" and method == "POST":
            self.created += 1
            self.new_manifest = kwargs["json"]["manifest"]
            return Response({"skillId": SKILL}, status=202)
        if path == "/v1/skills/uploads":
            return Response(
                {"uploadUrl": "https://bucket.s3.amazonaws.com/upload"}, status=201
            )
        if path.endswith("/imports"):
            self.imported = True
            self.files = self.uploaded
            return Response(status=202, location="/v1/skills/imports/import-1")
        if "/imports/" in path:
            return Response({"status": self.import_status})
        if path.endswith("/status"):
            return Response(
                {
                    "manifest": {"lastUpdateRequest": {"status": "SUCCEEDED"}},
                    "interactionModel": {
                        "en-GB": {"lastUpdateRequest": {"status": self.model_status}}
                    },
                }
            )
        if path.endswith("/manifest"):
            return Response(json.loads(self.files["skill-package/skill.json"]))
        if path.endswith("/enablement"):
            return Response(status=204)
        raise AssertionError((method, path))


@pytest.fixture
def deployment(monkeypatch, tmp_path):
    monkeypatch.setenv("LWA_CLIENT_ID", "client")
    monkeypatch.setenv("LWA_CLIENT_SECRET", "private-client-secret")
    monkeypatch.setenv(
        "LWA_REDIRECT_URI", BASE_URL + "/ma-alexa-skill/setup/oauth/callback"
    )
    monkeypatch.setenv("SKILL_HOSTNAME", BASE_URL + "/ma-alexa-skill/")
    monkeypatch.setenv("LOCALE", "en-GB")
    monkeypatch.setenv("SKILL_CERTIFICATE_TYPE", "Trusted")
    fake = Amazon()
    manager = DeploymentManager(tmp_path / "state.json", fake)
    manager.update(
        tokens={
            "access_token": "private-access",
            "refresh_token": "private-refresh",
            "expires": time.time() + 3600,
        },
        client_key=config_key(),
    )
    return manager, fake


def deploy(manager, create=False):
    manager.prepare("vendor", "" if create else SKILL, create)
    manager.deploy(manager.state["review"]["id"])


def test_existing_skill_preserves_identity_resources_and_invocation(deployment):
    manager, amazon = deployment
    original = copy.deepcopy(amazon.files)
    deploy(manager)
    assert manager.state["phase"] == "complete"
    assert manager.state["skill_id"] == SKILL
    assert amazon.created == 0
    assert (
        amazon.files["skill-package/assets/preserve.txt"]
        == original["skill-package/assets/preserve.txt"]
    )
    assert (
        amazon.files["skill-package/interactionModels/custom/de-DE.json"]
        == original["skill-package/interactionModels/custom/de-DE.json"]
    )
    manifest = json.loads(amazon.files["skill-package/skill.json"])["manifest"]
    assert (
        manifest["publishingInformation"]["locales"]["en-GB"]["name"]
        == "My personal radio"
    )
    assert (
        manifest["permissions"]
        == json.loads(original["skill-package/skill.json"])["manifest"]["permissions"]
    )
    assert (
        manifest["apis"]["custom"]["regions"]["EU"]["endpoint"]["uri"]
        == settings()["endpoint"]
    )
    model = json.loads(
        amazon.files["skill-package/interactionModels/custom/en-GB.json"]
    )
    assert model["interactionModel"]["languageModel"]["invocationName"] == "my radio"
    assert not any(method == "DELETE" for method, _, _ in amazon.calls)
    assert all("/live/" not in url for _, url, _ in amazon.calls)


def test_create_and_repeat_never_creates_duplicates(deployment):
    manager, amazon = deployment
    deploy(manager, create=True)
    assert amazon.created == 1
    assert list(amazon.new_manifest["publishingInformation"]["locales"]) == ["en-GB"]
    deploy(manager)
    assert amazon.created == 1
    with pytest.raises(DeploymentError, match="already saved"):
        manager.prepare("vendor", "", True)


def test_duplicate_names_require_explicit_id(deployment):
    manager, _amazon = deployment
    with pytest.raises(DeploymentError, match="Select a development"):
        manager.prepare("vendor", "Same name", False)
    manager.prepare("vendor", OTHER, False)
    assert manager.state["review"]["skill"] == OTHER


@pytest.mark.parametrize("stage", ["FAILED", "IN_PROGRESS", None])
def test_no_success_without_model_build(deployment, stage):
    manager, amazon = deployment
    amazon.model_status = stage
    with pytest.raises(DeploymentError, match="not confirmed"):
        deploy(manager)
    assert manager.state.get("verified_at") is None
    assert not any(url.endswith("/enablement") for _, url, _ in amazon.calls)


def test_failed_import_not_enabled(deployment):
    manager, amazon = deployment
    amazon.import_status = "FAILED"
    with pytest.raises(DeploymentError, match="package import failed"):
        deploy(manager)
    assert manager.state.get("phase") != "complete"


def test_restart_can_resume_accepted_import(deployment):
    manager, amazon = deployment
    manager.prepare("vendor", SKILL, False)
    review = manager.state["review"]
    manager.update(
        phase="running",
        vendor_id="vendor",
        skill_id=SKILL,
        deployed_settings=review["settings"],
        import_path="/v1/skills/imports/import-1",
        import_unknown=True,
    )
    amazon.files, _, _ = configure_package(
        amazon.files, "skill-package/skill.json", settings(), True
    )
    recovered = DeploymentManager(manager.path, amazon)
    assert recovered.state["phase"] == "interrupted"
    assert recovered.public_status()["can_resume"]
    recovered.update(expected_package=package_fingerprint(amazon.files))
    recovered.finish()
    assert recovered.state["phase"] == "complete"
    assert not recovered.state["import_unknown"]
    assert amazon.created == 0


def test_create_timeout_is_ambiguous_and_blocks_recreation(deployment):
    manager, amazon = deployment
    original = amazon.request

    def request(method, url, **kwargs):
        if method == "POST" and url == API + "/v1/skills":
            raise requests.Timeout("secret must not be displayed")
        return original(method, url, **kwargs)

    amazon.request = request
    with pytest.raises(DeploymentError, match="timed out"):
        deploy(manager, create=True)
    recovered = DeploymentManager(manager.path, amazon)
    with pytest.raises(DeploymentError, match="uncertain"):
        recovered.prepare("vendor", "", True)
    assert recovered.state["creation_unknown"]


def test_refresh_and_no_secret_status(deployment):
    manager, amazon = deployment
    manager.update(
        tokens={"access_token": "old", "refresh_token": "private-refresh", "expires": 0}
    )
    manager.accounts()
    assert amazon.calls[0][0] == "TOKEN"
    public = json.dumps(manager.public_status())
    assert "private-" not in public
    assert manager.path.stat().st_mode & 0o777 == 0o600


def test_refresh_rejection_requires_reconnect(deployment):
    manager, amazon = deployment
    manager.state["tokens"]["expires"] = 0
    amazon.post = lambda *args, **kwargs: Response(status=400)
    with pytest.raises(DeploymentError, match="reconnection"):
        manager.accounts()
    assert not manager.public_status()["connected"]


def test_configuration_change_invalidates_connection(deployment, monkeypatch):
    manager, _amazon = deployment
    monkeypatch.setenv("LWA_CLIENT_ID", "another")
    assert not manager.public_status()["connected"]
    with pytest.raises(DeploymentError, match="Connect Amazon"):
        manager.accounts()


def test_review_detects_amazon_concurrent_changes(deployment):
    manager, amazon = deployment
    manager.prepare("vendor", SKILL, False)
    amazon.files["skill-package/assets/new.txt"] = b"added after review"
    with pytest.raises(DeploymentError, match="changed in Amazon"):
        manager.deploy(manager.state["review"]["id"])
    assert not amazon.imported


def test_expired_review_and_settings_changes(deployment, monkeypatch):
    manager, _ = deployment
    manager.prepare("vendor", SKILL, False)
    manager.state["review"]["expires"] = 0
    with pytest.raises(DeploymentError, match="expired"):
        manager.deploy(manager.state["review"]["id"])
    manager.prepare("vendor", SKILL, False)
    monkeypatch.setenv("ENABLE_APL", "true")
    with pytest.raises(DeploymentError, match="settings changed"):
        manager.deploy(manager.state["review"]["id"])


@pytest.mark.parametrize(
    "apl,certificate", [("true", "Wildcard"), ("false", "Trusted")]
)
def test_apl_and_certificate_follow_options(deployment, monkeypatch, apl, certificate):
    manager, _ = deployment
    monkeypatch.setenv("ENABLE_APL", apl)
    monkeypatch.setenv("SKILL_CERTIFICATE_TYPE", certificate)
    deploy(manager)
    assert manager.state["deployed_settings"]["apl"] == (apl == "true")
    assert manager.state["deployed_settings"]["certificate"] == certificate


@pytest.mark.parametrize(
    "field,value",
    [
        ("LOCALE", "../en-GB"),
        ("SKILL_HOSTNAME", "http://alexa.example.com"),
        ("SKILL_HOSTNAME", "https://user:secret@example.com"),
        ("SKILL_HOSTNAME", "https://example.com:5000"),
        ("SKILL_CERTIFICATE_TYPE", "SelfSigned"),
    ],
)
def test_invalid_settings_rejected(deployment, monkeypatch, field, value):
    monkeypatch.setenv(field, value)
    with pytest.raises((DeploymentError, ValueError)):
        settings()


def test_oauth_state_single_use_expiry_and_browser_binding(deployment):
    manager, _ = deployment
    url = manager.authorize("browser")
    state = parse_qs(urlparse(url).query)["state"][0]
    assert "private-client-secret" not in url
    manager.callback(state, "browser", "private-code")
    with pytest.raises(DeploymentError, match="expired"):
        manager.callback(state, "browser", "private-code")
    for browser, expired in [("other", False), ("browser", True)]:
        url = manager.authorize("browser")
        state = parse_qs(urlparse(url).query)["state"][0]
        if expired:
            manager.state["oauth"]["expires"] = 0
        with pytest.raises(DeploymentError, match="expired"):
            manager.callback(state, browser, "private-code")


def test_setup_routes_auth_csrf_callback(deployment, client):
    from app import app

    manager, _ = deployment
    app.extensions["skill_deployment"] = manager
    assert client.get("/setup/status").status_code == 401
    assert client.get("/setup", headers=AUTH).status_code == 200
    assert client.post("/setup/preview", headers=AUTH, json={}).status_code == 403
    status = client.get("/setup/status", headers=AUTH, base_url=BASE_URL).json
    headers = {**AUTH, "X-CSRF-Token": status["csrf"]}
    response = client.post(
        "/setup/oauth/start", headers=headers, json={}, base_url=BASE_URL
    )
    assert response.status_code == 200
    state = parse_qs(urlparse(response.json["url"]).query)["state"][0]
    response = client.get(
        "/setup/oauth/callback",
        query_string={"state": state, "code": "private-code"},
        headers=AUTH,
        base_url=BASE_URL,
    )
    assert response.status_code == 200
    assert client.get("/setup/status", headers=AUTH, base_url=BASE_URL).json[
        "oauth_ready"
    ]
    assert (
        client.post(
            "/setup/oauth/finish", headers=headers, json={}, base_url=BASE_URL
        ).status_code
        == 200
    )
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert (
        client.get(
            "/setup/oauth/callback",
            query_string={"state": state, "code": "private-code"},
            headers=AUTH,
            base_url=BASE_URL,
        ).status_code
        == 400
    )


def test_setup_requires_credentials_outside_ingress(deployment, client, monkeypatch):
    status = client.get("/setup/status", headers=AUTH).json
    headers = {**AUTH, "X-CSRF-Token": status["csrf"]}
    assert (
        client.post("/setup/oauth/start", headers=headers, json={}).status_code == 200
    )
    monkeypatch.delenv("APP_USERNAME")
    monkeypatch.delenv("APP_PASSWORD")
    assert client.get("/setup/status").status_code == 403


def test_zip_path_traversal_and_storage_redirects_rejected(deployment):
    manager, _ = deployment
    with pytest.raises(DeploymentError, match="unsafe"):
        unpack_package(pack({"../skill.json": b"{}"}))
    for url in [
        "http://bucket.s3.amazonaws.com/file",
        "https://localhost/file",
        "https://bucket.s3.amazonaws.com.evil/file",
    ]:
        with pytest.raises(DeploymentError, match="unexpected"):
            manager.transfer("GET", url)


def test_background_errors_are_sanitized(deployment):
    manager, _ = deployment

    def bad():
        raise ValueError("private-client-secret")

    manager.start_job("preparing", bad)
    manager.thread.join(2)
    assert manager.state["phase"] == "failed"
    assert "private-" not in manager.state["message"]


def test_oauth_callback_codes_removed_from_werkzeug_logs(client):
    import logging

    from app import _CallbackLogFilter

    record = logging.LogRecord(
        "werkzeug",
        logging.INFO,
        "",
        1,
        "%s",
        ("GET /setup/oauth/callback?code=private-code&state=private-state HTTP/1.1",),
        None,
    )
    _CallbackLogFilter().filter(record)
    assert "private-" not in record.getMessage()
    assert "/setup/oauth/callback HTTP/1.1" in record.getMessage()


def test_rejected_access_token_refreshes_once(deployment):
    manager, amazon = deployment
    original = amazon.request
    rejected = False

    def request(method, url, **kwargs):
        nonlocal rejected
        if not rejected:
            rejected = True
            return Response(status=401)
        return original(method, url, **kwargs)

    amazon.request = request
    assert manager.accounts()[0]["id"] == "vendor"
    assert sum(method == "TOKEN" for method, _, _ in amazon.calls) == 1


def test_package_at_root_and_invocation_preserved(deployment):
    files = {"skill.json": (APP_DIR / "skill.json").read_bytes()}
    prepared, _, invocation = configure_package(files, "skill.json", settings(), False)
    assert "interactionModels/custom/en-GB.json" in prepared
    assert invocation == "music assistant"


def test_corrupt_uploaded_model_cannot_report_success(deployment):
    manager, amazon = deployment
    original = amazon.request

    def request(method, url, **kwargs):
        response = original(method, url, **kwargs)
        if url.endswith("/imports"):
            model = json.loads(
                amazon.files["skill-package/interactionModels/custom/en-GB.json"]
            )
            model["interactionModel"]["languageModel"]["invocationName"] = (
                "unexpected name"
            )
            amazon.files["skill-package/interactionModels/custom/en-GB.json"] = (
                json.dumps(model).encode()
            )
        return response

    amazon.request = request
    with pytest.raises(DeploymentError, match="differs from the approved"):
        deploy(manager)
    assert manager.state.get("verified_at") is None
    assert not any(url.endswith("/enablement") for _, url, _ in amazon.calls)


def test_routes_review_deploy_and_replay(deployment, client):
    from app import app

    manager, amazon = deployment
    app.extensions["skill_deployment"] = manager
    status = client.get("/setup/status", headers=AUTH).json
    headers = {**AUTH, "X-CSRF-Token": status["csrf"]}
    response = client.post(
        "/setup/preview",
        headers=headers,
        json={"vendor": "vendor", "skill": SKILL, "create": False},
    )
    assert response.status_code == 202
    manager.thread.join(2)
    status = client.get("/setup/status", headers=AUTH).json
    assert status["phase"] == "ready"
    review_id = status["review"]["id"]
    assert (
        client.post(
            "/setup/deploy", headers=headers, json={"review_id": review_id}
        ).status_code
        == 202
    )
    manager.thread.join(2)
    assert client.get("/setup/status", headers=AUTH).json["phase"] == "complete"
    assert (
        client.post(
            "/setup/deploy", headers=headers, json={"review_id": review_id}
        ).status_code
        == 202
    )
    manager.thread.join(2)
    assert manager.state["phase"] == "failed"
    assert amazon.created == 0


def test_active_import_prevents_another_preview(deployment):
    manager, amazon = deployment
    manager.update(import_path="/v1/skills/imports/import-1")
    amazon.import_status = "IN_PROGRESS"
    with pytest.raises(DeploymentError, match="still running"):
        manager.prepare("vendor", SKILL, False)


def test_shutdown_interrupts_api_calls_and_persists(deployment):
    manager, _ = deployment
    manager.update(phase="running")
    manager.stop()
    assert manager.state["phase"] == "interrupted"
    with pytest.raises(DeploymentError, match="shutdown"):
        manager.accounts()
    recovered = DeploymentManager(manager.path)
    assert recovered.state["phase"] == "interrupted"


@pytest.mark.parametrize("field,value", [("LOCALE", "invalid"), ("SKILL_HOSTNAME", "")])
def test_status_preserves_connected_deployment_with_invalid_current_settings(
    deployment, client, monkeypatch, field, value
):
    from app import app

    manager, _ = deployment
    manager.update(
        phase="complete",
        deployed_settings=settings(),
        message="Previously verified deployment",
    )
    app.extensions["skill_deployment"] = manager
    monkeypatch.setenv(field, value)
    response = client.get("/status/ask", headers=AUTH)
    assert response.status_code == 200
    html = response.json["skill_ask_html"]
    assert "Previously verified deployment" in html
    assert "led yellow" in html
    assert "ASK check error" not in html


@pytest.mark.parametrize(
    "changes,invalidated",
    [
        ({"locale": "en-US"}, False),
        ({"ma_hostname": "https://other-streams.example.com"}, False),
        ({"skill_hostname": BASE_URL + "/different-prefix/"}, False),
        ({"lwa_client_id": "different-client"}, True),
        ({"lwa_client_secret": "different-secret"}, True),
        ({"skill_hostname": "https://different.example.com/ma-alexa-skill/"}, True),
    ],
)
def test_settings_save_only_invalidates_sign_in_when_oauth_config_changes(
    deployment, client, monkeypatch, changes, invalidated
):
    from app import app
    from test_ingress import gateway

    manager, _ = deployment
    app.extensions["skill_deployment"] = manager
    monkeypatch.setenv("HA_INGRESS_ENABLED", "true")
    csrf = gateway(client, "/setup/status").json["csrf"]
    response = gateway(
        client, "/setup/oauth/start", "post", headers={"X-CSRF-Token": csrf}, json={}
    )
    state = parse_qs(urlparse(response.json["url"]).query)["state"][0]
    manager.update(review={"id": "old-review"})
    current = gateway(client, "/setup/settings").json
    saved = gateway(
        client,
        "/setup/settings",
        "post",
        headers={"X-CSRF-Token": csrf},
        json={"revision": current["revision"], "values": changes},
    )
    assert saved.status_code == 200
    assert manager.state["review"] is None
    if invalidated:
        with pytest.raises(DeploymentError, match="expired"):
            manager.accept_callback(state, "code")
    else:
        manager.accept_callback(state, "code")
        assert gateway(client, "/setup/status").json["oauth_ready"]
        assert (
            gateway(
                client,
                "/setup/oauth/finish",
                "post",
                headers={"X-CSRF-Token": csrf},
                json={},
            ).status_code
            == 200
        )


@pytest.mark.parametrize("failure", ["timeout", "invalid-id", "interruption"])
def test_ambiguous_import_is_durable_and_blocks_another_deployment(deployment, failure):
    manager, amazon = deployment
    manager.prepare("vendor", SKILL, False)
    review_id = manager.state["review"]["id"]
    original = amazon.request

    def lost_result(method, url, **kwargs):
        if method == "POST" and url.endswith("/imports"):
            # Durable uncertainty must precede the remote acceptance window.
            assert json.loads(manager.path.read_text())["import_unknown"] is True
            response = original(method, url, **kwargs)
            if failure == "timeout":
                raise requests.Timeout()
            if failure == "interruption":
                raise SystemExit()
            response.headers["Location"] = "https://unexpected.example.com/import"
            return response
        return original(method, url, **kwargs)

    amazon.request = lost_result
    with pytest.raises(SystemExit if failure == "interruption" else DeploymentError):
        manager.deploy(review_id)
    assert amazon.imported
    restored = DeploymentManager(manager.path, amazon)
    assert restored.state["import_unknown"]
    assert not restored.public_status()["can_resume"]
    assert "reconcile" in restored.public_status()["message"]
    before = len(amazon.calls)
    with pytest.raises(DeploymentError, match="Reconcile"):
        restored.prepare("vendor", SKILL, False)
    with pytest.raises(DeploymentError, match="Reconcile"):
        restored.deploy(review_id)
    assert len(amazon.calls) == before


def test_definitively_rejected_import_allows_new_review(deployment):
    manager, amazon = deployment
    original = amazon.request

    def rejected(method, url, **kwargs):
        if method == "POST" and url.endswith("/imports"):
            return Response(status=400)
        return original(method, url, **kwargs)

    amazon.request = rejected
    with pytest.raises(DeploymentError):
        deploy(manager)
    assert manager.state["import_unknown"] is False
    manager.prepare("vendor", SKILL, False)
    assert manager.state["phase"] == "ready"


@pytest.mark.parametrize("other_account", [True, False])
def test_resume_checks_original_vendor_and_skill_ownership(deployment, other_account):
    manager, amazon = deployment
    deploy(manager)
    manager.update(import_path="/v1/skills/imports/import-1", phase="interrupted")
    original = amazon.request

    def changed_connection(method, url, **kwargs):
        if url == API + "/v1/vendors" and other_account:
            return Response({"vendors": [{"id": "different-vendor", "name": "Other"}]})
        if url == API + "/v1/skills" and method == "GET" and not other_account:
            return Response({"skills": []})
        return original(method, url, **kwargs)

    amazon.request = changed_connection
    amazon.calls.clear()
    with pytest.raises(DeploymentError):
        manager.finish()
    assert manager.state["phase"] != "complete"
    assert not any(method == "PUT" for method, _, _ in amazon.calls)
    assert not any("/imports/" in url for _, url, _ in amazon.calls)


def uncertain_import(manager):
    manager.update(
        import_unknown=True,
        import_path=None,
        import_attempt_id="pending-attempt",
        vendor_id="vendor",
        skill_id=SKILL,
        phase="interrupted",
    )
    return {
        "attempt_id": "pending-attempt",
        "skill_id": SKILL,
        "result": "FAILED",
        "reference": "Amazon support confirmed this attempt finished",
        "confirmed": True,
    }


@pytest.mark.parametrize("result", ["SUCCEEDED", "FAILED", "NOT_ACCEPTED"])
def test_owner_reconciliation_records_terminal_result_before_clearing(
    deployment, client, monkeypatch, result
):
    from app import app
    from test_ingress import gateway

    manager, amazon = deployment
    app.extensions["skill_deployment"] = manager
    monkeypatch.setenv("HA_INGRESS_ENABLED", "true")
    payload = uncertain_import(manager)
    payload["result"] = result
    csrf = gateway(client, "/setup/status").json["csrf"]
    assert (
        client.post("/setup/reconcile-import", json=payload, headers=AUTH).status_code
        == 403
    )
    assert (
        gateway(client, "/setup/reconcile-import", "post", json=payload).status_code
        == 403
    )
    response = gateway(
        client,
        "/setup/reconcile-import",
        "post",
        json=payload,
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    saved = json.loads(manager.path.read_text())
    assert saved["import_unknown"] is False
    record = saved["import_reconciliations"][-1]
    assert (
        record["result"] == result and record["source"] == "owner_confirmed_with_amazon"
    )
    assert record["attempt_id"] == "pending-attempt" and record["skill_id"] == SKILL
    assert record["vendor_id"] == "vendor" and record["recorded_at"] > 0
    assert record["reference"] == payload["reference"]
    assert saved["phase"] == "reconciled" and saved["verified_at"] is None
    assert saved["review"] is None and amazon.created == 0 and not amazon.imported
    manager.prepare("vendor", SKILL, False)
    assert manager.state["phase"] == "ready"


@pytest.mark.parametrize(
    "changes",
    [
        {"confirmed": False},
        {"confirmed": "true"},
        {"result": "IN_PROGRESS"},
        {"result": "UNKNOWN"},
        {"result": ""},
        {"reference": ""},
        {"reference": "bad\nreference"},
        {"skill_id": OTHER},
        {"attempt_id": "stale-attempt"},
    ],
)
def test_reconciliation_requires_explicit_current_amazon_confirmation(
    deployment, changes
):
    manager, _ = deployment
    payload = uncertain_import(manager)
    payload.update(changes)
    with pytest.raises(DeploymentError):
        manager.reconcile_import(payload)
    assert manager.state["import_unknown"] is True
    assert "import_reconciliations" not in manager.state


def test_reconciliation_blocks_busy_jobs_and_other_account(deployment):
    manager, amazon = deployment
    payload = uncertain_import(manager)
    manager.update(phase="running")
    with pytest.raises(DeploymentError, match="current operation"):
        manager.reconcile_import(payload)
    manager.update(phase="interrupted")
    original = amazon.request

    def other_vendor(method, url, **kwargs):
        if url == API + "/v1/vendors":
            return Response({"vendors": [{"id": "other-vendor"}]})
        return original(method, url, **kwargs)

    amazon.request = other_vendor
    with pytest.raises(DeploymentError):
        manager.reconcile_import(payload)
    assert manager.state["import_unknown"] is True


def test_failed_reconciliation_save_keeps_both_blocks(deployment, monkeypatch):
    import skill_deployment

    manager, _ = deployment
    payload = uncertain_import(manager)

    def failed_write(*_args):
        raise OSError("simulated persistence failure")

    monkeypatch.setattr(skill_deployment, "atomic_write", failed_write)
    with pytest.raises(DeploymentError, match="Could not record"):
        manager.reconcile_import(payload)
    assert manager.state["import_unknown"] is True
    assert json.loads(manager.path.read_text())["import_unknown"] is True
    assert "import_reconciliations" not in manager.state


def test_restart_gives_legacy_uncertain_import_a_stable_attempt_id(deployment):
    manager, amazon = deployment
    uncertain_import(manager)
    manager.state.pop("import_attempt_id")
    manager.save()
    restored = DeploymentManager(manager.path, amazon)
    attempt = restored.public_status()["import_attempt_id"]
    assert attempt
    assert (
        DeploymentManager(manager.path, amazon).public_status()["import_attempt_id"]
        == attempt
    )
