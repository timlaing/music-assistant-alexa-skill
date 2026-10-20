import json
import urllib.parse
from copy import deepcopy
from pathlib import Path

import requests
from app_settings import get_setting
from flask import Blueprint, Response, jsonify, render_template, request
from page_nav import nav_html

simulator_bp = Blueprint("simulator_bp", __name__)


# Internal sample payload used by the simulator UI. This avoids reading
# repository-local payload.json files and prevents leaking account data.
SAMPLE_PAYLOAD = {
    "version": "1.0",
    "session": {
        "new": True,
        "sessionId": "SessionId.simulator",
        "application": {"applicationId": ""},
        "user": {"userId": "amzn1.ask.account.simulator"},
    },
    "context": {
        "System": {
            "apiAccessToken": "",
            "application": {"applicationId": ""},
            "device": {"deviceId": "amzn1.ask.device.simulator"},
        },
        "Advertising": {"advertisingId": ""},
    },
    "request": {
        "type": "IntentRequest",
        "requestId": "EdwRequestId.simulator",
        "locale": "en-US",
        "timestamp": "2020-01-01T00:00:00Z",
        "intent": {"name": "", "slots": {}},
    },
}


@simulator_bp.route("/simulator", methods=["GET"])
def simulator_index():
    page = render_template("simulator.html")
    page = page.replace("__NAV__", nav_html("simulator"))
    # expose configured SKILL_HOSTNAME to the client for convenience
    page = page.replace(
        "__SKILL_HOSTNAME__", json.dumps(get_setting("SKILL_HOSTNAME", ""))
    )
    return Response(page, mimetype="text/html")


def _load_model_intents():
    # Prefer en-US model; fall back to first model file if missing
    try:
        model_path = Path(__file__).parent.parent / "models" / "en-US.json"
        if not model_path.exists():
            # try scanning models dir
            mdir = Path(__file__).parent.parent / "models"
            for p in mdir.glob("*.json"):
                model_path = p
                break
        data = json.loads(model_path.read_text())
        intents = (
            data.get("interactionModel", {}).get("languageModel", {}).get("intents", [])
        )
        intents_out = []
        for it in intents:
            intents_out.append(
                {"name": it.get("name"), "samples": it.get("samples", [])}
            )
        return intents_out
    except Exception:
        return []


@simulator_bp.route("/simulator/api", methods=["GET"])
def simulator_api():
    """Return available intents and server-side config for the simulator UI."""
    intents = _load_model_intents()
    return jsonify(
        {"intents": intents, "skill_hostname": get_setting("SKILL_HOSTNAME", "")}
    )


@simulator_bp.route("/simulator/payload", methods=["GET"])
def simulator_payload():
    """Return the default constructed Alexa IntentRequest payload for an intent."""
    intent = request.args.get("intent")
    if not intent:
        return jsonify({"error": "missing intent parameter"}), 400
    # Use the internal SAMPLE_PAYLOAD to avoid reading repo-local files
    payload = deepcopy(SAMPLE_PAYLOAD)
    # set the requested intent name and ensure slots exist
    payload["request"]["intent"]["name"] = intent
    payload["request"]["intent"].setdefault("slots", {})
    return jsonify(payload)


def _resolve_doh(hostname):
    """Resolve hostname using DNS-over-HTTPS (Cloudflare) to bypass local /etc/hosts."""
    try:
        # Use Cloudflare DoH JSON endpoint
        resp = requests.get(
            "https://cloudflare-dns.com/dns-query",
            params={"name": hostname, "type": "A"},
            headers={"Accept": "application/dns-json"},
            timeout=5,
        )
        if resp.ok:
            j = resp.json()
            answers = j.get("Answer") or []
            for a in answers:
                # Answer entries may contain 'data' with IP or CNAMEs
                ip = a.get("data")
                if ip and isinstance(ip, str):
                    return ip
    except Exception:
        pass
    return None


def _build_payload(intent, override):
    """Return (payload, error_response); exactly one side is None."""
    if not override:
        return {
            "version": "1.0",
            "session": {
                "new": True,
                "sessionId": "SessionId.simulator",
                "application": {"applicationId": ""},
                "user": {"userId": "amzn1.ask.account.simulator"},
            },
            "context": {},
            "request": {
                "type": "IntentRequest",
                "requestId": "EdwRequestId.simulator",
                "locale": "en-US",
                "timestamp": "2020-01-01T00:00:00Z",
                "intent": {"name": intent, "slots": {}},
            },
        }, None
    try:
        payload = json.loads(override) if isinstance(override, str) else override
        return payload, None
    except Exception as e:
        return None, (jsonify({"error": f"invalid override_payload JSON: {e}"}), 400)


def _simulator_auth():
    """Basic-auth credentials for the local app, when configured."""
    try:
        from env_secrets import get_env_secret

        app_user = get_env_secret("APP_USERNAME")
        app_pass = get_env_secret("APP_PASSWORD")
        if app_user and app_pass:
            return (app_user, app_pass)
    except Exception:
        pass
    return None


def _resolve_target(use):
    """Return (target_url, headers, verify, error_response).

    Exactly one of the four values is meaningful: on error the first three
    are None and error_response is the response to return unchanged.
    """
    headers = {"Content-Type": "application/json"}
    if use == "hostname":
        raw = get_setting("SKILL_HOSTNAME", "").strip()
        if not raw:
            return (
                None,
                None,
                None,
                (
                    jsonify({"error": "SKILL_HOSTNAME not configured"}),
                    400,
                ),
            )
        if raw.startswith(("http://", "https://")):
            parsed = urllib.parse.urlparse(raw)
            scheme, host, port = parsed.scheme, parsed.hostname, parsed.port
        else:
            scheme, host, port = "https", raw, None
        ip = _resolve_doh(host)
        if not ip:
            return (
                None,
                None,
                None,
                (
                    jsonify({"error": "DNS resolution failed for host: " + host}),
                    500,
                ),
            )
        netloc = f"{ip}:{port}" if port else ip
        headers["Host"] = host
        # certificate verification is disabled because the resolved IP will
        # not match the certificate; the Host header keeps virtual hosts working
        return f"{scheme}://{netloc}/", headers, False, None
    # local: post to this server's root
    return request.host_url.rstrip("/") + "/", headers, True, None


def _post_json(target_url, payload, headers, auth, verify):
    """POST the payload and return the status/body response envelope."""
    resp = requests.post(
        target_url, json=payload, headers=headers, auth=auth, timeout=10, verify=verify
    )
    try:
        body = resp.content.decode("utf-8", errors="replace")
    except Exception:
        body = str(resp.content)
    return jsonify({"status_code": resp.status_code, "body": body})


def _retry_with_hostname(use, scheme, host, payload, headers, auth):
    """Best-effort SNI retry against the original hostname; None on failure."""
    if use != "hostname" or not host or not scheme:
        return None
    try:
        resp = requests.post(
            f"{scheme}://{host}/",
            json=payload,
            headers=headers,
            auth=auth,
            timeout=10,
            verify=True,
        )
        try:
            body = resp.content.decode("utf-8", errors="replace")
        except Exception:
            body = str(resp.content)
        return jsonify(
            {
                "status_code": resp.status_code,
                "body": body,
                "note": "retried using hostname for SNI",
            }
        )
    except Exception:
        return None


@simulator_bp.route("/simulator/send", methods=["POST"])
def simulator_send():
    """Send a constructed Alexa IntentRequest to the skill and return response.

    Request JSON fields:
    - intent: intent name (required)
    - use: 'local' or 'hostname' (default 'local')
    - sample: optional sample utterance (unused, kept for future)
    """
    data = request.get_json(silent=True) or {}
    intent = data.get("intent")
    use = data.get("use", "local")
    if not intent:
        return jsonify({"error": "missing intent"}), 400

    # build minimal Alexa intent request envelope (unless override provided)
    payload, error = _build_payload(intent, data.get("override_payload"))
    if error:
        return error

    auth = _simulator_auth()
    target_url, headers, verify, error = _resolve_target(use)
    if error:
        return error

    # Provide minimal Alexa signature headers so the ask-sdk verifier doesn't
    # immediately raise "Missing Signature/Certificate for the skill request".
    # These are dummy values intended for local testing only; the verifier may
    # still perform full signature validation depending on configuration.
    headers.setdefault("Signature", "dummy-signature")
    headers.setdefault(
        "SignatureCertChainUrl", "https://s3.amazonaws.com/echo.api/echo-api-cert.pem"
    )
    # Also send simulator-specific shadow headers which the app will copy into
    # the WSGI environ if the real headers are absent (helps when proxies or
    # local networking strip uncommon header names).
    headers.setdefault("X-Simulator-Signature", "dummy-signature")
    headers.setdefault(
        "X-Simulator-CertUrl", "https://s3.amazonaws.com/echo.api/echo-api-cert.pem"
    )

    try:
        return _post_json(target_url, payload, headers, auth, verify)
    except requests.exceptions.SSLError as e:
        # SSL handshake failed when connecting to resolved IP. Retry by
        # posting to the original hostname (this sets correct SNI). This
        # is a best-effort fallback for local testing.
        scheme = urllib.parse.urlparse(target_url).scheme
        retried = _retry_with_hostname(
            use, scheme, headers.get("Host"), payload, headers, auth
        )
        if retried is not None:
            return retried
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500
