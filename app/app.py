import base64
import json
import logging
import os
import re
import time

import alexa_api as alexa_api
import music_assistant_api as ma_api
import swagger_ui as maa_swagger
from endpoints import devices_bp, invocations_bp, simulator_bp, status_bp
from env_secrets import get_env_secret
from flask import Flask, Response, g, jsonify, request
from flask_ask_sdk.skill_adapter import SkillAdapter
from setup_helpers import ask_home_from_credentials_dir
from skill.lambda_function import (
    sb,  # sb is the SkillBuilder from skill/lambda_function.py
)
from skill_deployment import register_setup
from werkzeug.middleware.dispatcher import DispatcherMiddleware
from werkzeug.middleware.proxy_fix import ProxyFix


def _load_addon_options_into_env():
    """Load Home Assistant add-on options from /data/options.json."""
    options_path = '/data/options.json'
    try:
        if not os.path.exists(options_path):
            return {}
        with open(options_path, 'r', encoding='utf-8') as f:
            options = json.load(f)
        if not isinstance(options, dict):
            return {}

        loaded = {}
        for key, value in options.items():
            if value is None:
                continue
            os.environ[str(key)] = str(value)
            loaded[str(key)] = str(value)
        return loaded
    except Exception:
        return {}


def _safe_options_for_log(options):
    return {key: ('set' if value else '') if any(part in key.lower()
            for part in ('password', 'token', 'secret', 'username')) else value
            for key, value in options.items()}


_loaded_addon_options = _load_addon_options_into_env()

# Ensure boto3 has a default region in container/dev environments to avoid
# NoRegionError during imports that create AWS clients at module import time.
os.environ.setdefault('AWS_REGION', os.environ.get('AWS_DEFAULT_REGION', 'us-east-1'))
os.environ.setdefault('AWS_DEFAULT_REGION', os.environ.get('AWS_DEFAULT_REGION', 'us-east-1'))

class _CallbackLogFilter(logging.Filter):
    """Keep OAuth query strings out of Werkzeug logs even with QUIET_HTTP=0."""
    def filter(self, record):
        if isinstance(record.args, tuple):
            record.args = tuple(re.sub(r'(/setup/oauth/callback)\?[^\s]*', r'\1', item)
                                if isinstance(item, str) else item for item in record.args)
        return True


logging.getLogger('werkzeug').addFilter(_CallbackLogFilter())

app = Flask(__name__)
if _loaded_addon_options:
    app.logger.info('Loaded add-on options from /data/options.json: %s', _safe_options_for_log(_loaded_addon_options))
# Optionally silence HTTP request logs (werkzeug/urllib3) when running
# in container or debugger. Set QUIET_HTTP=0 to keep request logging.
try:
    quiet_http = os.environ.get('QUIET_HTTP', '1').lower()
    if quiet_http in ('1', 'true', 'yes', 'on'):
        logging.getLogger('werkzeug').setLevel(logging.WARNING)
        logging.getLogger('urllib3').setLevel(logging.WARNING)
        # Also reduce Flask's internal request logging
        logging.getLogger('flask.app').setLevel(logging.WARNING)
        app.logger.debug('QUIET_HTTP enabled: werkzeug/urllib3 log level set to WARNING')
except Exception:
    pass
# Allow overriding where ASK CLI stores credentials so they persist across containers.
# If ASK_CREDENTIALS_DIR is set (e.g. /root/.ask), set HOME to its parent so
# tools that rely on ~/.ask (ASK CLI) use the mounted location.
try:
    ask_home = ask_home_from_credentials_dir()
    if ask_home:
        os.environ['HOME'] = ask_home
        app.logger.info('Using ASK credentials under HOME=%s', ask_home)
except Exception:
    pass
skill_adapter = SkillAdapter(
    skill=sb.create(),
    skill_id="", # pyright: ignore[reportArgumentType]
    app=app)

# Mount the Music Assistant API (only ma routes will be mounted at /ma)
ma_app = ma_api.create_ma_app()
# Alexa-specific API (mounted at /alexa)
alexa_app = alexa_api.create_alexa_app()


class BasicAuthMiddleware:
    """WSGI middleware that enforces HTTP Basic auth using APP_USERNAME/APP_PASSWORD.

    Applied to the `ma_app` WSGI app so requests to `/ma` require the same
    APP_USERNAME/APP_PASSWORD credentials as the rest of the app. If no
    APP_USERNAME/APP_PASSWORD are configured, auth is not enforced.
    """
    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        user = get_env_secret('APP_USERNAME')
        pwd = get_env_secret('APP_PASSWORD')
        if not user and not pwd:
            return self.app(environ, start_response)

        auth = environ.get('HTTP_AUTHORIZATION')
        if auth and auth.startswith('Basic '):
            try:
                token = auth.split(' ', 1)[1].strip()
                decoded = base64.b64decode(token).decode('utf-8')
                u, sep, p = decoded.partition(':')
                if sep and u == user and p == pwd:
                    return self.app(environ, start_response)
            except Exception:
                pass

        start_response('401 Unauthorized', [('Content-Type', 'text/plain'), ('WWW-Authenticate', 'Basic realm="music-assistant-skill"')])
        return [b'Access denied']


@app.before_request
def _inject_simulator_signature_headers():
    """If the incoming request is from the simulator and lacks Alexa
    signature headers, allow simulator-provided fallbacks to be injected so
    the ask-sdk verifier sees them. This is intended for local development
    only.
    """
    try:
        if request.path == '/' and request.method == 'POST':
            # If real Signature headers are missing, accept simulator fallbacks
            env = request.environ
            if not request.headers.get('Signature'):
                sim_sig = request.headers.get('X-Simulator-Signature') or request.args.get('sim_signature')
                if sim_sig:
                    env['HTTP_SIGNATURE'] = sim_sig
            if not request.headers.get('SignatureCertChainUrl'):
                sim_cert = request.headers.get('X-Simulator-CertUrl') or request.args.get('sim_cert')
                if sim_cert:
                    env['HTTP_SIGNATURECERTCHAINURL'] = sim_cert
    except Exception:
        pass

# Respect X-Forwarded-* headers when running behind a reverse proxy so
# `request.host_url` and `request.scheme` reflect the external client URL.
# Apply ProxyFix to both apps before wiring the dispatcher.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
ma_app.wsgi_app = ProxyFix(ma_app.wsgi_app, x_for=1, x_proto=1, x_host=1)
alexa_app.wsgi_app = ProxyFix(alexa_app.wsgi_app, x_for=1, x_proto=1, x_host=1)
ma_app.wsgi_app = BasicAuthMiddleware(ma_app.wsgi_app)
alexa_app.wsgi_app = BasicAuthMiddleware(alexa_app.wsgi_app)
app.wsgi_app = DispatcherMiddleware(app.wsgi_app, {'/ma': ma_app.wsgi_app, '/alexa': alexa_app.wsgi_app})
# Log mount information only when running the module as the main program
try:
    if __name__ == '__main__':
        app.logger.info('Mounted MA API app at /ma and Alexa API at /alexa')
except Exception:
    # Fallback: do not allow logging failures to crash import
    pass

# Global basic auth for the app (protect everything except the root Alexa skill endpoint)
@app.before_request
def _check_app_basic_auth():
    # Allow the Alexa skill POST endpoint to be called without app-level auth
    if request.path == '/health' or (request.path == '/' and request.method == 'POST'):
        return None
    # Read credentials from secrets (APP_USERNAME/APP_PASSWORD)
    app_user = get_env_secret('APP_USERNAME')
    app_pass = get_env_secret('APP_PASSWORD')
    # If no app credentials configured, do not enforce auth
    if not app_user and not app_pass:
        return None
    auth = request.authorization
    if not auth or auth.username != app_user or auth.password != app_pass:
        resp = Response('Access denied', 401)
        resp.headers['WWW-Authenticate'] = 'Basic realm="music-assistant-skill"'
        return resp


# Capture incoming Alexa POST payloads so we can show them on the status page
@app.before_request
def _capture_incoming_intent():
    if request.path == '/' and request.method == 'POST':
        payload = None
        try:
            payload = request.get_json(silent=True)
        except Exception:
            payload = None
        if not payload:
            try:
                raw = request.get_data(as_text=True)
                if raw:
                    import json as _json
                    try:
                        payload = _json.loads(raw)
                    except Exception:
                        payload = None
            except Exception:
                payload = None
        if not payload:
            try:
                if request.form:
                    intent = request.form.get('intent')
                    raw_slots = request.form.get('slots')
                    if intent:
                        payload = {"version": "1.0", "request": {"type": "IntentRequest", "intent": {"name": intent}}}
                        if raw_slots:
                            try:
                                payload['request']['intent']['slots'] = _json.loads(raw_slots)
                            except Exception:
                                pass
            except Exception:
                pass

        g._incoming_alexa_payload = payload or {}
        g._incoming_alexa_ts = time.time()


@app.after_request
def _record_incoming_intent(response):
    if getattr(g, '_incoming_alexa_payload', None) is not None:
        try:
            logs = app.config.setdefault('INTENT_LOGS', [])
            entry = {
                'incoming': g._incoming_alexa_payload,
                'response_status': response.status_code,
                'response_body': response.get_data(as_text=True),
                'ts': getattr(g, '_incoming_alexa_ts', None)
            }
            logs.append(entry)
            maxlen = app.config.get('INTENT_LOGS_MAXLEN', 500)
            if len(logs) > maxlen:
                del logs[0:len(logs)-maxlen]
        except Exception:
            pass
    return response

# Deployment jobs share this single-worker application with playback requests.
register_setup(app)
app.config['INTENT_LOGS'] = []
app.config['INTENT_LOGS_MAXLEN'] = 500

for blueprint in (status_bp, invocations_bp, simulator_bp, devices_bp):
    app.register_blueprint(blueprint)


def shutdown_setup_children(signum):
    """Called by Gunicorn before the worker exits."""
    manager = app.extensions.get('skill_deployment')
    if manager:
        manager.stop()


@app.route("/", methods=["POST"])
def invoke_skill():
    # Allow simulator-originated requests to bypass signature/timestamp
    # verification for local testing when the simulator provides a
    # simulator-specific header. This creates a temporary handler with
    # verification disabled and dispatches the request through it. For
    # normal requests we keep the existing behavior.
    try:
        if request.headers.get('X-Simulator-Bypass') or request.headers.get('X-Simulator-Signature'):
            # Simulator bypass is available only to authenticated API users.
            user = get_env_secret('APP_USERNAME')
            password = get_env_secret('APP_PASSWORD')
            auth = request.authorization
            if not user or not password or not auth or auth.username != user or auth.password != password:
                return Response('Simulator authentication required', 403)
            try:
                from ask_sdk_webservice_support import verifier_constants
                from ask_sdk_webservice_support.webservice_handler import (
                    WebserviceSkillHandler,
                )
                content = request.data.decode(verifier_constants.CHARACTER_ENCODING)
                handler = WebserviceSkillHandler(skill_adapter._skill, verify_signature=False, verify_timestamp=False, verifiers=[])
                response = handler.verify_request_and_dispatch(http_request_headers=request.headers, http_request_body=content)
                return jsonify(response)
            except Exception:
                app.logger.exception('Simulator dispatch without verification failed')
                # fallthrough to normal dispatch
                pass
    except Exception:
        pass
    return skill_adapter.dispatch_request()

# Expose OpenAPI spec and Swagger UI from the main app so docs are available
# at `/openapi.json` and `/docs` (keeps documentation separate from the API
# implementation which is mounted at `/ma`).
@app.route('/openapi.json', methods=['GET'])
def openapi_json():
    return maa_swagger.openapi_spec()


@app.route('/docs', methods=['GET'])
def docs():
    return maa_swagger.render()


@app.before_request
def _request_playback_snapshot():
    from skill import data
    data.begin_request()


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})


if __name__ == "__main__":
    port = int(os.environ.get('PORT', '5000'))
    # Respect FLASK_DEBUG (1 enables debug mode) and FLASK_RELOADER (1 enables reloader)
    flask_debug = os.environ.get('FLASK_DEBUG', '0') == '1'
    if flask_debug:
        use_reloader = os.environ.get('FLASK_RELOADER', '0') == '1'
        app.run(debug=True, use_reloader=use_reloader, host="0.0.0.0", port=port)
    else:
        # Production/dev host mode: don't use the reloader to avoid transient restarts
        app.run(debug=False, host="0.0.0.0", port=port)
