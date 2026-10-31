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
from app_settings import (
    SettingsError,
    callback_url,
    get_setting,
    store,
    validate_certificate,
)
from env_secrets import get_env_secret
from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from page_nav import nav_html
from reachability import (
    _resolved_addresses,
    _DNS_TIMED_OUT,
    _DNS_UNAVAILABLE,
    _read_health_content,
    _PinnedAdapter,
)

APP_DIR = Path(__file__).resolve().parent
API = "https://api.amazonalexa.com"
TOKEN_URL = "https://api.amazon.com/auth/o2/token"
MAX_PACKAGE = 10 * 1024 * 1024
BUSY = {"preparing", "running"}
_MANAGER_LOCK = threading.Lock()
# Nabu Casa's remote UI proxies by SNI straight to Home Assistant itself: it
# serves the frontend and ingress URLs only, never a custom path such as
# /ma-alexa-skill/ (add-on issue #92), so its hostnames can carry neither the
# Amazon callback nor the deployed skill endpoint.
_NABU_CASA_SUFFIX = ".ui.nabu.casa"
# Shown on step 2 when the probe could not decide: transport failures, or a
# hostname whose local view is private/unresolved, say nothing about the path
# the browser and Amazon take, so sign-in still starts (issue #92 review).
_CALLBACK_UNVERIFIED = (
    "The add-on could not verify that the public callback URL routes back here, "
    "so Amazon sign-in was started anyway. If the account does not connect, "
    "check that the public hostname forwards /ma-alexa-skill/ to this add-on "
    "as described in the add-on docs."
)
# Exact marker only this add-on returns on a code-less callback probe.
# Prevents unrelated 400 JSON with "Sign-in expired" from passing as verified.
_CALLBACK_PROBE_MARKER = "ma-callback-probe"


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
    if certificate not in ("Trusted", "Wildcard", "SelfSigned"):
        raise DeploymentError(
            "Choose Trusted, Trusted sub-domain or File for the public HTTPS certificate."
        )
    pem = ""
    if certificate == "SelfSigned":
        try:
            pem = validate_certificate(get_setting("SKILL_CERTIFICATE_PEM", ""))
        except SettingsError as exc:
            raise DeploymentError(str(exc)) from exc
    return {
        "certificate_pem": pem,
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


def config_key(oauth=None):
    """SHA256 fingerprint of the current OAuth config for change detection.

    The fingerprint is stored in deployment state and backups; it must not
    contain the LWA client secret in plaintext. Use a fast hash because the
    secret is not a password — it is a config value compared for equality
    only. The format matches the pre-change fingerprint so existing
    deployments upgrade without re-connection.
    """
    return hashlib.sha256(
        "\0".join(oauth if oauth is not None else oauth_config()).encode()
    ).hexdigest()


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
    manifests = [key for key in files if PurePosixPath(key).name == SKILL_JSON_NAME]
    if len(manifests) != 1:
        raise DeploymentError("The skill package must contain exactly one skill.json.")
    return files, manifests[0]


ICON_URLS = {
    "small": "https://raw.githubusercontent.com/timlaing/music-assistant-alexa-skill/main/assets/icons/ma_108x108.png",
    "large": "https://raw.githubusercontent.com/timlaing/music-assistant-alexa-skill/main/assets/icons/ma_512x512.png",
}
ICON_SIZES = (("smallIconUri", "small"), ("largeIconUri", "large"))
ICON_ASSET_MARKER = "assets/images/"
SKILL_JSON_NAME = "skill.json"
SETUP_PATH = "/setup"


def normalize_icons(document):
    """Canonicalize icon URIs that Amazon materializes during import, in place.

    A real export cycle, verified against production, shows Amazon downloading
    the manifest's icon URLs, storing the artwork as
    assets/images/<locale>_<size>IconUri.png and rewriting each locale's
    smallIconUri/largeIconUri to that exact file:// form. A value equal to
    the configured artwork URL or to its own locale's materialized path
    canonicalizes to one token, so both sides of every comparison judge the
    icons equal. Any other value — a foreign URL, a cross-locale path, a
    missing key — stays as written and keeps failing validation.
    """
    locales = (
        document.get("manifest", {}).get("publishingInformation", {}).get("locales", {})
    )
    if not isinstance(locales, dict):
        return
    for locale, entry in locales.items():
        if not isinstance(entry, dict):
            continue
        for key, size in ICON_SIZES:
            value = entry.get(key)
            materialized = f"file://assets/images/{locale}_{size}IconUri.png"
            if value in (ICON_URLS[size], materialized):
                entry[key] = f"ma-icon:{size}"


def canonical_json(content):
    """Canonical byte form of a JSON package file for comparison.

    Shared by the fingerprint and the mismatch report so both judge file
    differences identically: whitespace, key order and Amazon's icon
    materialization are never differences.
    """
    document = json.loads(content)
    normalize_icons(document)
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode()


def package_members(files):
    """Map each member's name without Amazon's skill-package/ wrapper to its key.

    Amazon exports the package with or without one leading skill-package/
    folder. Two members that differ only by that wrapper would compare as
    one file, letting either hide the other, so such a package is refused.
    """
    members = {}
    for key in files:
        name = key.removeprefix("skill-package/")
        if name in members:
            raise DeploymentError(f"The skill package holds {name} twice.")
        members[name] = key
    return members


def package_fingerprint(files, members=None):
    # ZIP metadata varies between exports; compare canonical member content.
    normalized = {}
    for name, key in (members or package_members(files)).items():
        content = files[key]
        if name.endswith(".json"):
            content = canonical_json(content)
        normalized[name] = hashlib.sha256(content).hexdigest()
    return digest(json.dumps(normalized, sort_keys=True))


def package_fingerprints(files):
    """The fingerprint, and the one older versions saved with the wrapper in names.

    A review saved before an upgrade holds the older form and must still match.
    """
    return {
        package_fingerprint(files),
        package_fingerprint(files, {key: key for key in files}),
    }


def _diff_key(key, left, right, path, differences, walk):
    """Record the difference for one key of two dicts, then recurse."""
    child = f"{path}.{key}" if path else key
    if key not in left:
        differences.append(f"{child} (added by Amazon)")
    elif key not in right:
        differences.append(f"{child} (missing from Amazon's export)")
    else:
        walk(left[key], right[key], child)


def _json_diff(approved, actual, limit=6):
    """List the JSON paths where two parsed documents differ.

    Only paths are reported, never values, so the result is safe to show
    in the status API and error messages.
    """
    differences = []

    def walk(left, right, path):
        if len(differences) >= limit:
            return
        if isinstance(left, dict) and isinstance(right, dict):
            for key in sorted(set(left) | set(right)):
                _diff_key(key, left, right, path, differences, walk)
        elif left != right or type(left) is not type(right):
            differences.append(f"{path}: value differs" if path else "value differs")

    walk(approved, actual, "")
    return differences


def _json_file_differences(approved, actual):
    """Per-file differences for two JSON members; empty when equivalent."""
    try:
        if canonical_json(approved) == canonical_json(actual):
            return []  # only formatting or materialized icons differ
        left = json.loads(approved)
        right = json.loads(actual)
        normalize_icons(left)
        normalize_icons(right)
        return _json_diff(left, right)
    except (ValueError, TypeError):
        return []


def package_mismatch(approved, actual):
    """Describe per-file differences between two unpacked skill packages."""
    report = {}
    approved_members = package_members(approved)
    actual_members = package_members(actual)
    for name in sorted(set(approved_members) | set(actual_members)):
        # Report under the approved package's name for the member.
        key = approved_members.get(name) or actual_members[name]
        if name not in approved_members:
            report[key] = ["file exists only in Amazon's export"]
            continue
        if name not in actual_members:
            report[key] = ["file missing from Amazon's export"]
            continue
        left = approved[approved_members[name]]
        right = actual[actual_members[name]]
        if left == right:
            continue
        if name.endswith(".json"):
            differences = _json_file_differences(left, right)
            if differences:
                report[key] = differences
        else:
            report[key] = ["file bytes differ"]
    return report


def store_icon_assets(approved, actual):
    """Adopt Amazon's icon artwork bytes into the approved package.

    Amazon materializes and re-encodes assets/images/*IconUri.png once per
    artwork change; the re-encoded bytes are stable across later cycles
    (verified against production). Storing Amazon's exported artwork makes
    the approved package match the deployment it just verified, so later
    cycles compare bytes directly. Only icon artwork is touched — every
    other member keeps byte-for-byte verification.
    """
    stored = []
    approved_members = package_members(approved)
    actual_members = package_members(actual)
    for name, key in actual_members.items():
        # Replace an approved member in place, wrapper or not, so the
        # package never holds the same artwork under two names.
        target = approved_members.get(name, key)
        if ICON_ASSET_MARKER in name and approved.get(target) != actual[key]:
            approved[target] = actual[key]
            stored.append(target)
    for name, key in sorted(approved_members.items()):
        if ICON_ASSET_MARKER in name and name not in actual_members:
            del approved[key]
            stored.append(key)
    return sorted(stored)


def pack(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return output.getvalue()


def _configured_model(locale):
    """Load the bundled voice model for a locale.

    The bundled JSON is the source of truth, invocation name included:
    Music Assistant addresses the skill by that name in every locale
    (ALEXA_LANGUAGE_COMMANDS in its alexa provider), so an invocation
    preserved from an older deployment would keep MA-initiated playback
    silent (issue #71). Existing deployments adopt the bundled name on
    their next deploy.
    """
    model = json.loads((APP_DIR / "models" / f"{locale}.json").read_text())
    # Amazon's import build canonicalizes models to include the custom slot
    # type list. A bundled model that omits it is re-exported with
    # "types": [], so the approved-package fingerprint would never match
    # and deployment would be blocked for that locale.
    language = model["interactionModel"]["languageModel"]
    language.setdefault("types", [])
    return model


def _package_invocation(files, locale):
    """Invocation name of a locale's model as it stands in a skill package.

    Suffix-matched so it works for both Amazon exports and the local
    package zip, whatever the manifest's directory prefix is.
    """
    suffix = f"interactionModels/custom/{locale}.json"
    for name, raw in files.items():
        if not name.endswith(suffix):
            continue
        try:
            model = json.loads(raw)
            name = model["interactionModel"]["languageModel"]["invocationName"]
        except (ValueError, KeyError, TypeError):
            return None
        return name or None
    return None


def configure_package(files, manifest_path, config, existing):
    files = copy.copy(files)
    manifest = json.loads(files[manifest_path])["manifest"]
    pub = manifest.setdefault("publishingInformation", {})
    locale = config["locale"]
    locales = pub.setdefault("locales", {})
    if locale not in locales:
        template = json.loads((APP_DIR / SKILL_JSON_NAME).read_text())["manifest"][
            "publishingInformation"
        ]["locales"]
        # A bundled model can outpace the template's publishing entries; fall
        # back to the en-US entry so deployment still yields a complete
        # manifest instead of failing on a missing locale.
        locales[locale] = template.get(locale, template["en-US"])
    if not existing:
        pub["locales"] = {locale: locales[locale]}
        locales[locale]["name"] = "Music Assistant"
        if locale.startswith("en-"):
            locales[locale]["examplePhrases"] = [
                "Alexa, open music assistant",
                "Alexa, ask music assistant to play",
                "Alexa, play music assistant",
            ]
    for publishing_locale in locales.values():
        publishing_locale["smallIconUri"] = ICON_URLS["small"]
        publishing_locale["largeIconUri"] = ICON_URLS["large"]
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
    model = _configured_model(locale)
    # The bundled model intentionally replaces this locale's intents/slots.
    files[model_path] = json.dumps(model).encode()
    files[manifest_path] = json.dumps({"manifest": manifest}).encode()
    invocation_name = model["interactionModel"]["languageModel"]["invocationName"]
    return files, manifest, invocation_name


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
                    "package_mismatch",
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

    def authorize(self, browser_nonce, oauth=None):
        """Build Amazon's consent URL from one oauth_config() snapshot.

        The setup route reads the config once and passes the same tuple here
        and to ensure_callback_reachable, so the probed callback and the one
        registered with Amazon cannot diverge mid-request (issue #92 review).

        Rejects a new start while an OAuth flow is pending to prevent the
        earlier callback from being invalidated by a later retry (Sourcery
        review). Allows retry when the pending flow has no code (lost start
        response) or is expired. Rejects stale pending only when its saved
        fingerprint matches the current configuration.
        """
        with self.lock:
            if self.state.get("phase") in BUSY:
                raise DeploymentError(
                    "Wait for the current operation before reconnecting."
                )
            pending = self.state.get("oauth", {})
            oauth = oauth if oauth is not None else oauth_config()
            current_fp = config_key(oauth)
            saved_fp = pending.get("client_key")
            has_code = pending.get("code") is not None
            is_expired = pending.get("expires", 0) <= time.time()

            # If a valid code is already stored, do not replace the flow —
            # the original browser is completing the authorization.
            if has_code and not is_expired and saved_fp == current_fp:
                raise DeploymentError(
                    "A sign-in is already in progress with a returned code; "
                    "wait for it to complete or expire before retrying."
                )

            client, _, callback = oauth
            state = secrets.token_urlsafe(32)
            self.state["oauth"] = {
                "state": digest(state),
                "browser": digest(browser_nonce),
                "expires": time.time() + 600,
                # Hashed fingerprint matching config_key() for upgrade compatibility
                # and secret safety in backups.
                "client_key": current_fp,
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

    def _probe_callback(self, callback, host, public_addrs):
        """Verify every address within one HTTP budget; failures are inconclusive."""
        deadline = time.monotonic() + 8.0
        verified = bool(public_addrs)
        for addr in public_addrs:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False, _CALLBACK_UNVERIFIED
            session = requests.Session()
            response = None
            session.mount("https://", _PinnedAdapter(host, addr))
            session.trust_env = False
            try:
                response = session.get(
                    callback,
                    timeout=(min(3, remaining / 2), min(5, remaining / 2)),
                    allow_redirects=False,
                    stream=True,
                )
                content = _read_health_content(response, deadline=deadline)
                body = json.loads(content) if content else {}
                reached = (
                    response.status_code == 400
                    and isinstance(body, dict)
                    and body.get("probe") == _CALLBACK_PROBE_MARKER
                )
                verified = verified and reached
            except (requests.RequestException, ValueError):
                verified = False
            finally:
                if response is not None:
                    close = getattr(response, "close", None)
                    if close is not None:
                        close()
                session.close()
        return (True, "") if verified else (False, _CALLBACK_UNVERIFIED)

    def ensure_callback_reachable(self, callback):
        """Refuse Amazon sign-in while the public callback cannot arrive.

        Amazon redirects the browser to the registered callback after consent;
        when the public host does not route /ma-alexa-skill/ back to this
        add-on the authorization code is stranded there, the account never
        connects, and setup only ever reports null vendor/skill identifiers
        (add-on issue #92). Nabu Casa can never route it, so that shape is
        refused outright.

        The probe is bounded: DNS resolution uses the shared worker with a
        5 s deadline (issue #49), and the HTTP response is read at most 4 KiB
        with a total deadline so a slow or trickling server cannot hold the
        setup request (issue #53). The request is pinned to the public
        addresses validated by the lookup to prevent SSRF and DNS rebinding
        (Sourcery review).

        Returns (verified, warning): verified is True only when the add-on
        answered its own callback with the dedicated probe marker, and the
        hostname resolves fully publicly from here. Otherwise the sign-in
        still starts and the caller surfaces the inconclusive result.
        """
        host = (urlparse(callback).hostname or "").lower().rstrip(".")
        if host.endswith(_NABU_CASA_SUFFIX):
            raise DeploymentError(
                "Home Assistant Cloud (Nabu Casa) cannot host the Amazon callback: "
                "its URL serves Home Assistant and ingress pages only, never "
                "/ma-alexa-skill/. Put a reverse proxy (Nginx Proxy Manager, or a "
                "Cloudflare Tunnel in front of one) on a public HTTPS hostname as "
                "described in the add-on docs, use that hostname as the skill "
                "endpoint, and register its callback URL with Login with Amazon "
                "before connecting again."
            )

        # Bounded local-view lookup before any outbound request (SSRF prevention).
        addrs = _resolved_addresses(host)
        if addrs is _DNS_TIMED_OUT or addrs is _DNS_UNAVAILABLE:
            return (
                False,
                _CALLBACK_UNVERIFIED,
            )  # stalled or unavailable lookup is inconclusive
        public_addrs = [a for a in addrs or [] if a.is_global]
        private_addrs = [a for a in addrs or [] if not a.is_global]
        if not public_addrs:
            return (
                False,
                _CALLBACK_UNVERIFIED,
            )  # private or unresolved view: not Amazon's path
        # Mixed public + private DNS: inconclusive since browser/Amazon may
        # resolve differently (split-horizon DNS). Do not block; warn.
        if private_addrs:
            return (
                False,
                _CALLBACK_UNVERIFIED,
            )  # mixed view is not Amazon's path

        # Probe all public addresses; pin to each to prevent SSRF/rebinding.
        return self._probe_callback(callback, host, public_addrs)

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
            oauth = oauth_config()
            fingerprint = config_key(oauth)
            pending = self.state.get("oauth", {})
            if (
                pending.get("expires", 0) < time.time()
                or not pending.get("code")
                or not secrets.compare_digest(
                    pending.get("browser", ""), digest(browser_nonce)
                )
                or pending.get("client_key") != fingerprint
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
                    "redirect_uri": oauth[2],
                },
                oauth=oauth,
            )
            self.state.update(
                client_key=fingerprint,
                message="Amazon connected. Select the developer account and personal skill.",
            )
            self.save()

    def callback(self, state, browser_nonce, code):
        self.accept_callback(state, code)
        self.complete_authorization(browser_nonce)

    def _store_tokens(self, body):
        """Persist refreshed credentials, reusing the stored refresh token if absent."""
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

    def _token(self, payload, oauth=None):
        client, secret, _ = oauth if oauth is not None else oauth_config()
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
            self._store_tokens(response.json())
        except (requests.RequestException, ValueError) as exc:
            raise DeploymentError(
                "Amazon sign-in request failed. Reconnect and try again."
            ) from exc

    def _refresh_access_token(self):
        """Return a bearer token, refreshing it when it is close to expiring."""
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
            return self.state["tokens"]["access_token"]

    def _renew_token_after_rejection(self):
        """Amazon rejected a token explicitly; renew it for the next attempt."""
        with self.lock:
            self._token(
                {
                    "grant_type": "refresh_token",
                    "refresh_token": self.state["tokens"]["refresh_token"],
                }
            )

    def api(self, method, path, **kwargs):
        if self.cancel.is_set():
            raise DeploymentError("Deployment interrupted by shutdown.")
        if not path.startswith("/v1/"):
            raise DeploymentError("Unexpected Amazon API path.")
        for attempt in range(3):
            response = self._send_once(method, path, attempt, kwargs)
            if response is None:
                continue  # transient network failure; already paused for retry
            if self._handle_status(method, response, attempt):
                continue
            return response
        raise DeploymentError("Amazon is busy. Try again later.")

    def _send_once(self, method, path, attempt, kwargs):
        """One Amazon HTTP call; None on a retryable network failure."""
        token = self._refresh_access_token()
        try:
            return self.http.request(
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
                return None
            raise DeploymentError(
                "Amazon request timed out or could not connect. Check progress before retrying."
            ) from exc

    def _handle_status(self, method, response, attempt):
        """True to retry (401 refresh / GET backoff), False on success (else raise)."""
        if response.status_code == 401 and attempt == 0:
            self._renew_token_after_rejection()
            return True  # explicit rejection: no operation was accepted
        if method == "GET" and response.status_code in (429, 500, 503) and attempt < 2:
            self.pause(2**attempt)
            return True
        if not 200 <= response.status_code < 300:
            raise AmazonError(response.status_code)
        return False

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
        # The invocation name currently LIVE in Amazon (an export only exists
        # for an already-created skill); verification status page compares it
        # against the start command Music Assistant sends (issue #71).
        live = None
        if create:
            if (
                skill
                or self.state.get("skill_id")
                or self.state.get("creation_unknown")
            ):
                raise DeploymentError(
                    "A skill is already saved or creation is uncertain. Select the existing skill by ID; automatic creation is blocked to prevent duplicates."
                )
            files = {
                f"skill-package/{SKILL_JSON_NAME}": (
                    APP_DIR / SKILL_JSON_NAME
                ).read_bytes()
            }
            manifest_path, baseline = "skill-package/skill.json", None
        else:
            if not any(item["id"] == skill for item in available):
                raise DeploymentError(
                    "Select a development custom skill from this developer account."
                )
            files, manifest_path = self.export(skill)
            baseline = package_fingerprint(files)
            live = _package_invocation(files, config["locale"])
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
            package_mismatch=None,
            live_invocation=live,
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
        skill = self._resolve_deploy_skill(review)
        self.update(
            skill_id=skill,
            vendor_id=review["vendor"],
            creation_unknown=False,
            deployed_settings=review["settings"],
            # Unknown until Amazon's export confirms it: the previous value
            # must not outlive the window before _verify_deployed_package().
            live_invocation=None,
            import_path=None,
            verified_at=None,
            package_mismatch=None,
            expected_package=package_fingerprint(
                unpack_package(self.package_path.read_bytes())[0]
            ),
        )
        if review["create"]:
            self.wait_manifest(skill)
        self._submit_import(skill)
        self.finish()

    def _resolve_deploy_skill(self, review):
        """Verify the reviewed skill is unchanged, or create it and return its ID."""
        skill = review["skill"]
        if skill:
            fresh, _ = self.export(skill)
            if self.state["baseline"] not in package_fingerprints(fresh):
                raise DeploymentError(
                    "The skill changed in Amazon since the review. Prepare a new review to preserve those changes."
                )
            return skill
        return self._create_skill(review)

    def _create_skill(self, review):
        """Create the skill in Amazon and return its validated ID."""
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
        return skill

    def _submit_import(self, skill):
        """Upload the approved package and start its import, blocking replay on failure."""
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

    def _validate_reconcile(self, payload):
        """Validate a reconcile request; return (result, reference)."""
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
        return result, reference

    def _reconcile_snapshot_still_valid(self, snapshot):
        """Reject a reconcile whose pending import or Amazon connection moved on."""
        if (
            self.state.get("phase") in BUSY
            or (self.thread and self.thread.is_alive())
            or self.cancel.is_set()
            or not self.state.get("import_unknown")
            or self.state.get("import_path")
            or self.state.get("import_attempt_id") != snapshot["attempt_id"]
            or self.state.get("skill_id") != snapshot["skill"]
            or self.state.get("vendor_id") != snapshot["vendor"]
            or self.state.get("client_key") != snapshot["client_key"]
            or self.state.get("client_key") != config_key()
            or (self.state.get("tokens") or {}).get("refresh_token")
            != snapshot["refresh_token"]
        ):
            raise DeploymentError(
                "The pending import or Amazon connection changed. Reload and verify the displayed skill."
            )

    def reconcile_import(self, payload):
        """Record an owner's Amazon-confirmed terminal result before releasing a block."""
        with self.lock:
            if self.state.get("phase") in BUSY or (
                self.thread and self.thread.is_alive()
            ):
                raise DeploymentError(
                    "Wait for the current operation before reconciling."
                )
            result, reference = self._validate_reconcile(payload)
            vendor, skill = self.state.get("vendor_id"), self.state.get("skill_id")
            snapshot = {
                "attempt_id": self.state.get("import_attempt_id"),
                "skill": skill,
                "vendor": vendor,
                "client_key": self.state.get("client_key"),
                "refresh_token": (self.state.get("tokens") or {}).get("refresh_token"),
            }

        # Ownership listing can involve multiple slow API pages. Keep status and
        # local settings responsive while it runs; validate the snapshot again below.
        if not vendor or not any(item["id"] == skill for item in self.skills(vendor)):
            raise DeploymentError(
                "Reconnect the original developer account and verify ownership of the saved skill."
            )

        with self.lock:
            self._reconcile_snapshot_still_valid(snapshot)
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

    def _await_build(self, path, locale):
        """Wait for Amazon to finish the manifest and voice-model build."""
        deadline = time.monotonic() + 600
        while True:
            status = self.api(
                "GET",
                path + "/status",
                params=[("resource", "manifest"), ("resource", "interactionModel")],
            ).json()
            statuses = [
                status.get("manifest", {}).get("lastUpdateRequest", {}).get("status"),
                status.get("interactionModel", {})
                .get(locale, {})
                .get("lastUpdateRequest", {})
                .get("status"),
            ]
            if "FAILED" in statuses:
                raise DeploymentError(
                    "Amazon has not confirmed a successful build: manifest or voice model failed. Check the selected locale’s build diagnostics before retrying."
                )
            if any(
                value not in (None, "SUCCEEDED", "IN_PROGRESS") for value in statuses
            ):
                raise DeploymentError(
                    "Amazon returned an unexpected manifest or voice-model status. Resume later."
                )
            if "IN_PROGRESS" not in statuses:
                return
            if time.monotonic() >= deadline:
                raise DeploymentError(
                    "Amazon has not confirmed a successful manifest and voice-model build after ten minutes. Resume later to verify the accepted import."
                )
            self.update(
                message="Amazon accepted the import; waiting for the selected locale’s build to finish…"
            )
            self.pause(3)

    def _verify_deployed_manifest(self, path, config):
        """Confirm Amazon serves the approved endpoint and interfaces."""
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
        return custom

    def _verify_deployed_package(self, skill):
        """Confirm Amazon's export matches the approved package, adopting re-encoded icons."""
        actual_files, _ = self.export(skill)
        deployed = self.state.get("deployed_settings") or {}
        live = _package_invocation(actual_files, deployed.get("locale") or "")
        if live is not None and live != self.state.get("live_invocation"):
            # What Amazon just returned is the authoritative live invocation.
            self.update(live_invocation=live)
        if package_fingerprint(actual_files) == self.state.get("expected_package"):
            return
        approved_files = unpack_package(self.package_path.read_bytes())[0]
        stored = store_icon_assets(approved_files, actual_files)
        if stored:
            # Store Amazon's re-encoded icon artwork so this one-time
            # materialization never fails another verification cycle.
            atomic_write(self.package_path, pack(approved_files))
            self.update(
                expected_package=package_fingerprint(approved_files),
                assets_stored=sorted(
                    set(self.state.get("assets_stored") or []) | set(stored)
                ),
            )
        report = package_mismatch(approved_files, actual_files)
        if not report:
            return
        summary = "; ".join(
            f"{name} ({', '.join(differences[:3])})"
            for name, differences in list(report.items())[:3]
        )
        self.update(package_mismatch=report)
        raise DeploymentError(
            "Amazon’s exported package differs from the approved deployment: "
            f"{summary}. Review again before enabling testing."
        )

    def _push_self_signed_certificate(self, path, config, regions):
        """Upload the approved self-signed certificate and confirm Amazon stored it."""
        pem = config["certificate_pem"]
        certificates = {"sslCertificate": pem}
        if regions:
            certificates["regions"] = {
                region: {"sslCertificate": pem} for region in regions
            }
        self.api("PUT", path + "/sslCertificateSets/~latest", json=certificates)
        actual = self.api("GET", path + "/sslCertificateSets/~latest").json()
        if actual.get("sslCertificate", "").strip() != pem or any(
            actual.get("regions", {}).get(region, {}).get("sslCertificate", "").strip()
            != pem
            for region in regions
        ):
            raise DeploymentError(
                "Amazon did not confirm the uploaded certificate. Resume to retry before enabling testing."
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
        # Package imports are authoritative; the legacy skill-status API can omit
        # resources. Wait for any explicitly running build and reject failed ones,
        # then verify the exported approved package below, including its model.
        self._await_build(path, config["locale"])
        custom = self._verify_deployed_manifest(path, config)
        self._verify_deployed_package(skill)
        if config["certificate"] == "SelfSigned":
            self._push_self_signed_certificate(path, config, custom.get("regions", {}))
        self.api("PUT", path + "/stages/development/enablement")
        enabled = self.api("GET", path + "/stages/development/enablement")
        if enabled.status_code != 204:
            raise DeploymentError("Amazon did not confirm development enablement.")
        from private_access import save_allowed_skill_id

        try:
            save_allowed_skill_id(skill, self.path)
        except OSError as exc:
            raise DeploymentError(
                "Could not save the skill restriction. Check data-directory permissions and resume deployment."
            ) from exc
        self.update(
            phase="complete",
            message="Amazon confirmed the import, approved package, endpoint and development enablement. Test playback on your Echo.",
            verified_at=time.time(),
            import_path=None,
            import_unknown=False,
            package_mismatch=None,
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


def _setup_protect():
    """Require app credentials on setup routes (OAuth callback stays open)."""
    if (
        request.path
        not in ("/setup/oauth/callback", "/ma-alexa-skill/setup/oauth/callback")
        and not request.environ.get("ma.trusted_ingress")
        and (not get_env_secret("APP_USERNAME") or not get_env_secret("APP_PASSWORD"))
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


def _setup_private(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = (
        "SAMEORIGIN" if request.environ.get("ma.trusted_ingress") else "DENY"
    )
    return response


def _setup_deployment_error(exc):
    msg = str(exc)
    body = {"error": msg}
    if "Sign-in expired or was already returned" in msg:
        body["probe"] = _CALLBACK_PROBE_MARKER
    return jsonify(body), 400


def _setup_settings_error(exc):
    return jsonify(error=str(exc)), 400


def _setup_status():
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
        path=request.script_root + SETUP_PATH,
    )
    return response


def _save_settings():
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


def _start_preview():
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


def _start_deploy():
    payload = request.get_json() or {}
    if not isinstance(payload, dict) or not isinstance(payload.get("review_id"), str):
        raise DeploymentError("Prepare a review before deploying.")
    manager().start_job("running", manager().deploy, payload["review_id"])
    return jsonify(status="running"), 202


def _start_resume():
    if not manager().state.get("import_path"):
        raise DeploymentError("No accepted import is available to resume.")
    manager().start_job("running", manager().finish)
    return jsonify(status="running"), 202


def _wizard_done():
    """Report which wizard steps already have their prerequisites saved."""
    status = manager().public_status()
    values = store().public()["values"]
    return {
        1: bool(str(values.get("ma_hostname", "")).strip()),
        2: bool(status.get("connected")),
        3: bool(status.get("skill_id") or status.get("review")),
        4: status.get("phase") == "complete",
    }


def _first_incomplete_step(done):
    """Return the first step whose prerequisites are missing, else the last."""
    return next((number for number in range(1, 5) if not done[number]), 4)


SETUP_PAGES = {
    1: "setup_settings.html",
    2: "setup_amazon.html",
    3: "setup_skill.html",
    4: "setup_review.html",
}


def register_setup(app):
    blueprint = Blueprint("personal_setup", __name__)
    blueprint.before_request(_setup_protect)
    blueprint.after_request(_setup_private)
    blueprint.errorhandler(DeploymentError)(_setup_deployment_error)
    blueprint.errorhandler(SettingsError)(_setup_settings_error)

    @blueprint.route("/setup", methods=["GET"])
    def page():
        return redirect(url_for(".step", number=_first_incomplete_step(_wizard_done())))

    @blueprint.route("/setup/step/<int:number>", methods=["GET"])
    def step(number):
        if number not in SETUP_PAGES:
            abort(404)
        page = render_template(SETUP_PAGES[number], step=number, done=_wizard_done())
        return page.replace("__NAV__", nav_html("setup"))

    @blueprint.route("/setup/status", methods=["GET"])
    def status():
        return _setup_status()

    @blueprint.route("/setup/settings", methods=["GET", "POST"])
    def application_settings():
        return _save_settings()

    @blueprint.route("/setup/settings/api-password", methods=["POST"])
    def reveal_api_password():
        return jsonify(password=get_env_secret("APP_PASSWORD"))

    @blueprint.route("/setup/oauth/start", methods=["POST"])
    def connect():
        oauth = (
            oauth_config()
        )  # one snapshot: validate, probe and authorize the same callback
        verified, warning = manager().ensure_callback_reachable(oauth[2])
        nonce = secrets.token_urlsafe(32)
        response = jsonify(
            url=manager().authorize(nonce, oauth),
            callback_verified=verified,
            callback_warning=warning,
        )
        response.set_cookie(
            "setup_oauth",
            nonce,
            max_age=600,
            secure=request.is_secure,
            httponly=True,
            samesite="Lax",
            path=request.script_root + SETUP_PATH,
        )
        return response

    @blueprint.route("/ma-alexa-skill/setup/oauth/callback", methods=["GET"])
    @blueprint.route("/setup/oauth/callback", methods=["GET"])
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
        response.delete_cookie("setup_oauth", path=request.script_root + SETUP_PATH)
        return response

    @blueprint.route("/setup/accounts", methods=["GET"])
    def accounts():
        return jsonify(vendors=manager().accounts())

    @blueprint.route("/setup/skills", methods=["GET"])
    def skills():
        return jsonify(skills=manager().skills(request.args.get("vendor", "")))

    @blueprint.route("/setup/preview", methods=["POST"])
    def preview():
        return _start_preview()

    @blueprint.route("/setup/deploy", methods=["POST"])
    def deploy():
        return _start_deploy()

    @blueprint.route("/setup/reconcile-import", methods=["POST"])
    def reconcile():
        manager().reconcile_import(request.get_json(silent=True))
        return jsonify(status="reconciled")

    @blueprint.route("/setup/resume", methods=["POST"])
    def resume():
        return _start_resume()

    app.register_blueprint(blueprint)
