#!/usr/bin/env python3
"""
Quick smoke test — validates the healthcheck endpoint and recent DB changes
without touching Frigate, Google Drive, MQTT, or Mattermost.

Run: python quick_test.py
"""

import json
import os
import sqlite3
import sys
import tempfile
import time
import urllib.request
import urllib.error

# Use a temp DB so we never touch the real one.
tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
tmp.close()
DB_FILE = tmp.name

sys.path.insert(0, '.')

# Point the database module at our temp file before anything else imports it.
from src import database
database.DB_PATH = DB_FILE

# Build the real schema + migrations on the temp DB.
from src.database import init_db, run_migrations
init_db()
run_migrations()

# Seed with realistic data so /status has something to report.
conn = sqlite3.connect(DB_FILE)
now_ts = time.time()
rows = [
    ("ev1", 1, None, 1, 0, time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now_ts - 3600)), now_ts - 3600),
    ("ev2", 0, "frigate_download_timeout", 1, 3, time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now_ts - 7200)), now_ts - 7200),
    ("ev3", 0, "drive_network", 0, 5, time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now_ts - 90000)), now_ts - 90000),
]
conn.executemany(
    "INSERT INTO events (event_id, uploaded, last_error_kind, retry, tries, created, start_time) VALUES (?,?,?,?,?,?,?)",
    rows
)
conn.commit()
conn.close()

from src.healthcheck import HealthState, start_healthcheck_server


class FakeScheduler:
    running = True


state = HealthState(
    db_path=DB_FILE,
    scheduler=FakeScheduler(),
    mqtt_is_connected=lambda: True,
    status_token="test-token-42",
)

server, _ = start_healthcheck_server(state, host="127.0.0.1", port=18081)
time.sleep(0.3)


def fetch(path, headers=None, method="GET"):
    req = urllib.request.Request(f"http://127.0.0.1:18081{path}", headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.getcode(), r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def assert_eq(label, actual, expected):
    if actual != expected:
        print(f"FAIL  {label}: expected {expected!r}, got {actual!r}")
        sys.exit(1)
    print(f"OK    {label}")


# 1. /health happy path
code, body = fetch("/health")
payload = json.loads(body)
assert_eq("/health status code", code, 200)
assert_eq("/health status payload", payload["status"], "ok")
assert_eq("/health db ok", payload["checks"]["db"], "ok")
assert_eq("/health scheduler ok", payload["checks"]["scheduler"], "ok")
assert_eq("/health mqtt ok", payload["checks"]["mqtt"], "ok")

# 2. /status without token → 401
code, _ = fetch("/status")
assert_eq("/status no auth", code, 401)

# 3. /status with wrong token → 401
code, _ = fetch("/status", {"Authorization": "Bearer wrong"})
assert_eq("/status wrong token", code, 401)

# 4. /status with correct token → 200 + realistic stats
code, body = fetch("/status", {"Authorization": "Bearer test-token-42"})
payload = json.loads(body)
assert_eq("/status auth code", code, 200)
assert_eq("/status uploaded_last_24h", payload["stats"]["uploaded_last_24h"], 1)
assert_eq("/status pending_total", payload["stats"]["pending_total"], 2)
assert_eq("/status total_uploaded", payload["stats"]["total_uploaded"], 1)
assert_eq("/status pending_gt_3d", payload["stats"]["pending_gt_3d"], 1)
# Error-kind breakdown
kinds = {k["kind"]: k["count"] for k in payload["stats"]["pending_error_kinds"]}
assert_eq("/status error_kind drive_network", kinds.get("drive_network"), 1)
assert_eq("/status error_kind frigate_download_timeout", kinds.get("frigate_download_timeout"), 1)
# Security: no event IDs leaked
assert "oldest_pending_event_id" not in payload["stats"], "FAIL: event_id leaked in /status"
print("OK    /status no event_id leak")

# 5. Shutdown flag → 503
state.shutting_down.set()
code, body = fetch("/health")
payload = json.loads(body)
assert_eq("/health shutdown code", code, 503)
assert_eq("/health shutdown lifecycle", payload["checks"].get("lifecycle"), "shutting_down")
state.shutting_down.clear()

# 6. Scheduler down → 503
FakeScheduler.running = False
code, body = fetch("/health")
payload = json.loads(body)
assert_eq("/health scheduler down code", code, 503)
assert_eq("/health scheduler down flag", payload["checks"]["scheduler"], "fail")
FakeScheduler.running = True

# 7. HEAD /health → no body
code, body = fetch("/health", method="HEAD")
assert_eq("HEAD /health code", code, 200)
assert_eq("HEAD /health body empty", len(body), 0)

# 8. POST /health → 405
code, _ = fetch("/health", method="POST")
assert_eq("POST /health code", code, 405)

# 9. Unknown path → 404
code, _ = fetch("/favicon.ico")
assert_eq("unknown path code", code, 404)

# Clean up
server.shutdown()
os.unlink(DB_FILE)

print("\n=== All 16 checks passed. Healthcheck + DB integration OK. ===")
