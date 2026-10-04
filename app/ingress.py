"""Trust Home Assistant's ingress gateway only on the private listener."""

import os
import re

from werkzeug.wrappers import Response


def enabled():
    return os.environ.get("HA_INGRESS_ENABLED", "").lower() == "true"


def ui_path(path):
    return any(
        path == item or path.startswith(item + "/")
        for item in (
            "/status",
            "/setup",
            "/devices",
            "/invocations",
            "/simulator",
            "/docs",
            "/openapi.json",
        )
    )


class IngressMiddleware:
    def __init__(self, application):
        self.application = application

    def __call__(self, environ, start_response):
        # Inspect socket metadata BEFORE ProxyFix processes forwarded headers.
        if enabled() and environ.get("SERVER_PORT") == "8099":
            prefix = environ.get("HTTP_X_INGRESS_PATH", "")
            if environ.get("REMOTE_ADDR") != "172.30.32.2" or not re.fullmatch(
                r"/api/hassio_ingress/[A-Za-z0-9_-]+", prefix
            ):
                return Response("Ingress gateway required", 403)(
                    environ, start_response
                )
            if not (
                ui_path(environ.get("PATH_INFO", "")) or environ.get("PATH_INFO") == "/"
            ):
                return Response("Use the API listener for playback APIs", 403)(
                    environ, start_response
                )
            environ["ma.trusted_ingress"] = True
            environ["SCRIPT_NAME"] = prefix
        return self.application(environ, start_response)
