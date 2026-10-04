"""Self-hosted personal skill deployment. Only development-stage APIs are used.

Amazon API bodies, OAuth codes, tokens and presigned URLs must never be logged.
Jobs run in the app's single worker; private state survives worker replacement.
"""

import copy
import hashlib
import io
import json
import os
import re
import secrets
import tempfile
import threading
import time
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlencode, urlparse

import requests
from app_settings import SettingsError, callback_url, get_setting, store
from env_secrets import get_env_secret
from flask import Blueprint, Response, current_app, jsonify, render_template, request

APP_DIR = Path(__file__).resolve().parent
API = "https://api.amazonalexa.com"
TOKEN_URL = "https://api.amazon.com/auth/o2/token"
MAX_PACKAGE = 10 * 1024 * 1024
BUSY = {"preparing", "running"}
_MANAGER_LOCK = threading.Lock()


class DeploymentError(Exception):
    """An intentionally sanitized error suitable for displaying to the owner."""


class AmazonError(DeploymentError):
    def __init__(self, status):
        self.status = status
        super().__init__(
            f"Amazon returned HTTP {status}. Check permissions/settings and retry; reconnect for 401/403."
        )


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def https_url(value, label):
    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise DeploymentError(f"{label} has an invalid port.") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or port not in (None, 443)
    ):
        raise DeploymentError(
            f"{label} must be a public HTTPS URL on port 443 without credentials, query or fragment."
        )
    return value


def settings():
    endpoint = https_url(get_setting("SKILL_HOSTNAME", "").strip(), "Skill endpoint")
    locale = get_setting("LOCALE", "en-US")
    if (
        not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", locale)
        or not (APP_DIR / "models" / f"{locale}.json").is_file()
    ):
        raise DeploymentError("The configured locale has no bundled interaction model.")
    certificate = get_setting("SKILL_CERTIFICATE_TYPE", "Trusted")
    if certificate not in ("Trusted", "Wildcard"):
        raise DeploymentError(
            "Choose Trusted or Wildcard for the public HTTPS certificate."
        )
    return {
        "endpoint": endpoint,
        "locale": locale,
        "certificate": certificate,
        "apl": get_setting("ENABLE_APL", "false").lower() in ("true", "1", "yes", "on"),
    }


def oauth_config():
    client = get_env_secret("LWA_CLIENT_ID")
    secret = get_env_secret("LWA_CLIENT_SECRET")
    callback = https_url(
        callback_url(get_setting("SKILL_HOSTNAME"))
        or os.environ.get("LWA_REDIRECT_URI", "").strip(),
        "Amazon callback",
    )
    if urlparse(callback).path != "/ma-alexa-skill/setup/oauth/callback":
        raise DeploymentError(
            "Amazon callback path must be /ma-alexa-skill/setup/oauth/callback."
        )
    if not client or not secret:
        raise DeploymentError(
            "Configure the Login with Amazon client ID and secret first."
        )
    return client, secret, callback


def config_key():
    return digest("\0".join(oauth_config()))


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".skill-deployment-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)  # mkstemp creates mode 0600, including replacement
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def unpack_package(content):
    """Read bounded ZIP members in memory, never extract paths to disk."""
    files = {}
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            if sum(member.file_size for member in archive.infolist()) > MAX_PACKAGE:
                raise DeploymentError(
                    "Amazon skill package exceeds the supported 10 MiB limit."
                )
            for member in archive.infolist():
                if member.is_dir():
                    continue
                path = PurePosixPath(member.filename)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or "\\" in member.filename
                    or member.filename in files
                ):
                    raise DeploymentError("Amazon returned an unsafe skill package.")
                files[member.filename] = archive.read(member)
    except (zipfile.BadZipFile, RuntimeError) as exc:
        raise DeploymentError("Amazon returned an unreadable skill package.") from exc
    manifests = [key for key in files if PurePosixPath(key).name == "skill.json"]
    if len(manifests) != 1:
        raise DeploymentError("The skill package must contain exactly one skill.json.")
    return files, manifests[0]


def package_fingerprint(files):
    # ZIP metadata varies between exports; compare canonical member content.
    normalized = {}
    for key, content in files.items():
        if key.endswith(".json"):
            content = json.dumps(
                json.loads(content), sort_keys=True, separators=(",", ":")
            ).encode()
        normalized[key] = hashlib.sha256(content).hexdigest()
    return digest(json.dumps(normalized, sort_keys=True))


def pack(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return output.getvalue()


def configure_package(files, manifest_path, config, existing):
    files = copy.copy(files)
    manifest = json.loads(files[manifest_path])["manifest"]
    pub = manifest.setdefault("publishingInformation", {})
    locale = config["locale"]
    locales = pub.setdefault("locales", {})
    if locale not in locales:
        locales[locale] = json.loads((APP_DIR / "skill.json").read_text())["manifest"][
            "publishingInformation"
        ]["locales"][locale]
    if not existing:
        pub["locales"] = {locale: locales[locale]}
        locales[locale]["name"] = "Music Assistant"
        if locale.startswith("en-"):
            locales[locale]["examplePhrases"] = [
                "Alexa, open music assistant",
                "Alexa, ask music assistant to play",
                "Alexa, play music assistant",
            ]
    custom = manifest.setdefault("apis", {}).setdefault("custom", {})
    endpoint = {"uri": config["endpoint"], "sslCertificateType": config["certificate"]}
    custom["endpoint"] = endpoint
    for region in custom.get("regions", {}).values():
        region["endpoint"] = copy.copy(endpoint)
    interfaces = custom.setdefault("interfaces", [])
    interfaces[:] = [
        item
        for item in interfaces
        if item.get("type") != "ALEXA_PRESENTATION_APL" or config["apl"]
    ]
    for interface in ["AUDIO_PLAYER"] + (
        ["ALEXA_PRESENTATION_APL"] if config["apl"] else []
    ):
        if not any(item.get("type") == interface for item in interfaces):
            interfaces.append({"type": interface})
    prefix = str(PurePosixPath(manifest_path).parent)
    prefix = "" if prefix == "." else prefix + "/"
    model_path = f"{prefix}interactionModels/custom/{locale}.json"
    model = json.loads((APP_DIR / "models" / f"{locale}.json").read_text())
    if model_path in files:
        original = json.loads(files[model_path])
        invocation = (
            original.get("interactionModel", {})
            .get("languageModel", {})
            .get("invocationName")
        )
        if invocation:
            model["interactionModel"]["languageModel"]["invocationName"] = invocation
    elif locale.startswith("en-"):
        model["interactionModel"]["languageModel"]["invocationName"] = "music assistant"
    # The bundled model intentionally replaces this locale's intents/slots.
    files[model_path] = json.dumps(model).encode()
    files[manifest_path] = json.dumps({"manifest": manifest}).encode()
    return files, manifest, model["interactionModel"]["languageModel"]["invocationName"]


class DeploymentManager:
    def __init__(self, path, transport=requests):
        self.path = Path(path)
        self.package_path = self.path.with_suffix(".zip")
        self.http = transport
        self.lock = threading.RLock()
        self.cancel = threading.Event()
        self.thread = None
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {}
        if self.state.get("import_unknown") and not self.state.get("import_attempt_id"):
            self.state["import_attempt_id"] = secrets.token_urlsafe(24)
            self.save()
        if self.state.get("phase") in BUSY:
            self.state.update(
                phase="interrupted",
                message="Deployment interrupted by restart. Resume an accepted import, or review the selected skill again.",
            )
            self.save()

    def save(self):
        atomic_write(self.path, json.dumps(self.state).encode())

    def update(self, **values):
        with self.lock:
            self.state.update(values)
            self.save()

    def public_status(self):
        with self.lock:
            try:
                oauth_config()  # Validate prerequisites even before tokens exist.
                ready = (
                    bool(self.state.get("tokens"))
                    and self.state.get("client_key") == config_key()
                )
                error = None
            except (DeploymentError, ValueError) as exc:
                ready, error = False, str(exc)
            result = {
                key: copy.deepcopy(self.state.get(key))
                for key in (
                    "phase",
                    "message",
                    "skill_id",
                    "vendor_id",
                    "creation_unknown",
                    "review",
                    "verified_at",
                    "import_attempt_id",
                )
            }
            if self.state.get("import_unknown"):
                result["message"] = (
                    "Amazon may have accepted an import without returning its recovery ID. Further deployments are blocked; reconcile the operation with Amazon before retrying."
                )
            result.update(
                import_unknown=bool(self.state.get("import_unknown")),
                connected=ready,
                configuration_error=error,
                can_resume=bool(self.state.get("import_path"))
                and result["phase"] not in BUSY,
            )
            return result

    def authorize(self, browser_nonce):
        with self.lock:
            if self.state.get("phase") in BUSY:
                raise DeploymentError(
                    "Wait for the current operation before reconnecting."
                )
            client, _, callback = oauth_config()
            state = secrets.token_urlsafe(32)
            self.state["oauth"] = {
                "state": digest(state),
                "browser": digest(browser_nonce),
                "expires": time.time() + 600,
                "client_key": config_key(),
            }
            self.save()
        return "https://www.amazon.com/ap/oa?" + urlencode(
            {
                "client_id": client,
                "redirect_uri": callback,
                "response_type": "code",
                "scope": "alexa::ask:skills:readwrite alexa::ask:models:readwrite",
                "state": state,
            }
        )

    def accept_callback(self, state, code):
        """Public return endpoint: retain code, never connect an account here."""
        with self.lock:
            pending = self.state.get("oauth", {})
            if (
                pending.get("expires", 0) < time.time()
                or not code
                or not secrets.compare_digest(pending.get("state", ""), digest(state))
                or pending.get("client_key") != config_key()
            ):
                raise DeploymentError(
                    "Sign-in expired or was already returned. Connect Amazon again."
                )
            pending.pop("state")
            pending["code"] = code
            self.save()

    def complete_authorization(self, browser_nonce):
        with self.lock:
            pending = self.state.get("oauth", {})
            if (
                pending.get("expires", 0) < time.time()
                or not pending.get("code")
                or not secrets.compare_digest(
                    pending.get("browser", ""), digest(browser_nonce)
                )
                or pending.get("client_key") != config_key()
                or self.state.get("phase") in BUSY
            ):
                raise DeploymentError(
                    "Sign-in expired or did not match this browser. Connect Amazon again."
                )
            self.state.pop("oauth")
            self.state.pop("review", None)
            self.save()
            self._token(
                {
                    "grant_type": "authorization_code",
                    "code": pending["code"],
                    "redirect_uri": oauth_config()[2],
                }
            )
            self.state.update(
                client_key=config_key(),
                message="Amazon connected. Select the developer account and personal skill.",
            )
            self.save()

    def callback(self, state, browser_nonce, code):
        self.accept_callback(state, code)
        self.complete_authorization(browser_nonce)

    def _token(self, payload):
        client, secret, _ = oauth_config()
        try:
            response = self.http.post(
                TOKEN_URL,
                data={**payload, "client_id": client, "client_secret": secret},
                timeout=30,
                allow_redirects=False,
            )
            if response.status_code != 200:
                self.state.pop("tokens", None)
                self.save()
                raise DeploymentError(
                    "Amazon sign-in needs reconnection. Check the Login with Amazon registration."
                )
            body = response.json()
            refresh = body.get("refresh_token") or self.state.get("tokens", {}).get(
                "refresh_token"
            )
            if not body.get("access_token") or not refresh:
                raise DeploymentError(
                    "Amazon did not return the required credentials. Reconnect."
                )
            self.state["tokens"] = {
                "access_token": body["access_token"],
                "refresh_token": refresh,
                "expires": time.time() + int(body.get("expires_in", 3600)),
            }
            self.save()
        except (requests.RequestException, ValueError) as exc:
            raise DeploymentError(
                "Amazon sign-in request failed. Reconnect and try again."
            ) from exc

    def api(self, method, path, **kwargs):
        if self.cancel.is_set():
            raise DeploymentError("Deployment interrupted by shutdown.")
        if not path.startswith("/v1/"):
            raise DeploymentError("Unexpected Amazon API path.")
        for attempt in range(3):
            with self.lock:
                if self.state.get("client_key") != config_key() or not self.state.get(
                    "tokens"
                ):
                    raise DeploymentError("Connect Amazon before deploying.")
                if self.state["tokens"]["expires"] < time.time() + 60:
                    self._token(
                        {
                            "grant_type": "refresh_token",
                            "refresh_token": self.state["tokens"]["refresh_token"],
                        }
                    )
                token = self.state["tokens"]["access_token"]
            try:
                response = self.http.request(
                    method,
                    API + path,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=30,
                    allow_redirects=False,
                    **kwargs,
                )
            except requests.RequestException as exc:
                if method == "GET" and attempt < 2:
                    self.pause(2**attempt)
                    continue
                raise DeploymentError(
                    "Amazon request timed out or could not connect. Check progress before retrying."
                ) from exc
            if response.status_code == 401 and attempt == 0:
                with self.lock:
                    self._token(
                        {
                            "grant_type": "refresh_token",
                            "refresh_token": self.state["tokens"]["refresh_token"],
                        }
                    )
                continue  # explicit rejection: no operation was accepted
            if (
                method == "GET"
                and response.status_code in (429, 500, 503)
                and attempt < 2
            ):
                self.pause(2**attempt)
                continue
            if not 200 <= response.status_code < 300:
                raise AmazonError(response.status_code)
            return response
        raise DeploymentError("Amazon is busy. Try again later.")

    def pause(self, seconds):
        if self.cancel.wait(seconds):
            raise DeploymentError(
                "Deployment interrupted by shutdown; resume or review again after restart."
            )

    def accounts(self):
        return self.api("GET", "/v1/vendors").json().get("vendors", [])

    def skills(self, vendor):
        if not any(item["id"] == vendor for item in self.accounts()):
            raise DeploymentError(
                "Choose a developer account from this Amazon connection."
            )
        result, token = {}, None
        while True:
            params = {"vendorId": vendor, "maxResults": 50}
            if token:
                params["nextToken"] = token
            body = self.api("GET", "/v1/skills", params=params).json()
            for skill in body.get("skills", []):
                if skill.get("stage") == "development" and "custom" in skill.get(
                    "apis", []
                ):
                    result[skill["skillId"]] = {
                        "id": skill["skillId"],
                        "names": skill.get("nameByLocale", {}),
                    }
            new_token = body.get("nextToken")
            if not new_token:
                return list(result.values())
            if new_token == token:
                raise DeploymentError("Amazon returned an invalid page token.")
            token = new_token

    def start_job(self, phase, operation, *args):
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise DeploymentError("An operation is already running.")
            self.cancel.clear()
            self.update(
                phase=phase,
                message="Preparing review…"
                if phase == "preparing"
                else "Deploying personal skill…",
            )
            self.thread = threading.Thread(
                target=self._run, args=(operation, args), daemon=True
            )
            self.thread.start()

    def _run(self, operation, args):
        try:
            operation(*args)
        except DeploymentError as exc:
            self.update(
                phase="interrupted" if self.cancel.is_set() else "failed",
                message=str(exc),
            )
        except Exception:  # noqa: BLE001 - keep the job failure private and observable
            # Do not include exception text: requests/JSON errors can contain secrets.
            self.update(
                phase="failed",
                message="Deployment could not finish. Review settings and reconnect if necessary.",
            )

    def stop(self):
        self.cancel.set()
        if self.state.get("phase") in BUSY:
            self.update(
                phase="interrupted",
                message="Deployment interrupted by shutdown. Resume or review again.",
            )

    def poll(self, path, label):
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            body = self.api("GET", path).json()
            if body.get("status") == "SUCCEEDED":
                return body
            if body.get("status") == "FAILED":
                raise DeploymentError(
                    f"Amazon {label} failed. Open the selected skill in the developer console for build diagnostics, then review and retry."
                )
            if body.get("status") != "IN_PROGRESS":
                raise DeploymentError(f"Amazon returned an unexpected {label} status.")
            self.pause(3)
        raise DeploymentError(
            f"Amazon {label} is taking longer than ten minutes. Resume the import or review again later."
        )

    @staticmethod
    def job_path(response, kind):
        parsed = urlparse(response.headers.get("Location", ""))
        if parsed.netloc and parsed.netloc != urlparse(API).netloc:
            raise DeploymentError("Unexpected Amazon operation URL.")
        if (
            not re.fullmatch(r"/v1/skills/" + kind + r"/[A-Za-z0-9._-]+", parsed.path)
            or parsed.query
            or parsed.fragment
        ):
            raise DeploymentError("Amazon did not return a valid operation ID.")
        return parsed.path

    def transfer(self, method, url, **kwargs):
        parsed = urlparse(url)
        # URLs originate from authenticated SMAPI, but still restrict downloads/uploads.
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or not parsed.hostname.endswith(".amazonaws.com")
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
        ):
            raise DeploymentError("Amazon returned an unexpected package storage URL.")
        try:
            response = self.http.request(
                method, url, timeout=30, allow_redirects=False, **kwargs
            )
            if not 200 <= response.status_code < 300:
                raise DeploymentError(
                    "Amazon package transfer failed. Review and try again."
                )
            return response
        except requests.RequestException as exc:
            raise DeploymentError(
                "Amazon package transfer could not finish. Review and try again."
            ) from exc

    def export(self, skill):
        response = self.api(
            "POST", f"/v1/skills/{quote(skill, safe='')}/stages/development/exports"
        )
        status = self.poll(self.job_path(response, "exports"), "package export")
        with self.transfer("GET", status["skill"]["location"], stream=True) as response:
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > MAX_PACKAGE:
                    raise DeploymentError(
                        "Amazon package exceeds the supported 10 MiB limit."
                    )
                chunks.append(chunk)
            return unpack_package(b"".join(chunks))

    def require_known_import(self):
        if self.state.get("import_unknown"):
            raise DeploymentError(
                "An import may still be running in Amazon without a saved recovery ID. Reconcile it with Amazon before another deployment."
            )

    def prepare(self, vendor, skill, create):
        self.require_known_import()
        config = settings()
        if self.state.get("import_path"):
            status = self.api("GET", self.state["import_path"]).json().get("status")
            if status not in ("SUCCEEDED", "FAILED"):
                raise DeploymentError(
                    "An accepted import is still running. Resume it before preparing another deployment."
                )
            self.update(import_path=None)
        available = self.skills(vendor)
        if create:
            if (
                skill
                or self.state.get("skill_id")
                or self.state.get("creation_unknown")
            ):
                raise DeploymentError(
                    "A skill is already saved or creation is uncertain. Select the existing skill by ID; automatic creation is blocked to prevent duplicates."
                )
            files = {"skill-package/skill.json": (APP_DIR / "skill.json").read_bytes()}
            manifest_path, baseline = "skill-package/skill.json", None
        else:
            if not any(item["id"] == skill for item in available):
                raise DeploymentError(
                    "Select a development custom skill from this developer account."
                )
            files, manifest_path = self.export(skill)
            baseline = package_fingerprint(files)
        files, manifest, invocation = configure_package(
            files, manifest_path, config, not create
        )
        atomic_write(self.package_path, pack(files))
        review = {
            "id": secrets.token_urlsafe(24),
            "expires": time.time() + 600,
            "vendor": vendor,
            "skill": skill,
            "create": create,
            "settings": config,
            "name": manifest["publishingInformation"]["locales"][config["locale"]][
                "name"
            ],
            "invocation": invocation,
            "locales": list(manifest["publishingInformation"]["locales"]),
            "manifest": manifest,
        }
        self.update(
            review=review,
            baseline=baseline,
            phase="ready",
            message="Review these settings before deploying. Only the selected locale’s voice model will be replaced.",
        )

    def deploy(self, review_id):
        with self.lock:
            self.require_known_import()
            review = copy.deepcopy(self.state.get("review", {}))
            if (
                review.get("id") != review_id
                or review.get("expires", 0) < time.time()
                or review.get("settings") != settings()
            ):
                raise DeploymentError(
                    "The review expired or settings changed. Prepare a new review."
                )
            self.state.pop("review", None)  # cannot replay this approval
            self.save()
        skill = review["skill"]
        if skill:
            fresh, _ = self.export(skill)
            if package_fingerprint(fresh) != self.state["baseline"]:
                raise DeploymentError(
                    "The skill changed in Amazon since the review. Prepare a new review to preserve those changes."
                )
        else:
            self.update(creation_unknown=True)
            try:
                response = self.api(
                    "POST",
                    "/v1/skills",
                    json={"vendorId": review["vendor"], "manifest": review["manifest"]},
                )
            except AmazonError as exc:
                if 400 <= exc.status < 500:
                    self.update(creation_unknown=False)
                raise
            skill = response.json().get("skillId")
            if not isinstance(skill, str) or not re.fullmatch(
                r"amzn1\.(?:ask|alexa)\.skill\.[A-Za-z0-9._-]+", skill
            ):
                raise DeploymentError(
                    "Creation result is uncertain. Refresh Amazon’s skill list and select the skill; do not create another."
                )
        self.update(
            skill_id=skill,
            vendor_id=review["vendor"],
            creation_unknown=False,
            deployed_settings=review["settings"],
            import_path=None,
            verified_at=None,
            expected_package=package_fingerprint(
                unpack_package(self.package_path.read_bytes())[0]
            ),
        )
        if review["create"]:
            self.wait_manifest(skill)
        uploaded = self.api("POST", "/v1/skills/uploads").json()["uploadUrl"]
        self.transfer(
            "PUT",
            uploaded,
            data=self.package_path.read_bytes(),
            headers={"Content-Type": "application/zip"},
        )
        # Persist the uncertain transition BEFORE Amazon can accept the mutation.
        # A lost response, invalid operation URL or shutdown must block another import.
        self.update(import_unknown=True, import_attempt_id=secrets.token_urlsafe(24))
        try:
            response = self.api(
                "POST",
                f"/v1/skills/{quote(skill, safe='')}/imports",
                json={"location": uploaded},
            )
        except AmazonError as exc:
            if 400 <= exc.status < 500:
                self.update(import_unknown=False)
            raise
        self.update(
            import_path=self.job_path(response, "imports"),
            import_unknown=False,
            message="Amazon is importing and building the skill…",
        )
        self.finish()

    def reconcile_import(self, payload):
        """Record an owner's Amazon-confirmed terminal result before releasing a block."""
        with self.lock:
            if self.state.get("phase") in BUSY or (
                self.thread and self.thread.is_alive()
            ):
                raise DeploymentError(
                    "Wait for the current operation before reconciling."
                )
            if not self.state.get("import_unknown") or self.state.get("import_path"):
                raise DeploymentError(
                    "There is no import without a recovery ID to reconcile."
                )
            if not isinstance(payload, dict) or payload.get("confirmed") is not True:
                raise DeploymentError(
                    "Confirm the terminal result with Amazon before continuing."
                )
            if payload.get("attempt_id") != self.state.get(
                "import_attempt_id"
            ) or payload.get("skill_id") != self.state.get("skill_id"):
                raise DeploymentError(
                    "The pending import changed. Reload and verify the displayed skill."
                )
            result, reference = payload.get("result"), payload.get("reference")
            if result not in ("SUCCEEDED", "FAILED", "NOT_ACCEPTED"):
                raise DeploymentError(
                    "Record a confirmed success, failure or non-acceptance; a running or unknown operation cannot be cleared."
                )
            if (
                not isinstance(reference, str)
                or not 1 <= len(reference.strip()) <= 1000
                or any(ord(c) < 32 for c in reference)
            ):
                raise DeploymentError(
                    "Provide Amazon's confirmation reference or an explanation without secrets."
                )
            vendor, skill = self.state.get("vendor_id"), self.state.get("skill_id")
            attempt_id = self.state.get("import_attempt_id")
            client_key = self.state.get("client_key")
            refresh_token = (self.state.get("tokens") or {}).get("refresh_token")

        # Ownership listing can involve multiple slow API pages. Keep status and
        # local settings responsive while it runs; validate the snapshot again below.
        if not vendor or not any(item["id"] == skill for item in self.skills(vendor)):
            raise DeploymentError(
                "Reconnect the original developer account and verify ownership of the saved skill."
            )

        with self.lock:
            if (
                self.state.get("phase") in BUSY
                or (self.thread and self.thread.is_alive())
                or self.cancel.is_set()
                or not self.state.get("import_unknown")
                or self.state.get("import_path")
                or self.state.get("import_attempt_id") != attempt_id
                or self.state.get("skill_id") != skill
                or self.state.get("vendor_id") != vendor
                or self.state.get("client_key") != client_key
                or self.state.get("client_key") != config_key()
                or (self.state.get("tokens") or {}).get("refresh_token")
                != refresh_token
            ):
                raise DeploymentError(
                    "The pending import or Amazon connection changed. Reload and verify the displayed skill."
                )
            state = copy.deepcopy(self.state)
            state.setdefault("import_reconciliations", []).append(
                {
                    "attempt_id": state["import_attempt_id"],
                    "skill_id": skill,
                    "vendor_id": vendor,
                    "result": result,
                    "reference": reference.strip(),
                    "recorded_at": time.time(),
                    "source": "owner_confirmed_with_amazon",
                }
            )
            state.update(
                import_unknown=False,
                review=None,
                verified_at=None,
                phase="reconciled",
                message="Your confirmed Amazon result was recorded. Prepare a fresh review before deploying again.",
            )
            # The confirmation and marker change are committed together; a write failure
            # keeps the in-memory block too. Owner attestation is not API verification.
            try:
                atomic_write(self.path, json.dumps(state).encode())
            except OSError as exc:
                raise DeploymentError(
                    "Could not record the confirmation. The import remains blocked; check private storage and retry."
                ) from exc
            self.state = state

    def wait_manifest(self, skill):
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            body = self.api(
                "GET",
                f"/v1/skills/{quote(skill, safe='')}/status",
                params={"resource": "manifest"},
            ).json()
            status = body.get("manifest", {}).get("lastUpdateRequest", {}).get("status")
            if status == "SUCCEEDED":
                return
            if status == "FAILED":
                raise DeploymentError(
                    "Amazon rejected the skill manifest. Check the developer console before retrying."
                )
            self.pause(3)
        raise DeploymentError(
            "Amazon manifest build timed out. Review the saved skill again."
        )

    def finish(self):
        if not self.state.get("import_path"):
            raise DeploymentError(
                "There is no accepted import to resume. Review the saved skill again."
            )
        vendor, skill = self.state.get("vendor_id"), self.state.get("skill_id")
        if not vendor or not any(item["id"] == skill for item in self.skills(vendor)):
            raise DeploymentError(
                "The saved import must belong to a development custom skill in its original developer account. Reconnect that account before resuming."
            )
        self.poll(self.state["import_path"], "package import")
        skill, config = self.state["skill_id"], self.state["deployed_settings"]
        path = f"/v1/skills/{quote(skill, safe='')}"
        status = self.api(
            "GET",
            path + "/status",
            params=[("resource", "manifest"), ("resource", "interactionModel")],
        ).json()
        model_status = (
            status.get("interactionModel", {})
            .get(config["locale"], {})
            .get("lastUpdateRequest", {})
            .get("status")
        )
        if (
            status.get("manifest", {}).get("lastUpdateRequest", {}).get("status")
            != "SUCCEEDED"
            or model_status != "SUCCEEDED"
        ):
            raise DeploymentError(
                "Amazon has not confirmed a successful manifest and voice-model build. Resume later or check build diagnostics."
            )
        manifest = self.api("GET", path + "/stages/development/manifest").json()[
            "manifest"
        ]
        custom = manifest.get("apis", {}).get("custom", {})
        interfaces = {item["type"] for item in custom.get("interfaces", [])}
        expected = {
            "uri": config["endpoint"],
            "sslCertificateType": config["certificate"],
        }
        endpoints = [custom.get("endpoint", {})] + [
            item.get("endpoint", {}) for item in custom.get("regions", {}).values()
        ]
        if (
            any(
                any(endpoint.get(key) != value for key, value in expected.items())
                for endpoint in endpoints
            )
            or "AUDIO_PLAYER" not in interfaces
            or ("ALEXA_PRESENTATION_APL" in interfaces) != config["apl"]
        ):
            raise DeploymentError(
                "Amazon configuration did not match the approved endpoint/interfaces. Review and retry."
            )
        actual_files, _ = self.export(skill)
        if package_fingerprint(actual_files) != self.state.get("expected_package"):
            raise DeploymentError(
                "Amazon’s exported package differs from the approved deployment. Review again before enabling testing."
            )
        self.api("PUT", path + "/stages/development/enablement")
        enabled = self.api("GET", path + "/stages/development/enablement")
        if enabled.status_code != 204:
            raise DeploymentError("Amazon did not confirm development enablement.")
        self.update(
            phase="complete",
            message="Amazon confirmed the endpoint, voice-model build and development enablement. Test playback on your Echo.",
            verified_at=time.time(),
            import_path=None,
            import_unknown=False,
        )


def manager():
    with _MANAGER_LOCK:
        instance = current_app.extensions.get("skill_deployment")
        if instance is None:
            instance = DeploymentManager(
                os.environ.get("SKILL_DEPLOYMENT_PATH", "/data/skill-deployment.json")
            )
            current_app.extensions["skill_deployment"] = instance
        return instance


def register_setup(app):
    blueprint = Blueprint("personal_setup", __name__)

    @blueprint.before_request
    def protect():
        if (
            request.path
            not in ("/setup/oauth/callback", "/ma-alexa-skill/setup/oauth/callback")
            and not request.environ.get("ma.trusted_ingress")
            and (
                not get_env_secret("APP_USERNAME") or not get_env_secret("APP_PASSWORD")
            )
        ):
            return jsonify(
                error="Set the app username and password before using skill deployment."
            ), 403
        if request.method == "POST":
            cookie, header = (
                request.cookies.get("setup_csrf", ""),
                request.headers.get("X-CSRF-Token", ""),
            )
            if not cookie or not secrets.compare_digest(cookie, header):
                return jsonify(error="Reload the setup page before continuing."), 403

    @blueprint.after_request
    def private(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = (
            "SAMEORIGIN" if request.environ.get("ma.trusted_ingress") else "DENY"
        )
        return response

    @blueprint.errorhandler(DeploymentError)
    def error(exc):
        return jsonify(error=str(exc)), 400

    @blueprint.route("/setup")
    def page():
        return render_template("setup.html")

    @blueprint.route("/setup/status")
    def status():
        response = jsonify(
            **manager().public_status(),
            oauth_ready=bool(manager().state.get("oauth", {}).get("code"))
            and secrets.compare_digest(
                manager().state.get("oauth", {}).get("browser", ""),
                digest(request.cookies.get("setup_oauth", "")),
            ),
            csrf=request.cookies.get("setup_csrf") or secrets.token_urlsafe(32),
        )
        body = response.get_json()
        response.set_cookie(
            "setup_csrf",
            body["csrf"],
            httponly=True,
            secure=request.is_secure,
            samesite="Strict",
            path=request.script_root + "/setup",
        )
        return response

    @blueprint.errorhandler(SettingsError)
    def settings_error(exc):
        return jsonify(error=str(exc)), 400

    @blueprint.route("/setup/settings", methods=["GET", "POST"])
    def application_settings():
        if request.method == "GET":
            return jsonify(**store().public())
        instance = manager()
        with instance.lock:
            if instance.thread and instance.thread.is_alive():
                raise SettingsError(
                    "Wait for the current deployment operation before saving settings."
                )
            try:
                previous_key = config_key()
            except (DeploymentError, ValueError):
                previous_key = None
            result = store().save(request.get_json(silent=True))
            try:
                current_key = config_key()
            except (DeploymentError, ValueError):
                current_key = None
            # Keep pending sign-in for unrelated edits; OAuth binds to its own config.
            changes = {"review": None}
            if previous_key != current_key:
                changes["oauth"] = {}
            instance.update(**changes)
        return jsonify(**result)

    @blueprint.route("/setup/settings/api-password", methods=["POST"])
    def reveal_api_password():
        return jsonify(password=get_env_secret("APP_PASSWORD"))

    @blueprint.route("/setup/oauth/start", methods=["POST"])
    def connect():
        oauth_config()  # validate the separate public callback registration
        nonce = secrets.token_urlsafe(32)
        response = jsonify(url=manager().authorize(nonce))
        response.set_cookie(
            "setup_oauth",
            nonce,
            max_age=600,
            secure=request.is_secure,
            httponly=True,
            samesite="Lax",
            path=request.script_root + "/setup",
        )
        return response

    @blueprint.route("/ma-alexa-skill/setup/oauth/callback")
    @blueprint.route("/setup/oauth/callback")
    def callback():
        manager().accept_callback(
            request.args.get("state", ""), request.args.get("code", "")
        )
        return Response(
            "<!doctype html><title>Amazon sign-in returned</title><p>Return to the Home Assistant setup page to finish connecting Amazon. You can close this window.</p><script>window.close();</script>",
            mimetype="text/html",
        )

    @blueprint.route("/setup/oauth/finish", methods=["POST"])
    def finish_authorization():
        manager().complete_authorization(request.cookies.get("setup_oauth", ""))
        response = jsonify(status="connected")
        response.delete_cookie("setup_oauth", path=request.script_root + "/setup")
        return response

    @blueprint.route("/setup/accounts")
    def accounts():
        return jsonify(vendors=manager().accounts())

    @blueprint.route("/setup/skills")
    def skills():
        return jsonify(skills=manager().skills(request.args.get("vendor", "")))

    @blueprint.route("/setup/preview", methods=["POST"])
    def preview():
        payload = request.get_json() or {}
        if not isinstance(payload, dict) or not isinstance(
            payload.get("create", False), bool
        ):
            raise DeploymentError(
                "Choose an existing skill or explicitly request creation."
            )
        settings()  # fail before spawning a job
        manager().start_job(
            "preparing",
            manager().prepare,
            payload.get("vendor", ""),
            payload.get("skill", ""),
            payload.get("create") is True,
        )
        return jsonify(status="preparing"), 202

    @blueprint.route("/setup/deploy", methods=["POST"])
    def deploy():
        payload = request.get_json() or {}
        if not isinstance(payload, dict) or not isinstance(
            payload.get("review_id"), str
        ):
            raise DeploymentError("Prepare a review before deploying.")
        manager().start_job("running", manager().deploy, payload["review_id"])
        return jsonify(status="running"), 202

    @blueprint.route("/setup/reconcile-import", methods=["POST"])
    def reconcile():
        manager().reconcile_import(request.get_json(silent=True))
        return jsonify(status="reconciled")

    @blueprint.route("/setup/resume", methods=["POST"])
    def resume():
        if not manager().state.get("import_path"):
            raise DeploymentError("No accepted import is available to resume.")
        manager().start_job("running", manager().finish)
        return jsonify(status="running"), 202

    app.register_blueprint(blueprint)
