"""Narrow, loopback-only companion service for the local operator UI.

It intentionally has no generic file endpoint.  The Firebase-hosted UI is an
identity surface; production browser access to this service stays disabled
until Private Network Access has been verified in the target browser.  Local
use can provide an independently verified subject via a deployment-specific
verifier, then this service applies a second explicit operator allowlist.
"""

from __future__ import annotations

import dataclasses
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

LOOPBACK_HOST = "127.0.0.1"
DEFAULT_ALLOWED_ORIGINS = frozenset({"https://angel-508305.web.app", "http://127.0.0.1"})


class AuthorizationError(PermissionError):
    pass


@dataclasses.dataclass(frozen=True)
class ServicePolicy:
    allowed_origins: frozenset[str]
    operator_subjects: frozenset[str]

    def __post_init__(self) -> None:
        if not self.allowed_origins or "*" in self.allowed_origins:
            raise ValueError("CORS needs one or more exact origins; wildcard is forbidden")
        if not self.operator_subjects:
            raise ValueError("an explicit operator allowlist is required")


class OperatorAuthorizer:
    def __init__(self, *, policy: ServicePolicy, verify_bearer: Callable[[str], str]):
        self.policy = policy
        self.verify_bearer = verify_bearer

    def authorize(self, authorization: str | None) -> str:
        if not authorization or not authorization.startswith("Bearer "):
            raise AuthorizationError("missing bearer token")
        try:
            subject = self.verify_bearer(authorization.removeprefix("Bearer "))
        except Exception as exc:  # verifier details must not become an HTTP oracle
            raise AuthorizationError("invalid bearer token") from exc
        if subject not in self.policy.operator_subjects:
            raise AuthorizationError("authenticated subject is not an Angel operator")
        return subject


class CompanionService(ThreadingHTTPServer):
    """Server constructor refuses non-loopback binding before it listens."""

    def __init__(self, host: str, port: int, *, authorizer: OperatorAuthorizer, overview: Callable[[], dict]):
        if host != LOOPBACK_HOST:
            raise ValueError("Angel companion service may bind only to 127.0.0.1")
        self.authorizer = authorizer
        self.overview = overview
        super().__init__((host, port), _Handler)


class _Handler(BaseHTTPRequestHandler):
    server: CompanionService

    def log_message(self, _format: str, *_args: object) -> None:  # no token/header logging
        return

    def _origin_allowed(self) -> bool:
        return self.headers.get("Origin") in self.server.authorizer.policy.allowed_origins

    def _cors(self) -> None:
        origin = self.headers.get("Origin")
        if origin in self.server.authorizer.policy.allowed_origins:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")

    def do_OPTIONS(self) -> None:  # noqa: N802
        if not self._origin_allowed():
            self.send_error(HTTPStatus.FORBIDDEN, "unapproved origin")
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/v1/overview":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not self._origin_allowed():
            self.send_error(HTTPStatus.FORBIDDEN, "unapproved origin")
            return
        try:
            self.server.authorizer.authorize(self.headers.get("Authorization"))
        except AuthorizationError:
            self.send_error(HTTPStatus.UNAUTHORIZED, "operator authorization required")
            return
        encoded = json.dumps(self.server.overview(), sort_keys=True).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)
