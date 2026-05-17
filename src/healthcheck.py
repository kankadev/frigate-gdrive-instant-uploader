"""
Lightweight HTTP healthcheck endpoint.

Exposes two endpoints:

  GET /health
      Liveness probe. Returns 200 OK if the core subsystems (DB + scheduler)
      are healthy, 503 otherwise. MQTT disconnects are intentionally NOT
      treated as unhealthy: the periodic scheduler job catches missed events,
      so a transient MQTT hiccup must not trigger a container restart.

      No authentication. Designed to be called by Docker's HEALTHCHECK
      directive from inside the container. Response body is minimal JSON
      and contains no sensitive data.

  GET /status
      Detailed JSON status with aggregate counts (uploaded last 24h,
      pending total, error-kind breakdown, subsystem flags). Optionally
      protected by a bearer token if HEALTHCHECK_TOKEN is set in the env.

      Deliberately leaks NO sensitive information: no service-account paths,
      no Frigate URL, no Mattermost webhook, no MQTT credentials, no
      individual event IDs (which could expose surveillance timestamps).

Implementation uses only the Python stdlib (`http.server.ThreadingHTTPServer`)
to avoid adding a new dependency for what is essentially a 2-endpoint API.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional

from src import database


@dataclass
class HealthState:
    """
    Container for runtime references the health endpoints need to inspect.

    All fields are optional so the healthcheck server can be started before
    every subsystem is fully initialised; missing references degrade
    gracefully rather than crashing the endpoint.
    """
    db_path: str
    scheduler: Any = None
    # Callable returning True/False so we don't pin a specific MQTT client type
    # and so that we can re-check at request time (the underlying client
    # connection state changes over the process lifetime).
    mqtt_is_connected: Optional[Callable[[], bool]] = None
    # Optional bearer token guarding /status. Empty/None disables auth.
    status_token: Optional[str] = None
    # When True, /health returns 503 instead of 200 (e.g. during shutdown).
    shutting_down: threading.Event = field(default_factory=threading.Event)


def _check_db(db_path: str, timeout: float = 1.0) -> tuple[bool, Optional[str]]:
    """Run a trivial query against the SQLite DB. Returns (ok, error_message)."""
    try:
        conn = sqlite3.connect(db_path, timeout=timeout)
        try:
            conn.execute("SELECT 1").fetchone()
            return True, None
        finally:
            conn.close()
    except Exception as e:
        # Don't leak the actual exception message to the client (could contain
        # filesystem paths). Log it server-side, return a generic flag.
        logging.warning(f"Healthcheck DB probe failed: {e}")
        return False, "db_unreachable"


def _check_scheduler(scheduler) -> bool:
    if scheduler is None:
        return False
    try:
        return bool(scheduler.running)
    except Exception:
        return False


def _check_mqtt(probe: Optional[Callable[[], bool]]) -> bool:
    if probe is None:
        return False
    try:
        return bool(probe())
    except Exception:
        return False


class _SilentHandler(BaseHTTPRequestHandler):
    """
    HTTP handler with all access logs suppressed. The Docker HEALTHCHECK
    hits us every 30 s by default — without this override the application
    log would be flooded with `127.0.0.1 - - [..] "GET /health HTTP/1.1" 200 -`
    lines, drowning out genuine signal.
    """

    # Class-level reference, populated by `start_healthcheck_server`.
    state: HealthState = None  # type: ignore[assignment]

    # ------------------------------------------------------------------ logging
    def log_message(self, format, *args):
        # Swallow access logs entirely. Errors still go through log_error.
        return

    def log_error(self, format, *args):
        # Route handler-level errors to our application logger at WARNING
        # level so they remain visible without spamming.
        logging.warning("Healthcheck handler: " + (format % args))

    # ------------------------------------------------------------------ helpers
    def _send_json(self, status_code: int, payload: dict) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # Discourage caches and proxies from holding onto health responses.
        self.send_header("Cache-Control", "no-store")
        # Minimal hardening: never let a healthcheck response be embedded
        # in another origin's frame, and don't sniff content types.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_plain(self, status_code: int, message: str) -> None:
        body = message.encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _check_token(self) -> bool:
        """Return True if no token is configured or the request supplies it."""
        token = (self.state.status_token or "").strip()
        if not token:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False
        # Constant-time comparison to thwart timing attacks. The token length
        # itself is leaked by string length but that's acceptable for a
        # homelab tool.
        import hmac
        return hmac.compare_digest(header[len("Bearer "):].strip(), token)

    # ------------------------------------------------------------------ routes
    def _route_health(self) -> None:
        s = self.state
        db_ok, db_reason = _check_db(s.db_path)
        scheduler_ok = _check_scheduler(s.scheduler)
        mqtt_ok = _check_mqtt(s.mqtt_is_connected)
        shutting_down = s.shutting_down.is_set()

        # MQTT is NOT a hard requirement — the periodic scheduler job is the
        # safety net that picks up missed events. We surface its state but
        # do not flunk the healthcheck on it.
        is_healthy = db_ok and scheduler_ok and not shutting_down

        payload = {
            "status": "ok" if is_healthy else "unhealthy",
            "checks": {
                "db": "ok" if db_ok else "fail",
                "scheduler": "ok" if scheduler_ok else "fail",
                "mqtt": "ok" if mqtt_ok else "disconnected",
            },
        }
        if shutting_down:
            payload["checks"]["lifecycle"] = "shutting_down"
        if not db_ok and db_reason:
            payload["checks"]["db_reason"] = db_reason
        self._send_json(200 if is_healthy else 503, payload)

    def _route_status(self) -> None:
        if not self._check_token():
            # Use the standard challenge so curl --user works if someone
            # mistakenly tries basic auth.
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Bearer realm="status"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        s = self.state

        # Aggregate stats from the existing health-stats helper. This already
        # filters to non-sensitive aggregates (counts and kinds only).
        try:
            stats = database.get_health_stats()
        except Exception as e:
            logging.warning(f"Healthcheck /status failed to load stats: {e}")
            self._send_json(503, {"status": "stats_unavailable"})
            return

        db_ok, _ = _check_db(s.db_path)
        scheduler_ok = _check_scheduler(s.scheduler)
        mqtt_ok = _check_mqtt(s.mqtt_is_connected)

        # Project a SAFE subset of the stats. Specifically we strip
        # `oldest_pending_event_id` (an event id can leak surveillance
        # timestamps if /status is exposed externally without auth).
        safe_stats = {
            "uploaded_last_24h": stats.get("uploaded_last_24h", 0),
            "pending_total": stats.get("pending_total", 0),
            "pending_lt_1d": stats.get("pending_lt_1d", 0),
            "pending_1d_2d": stats.get("pending_1d_2d", 0),
            "pending_2d_3d": stats.get("pending_2d_3d", 0),
            "pending_gt_3d": stats.get("pending_gt_3d", 0),
            "oldest_pending_age_days": stats.get("oldest_pending_age_days"),
            "total_uploaded": stats.get("total_uploaded", 0),
            "pending_error_kinds": [
                {"kind": k, "count": c}
                for k, c in stats.get("pending_error_kinds", [])
            ],
        }

        self._send_json(200, {
            "status": "ok",
            "subsystems": {
                "db": db_ok,
                "scheduler": scheduler_ok,
                "mqtt": mqtt_ok,
            },
            "stats": safe_stats,
        })

    # ------------------------------------------------------------------ verbs
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._route_health()
        elif path == "/status":
            self._route_status()
        else:
            self._send_plain(404, "not found")

    def do_HEAD(self):
        # Treat HEAD identically to GET — same status + headers, no body.
        # The _send_* helpers already check self.command to suppress the body.
        self.do_GET()

    def _reject_method(self):
        self.send_response(405)
        self.send_header("Allow", "GET, HEAD")
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_POST = _reject_method
    do_PUT = _reject_method
    do_DELETE = _reject_method
    do_PATCH = _reject_method
    do_OPTIONS = _reject_method


def start_healthcheck_server(
    state: HealthState,
    host: str = "0.0.0.0",
    port: int = 8080,
) -> tuple[ThreadingHTTPServer, threading.Thread]:
    """
    Start the healthcheck HTTP server in a background daemon thread.

    Returns the (server, thread) tuple so the caller can shut it down cleanly
    on graceful termination.
    """
    # Build a one-off handler subclass that closes over `state`. Using a
    # class-level attribute (rather than a global) keeps the module
    # re-entrant and testable.
    handler_cls = type(
        "HealthCheckHandler",
        (_SilentHandler,),
        {"state": state},
    )

    server = ThreadingHTTPServer((host, port), handler_cls)
    # Mark sockets as daemon so the process can exit even if a request is
    # in-flight; combined with threading.Thread(daemon=True) below this
    # means a Ctrl+C is honoured immediately.
    server.daemon_threads = True

    thread = threading.Thread(
        target=server.serve_forever,
        name="healthcheck-server",
        daemon=True,
    )
    thread.start()
    logging.info(f"Healthcheck server listening on http://{host}:{port}/health")
    return server, thread
