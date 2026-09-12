# Frigate to Google Drive Instant Uploader with MQTT

Uploads the recorded portions of completed Frigate object events to Google Drive.
MQTT queues events promptly; HTTP reconciles the complete DB retention window every
10 minutes, including long events that ended during a disconnected period.

## Recovery behavior

- Pending work and confirmed unavailable records persist across restarts and DB cleanup.
- Transient failures retry indefinitely with backoff from 1 minute to 1 hour.
- Source absence requires successful checks at least one hour apart. Network and
  authentication errors never count as proof that a recording disappeared.
- Existing recordings are exported in parts of at most 5 minutes, targeting 128 MiB.
  Parts use a 512 MiB download safety limit and a persistent spool capped near 2 GiB.
  Hitting a safety limit retains the job for investigation; it never marks it uploaded.
- Each part is checked with ffprobe, then verified against Drive size and MD5.
  Pre-generated Drive IDs recover uploads whose success response was lost.
- Single-part events retain their filename. Multipart events add `__part-00001`, etc.
- No object-label or total-event-duration filter discards existing recordings.
  Motion without an object event is outside this uploader's scope.
- Recovery depends on Frigate retaining the source until it is downloaded. A long
  outage extending beyond source retention can still leave unavailable material.

`DB_RETENTION_DAYS` applies only to successful deduplication markers. Set it above
Frigate retention. Legacy `MAX_RETRY_ATTEMPTS`, `MAX_CLIP_SIZE`, and
`SKIP_EVENTS_LONGER_THAN_SECONDS` no longer control the durable worker.

## Features

Configuration is provided through Compose's `env_file`; credentials, SQLite data,
logs and downloaded video parts are excluded from the Docker build context.
They remain in the existing runtime bind mounts.

For an application-only redeployment, retain the verified local runtime image and
build the committed source without updating Python or system packages:

```bash
base_image=$(docker inspect -f '{{.Image}}' frigate-gdrive-instant-uploader)
revision=$(git rev-parse HEAD)
docker build -f Dockerfile.runtime --build-arg RUNTIME_BASE_IMAGE="$base_image" \
  --label org.opencontainers.image.revision="$revision" -t frigate-uploader:verified .
```

Test the image before assigning it to the Compose service. A normal fresh build
still uses `Dockerfile`. Never publish an image derived from an older runtime
that might contain credentials in historical layers; use a clean build context
and review dependencies before distributing images.

- **Prompt queueing** via MQTT (completed events become eligible after 30 seconds)
- **Self-healing retry queue:** failed events remain in SQLite and retry with bounded backoff
- **Missing-source records:** confirmed absent recordings retain an explicit unavailable status
- **Folder structure based on recording date:** `/<UPLOAD_DIR>/<YEAR>/<MONTH>/<DAY>/`
- **Filename includes detected object label:** e.g. `2026-05-15-19-51-14__inside_kitchen__person__<event_id>.mp4`
- **Thread-safe uploads:** a global lock serializes concurrent Google Drive API calls (prevents SSL errors)
- **SQLite WAL mode** for safer concurrent reads/writes
- **Optional Google Drive retention** – delete files older than X days (set `GDRIVE_RETENTION_DAYS=0` to disable)
- **Optional Mattermost notifications:**
  - Real-time error alerts (via logging handler)
  - **Daily health report** at 09:00 with color-coded severity (green/orange/red) and recommended actions

You'll need an MQTT broker like Apache Mosquitto. In a typical setup, Frigate, Mosquitto and this script run
on the same host (e.g. Proxmox LXC containers).

## Requirements
- Python 3.12 (when running outside Docker)
- MQTT broker (e.g. Mosquitto)
- Frigate with MQTT configured
- Google Service Account with Drive access

# Example Frigate configuration
```yaml

mqtt:
  host: 192.168.0.55
  user: username
  password: example-password-change-me
  port: 1883
  topic_prefix: frigate
  client_id: frigate

# rest of your config.yml
````

Check if your MQTT broker is working by subscribing to the topic `frigate/events` with a MQTT client like MQTT Explorer 
or mosquitto_sub. If so, you should see events from Frigate and can use this script.

# Usage without Docker
1. clone this repository
2. rename `env_example` to `.env` and change values to your needs
3. run `python setup.py` in project root directory to install all required packages
4. create a project in google cloud console and enable drive api
5. create a service account and give it access to your Google Drive
6. activate domain-wide-delegation for the service account and add the necessary scope "https://www.googleapis.com/auth/drive" to prevent "Quota Exceeded" errors if you upload more than 15 GB per day.
7. download the service account json file from Google and copy its content to `credentials/service_account.json`
8. run `python main.py` in project root directory

# Usage with Docker
1. clone this repository
2. rename `env_example` to `.env` and change values to your needs
3. create a project in google cloud console and enable drive api
4. create a service account and give it access to your Google Drive
5. download the service account json file from Google and copy its content to `credentials/service_account.json`
6. activate domain-wide-delegation for the service account and add the necessary scope "https://www.googleapis.com/auth/drive" to prevent "Quota Exceeded" errors if you upload more than 15 GB per day.
7. run `docker compose up -d` in project root directory
8. check logs with `docker logs frigate-gdrive-instant-uploader` or see `/logs/app.log`

# Configuration

All configuration is read from `.env` (use `env_example` as template).

| Variable | Default | Description |
|---|---|---|
| `TZ` | `Europe/Istanbul` | Container timezone (also affects log timestamps and Daily Report) |
| `LOGGING_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL` |
| `FRIGATE_URL` | – | Frigate base URL incl. scheme and port |
| `MQTT_BROKER_ADDRESS` / `MQTT_PORT` / `MQTT_USER` / `MQTT_PASSWORD` / `MQTT_TOPIC` | – | MQTT broker connection details |
| `SERVICE_ACCOUNT_FILE` | `credentials/service_account.json` | Google service account JSON |
| `GOOGLE_ACCOUNT_TO_IMPERSONATE` | – | Drive account the service account impersonates |
| `UPLOAD_DIR` | `frigate` | Root folder in Drive; videos go to `/UPLOAD_DIR/YYYY/MM/DD/` |
| `DB_RETENTION_DAYS` | `30` | Delete SQLite rows older than this, regardless of upload status. Drive files unaffected |
| `MAX_RETRY_ATTEMPTS` | `50` | Legacy setting; durable worker retries transient errors indefinitely |
| `MAX_CLIP_SIZE` | – | Legacy setting; durable worker exports bounded parts without discarding whole events |
| `SKIP_EVENTS_LONGER_THAN_SECONDS` | `0` | Legacy setting; durable worker does not discard long events |
| `HEALTH_REPORT_TIME` | `09:00` | Time of day (24h `HH:MM`, container timezone) to send the Daily Health Report. Invalid values fall back to `09:00`. |
| `HEALTH_REPORT_ONLY_ON_ISSUES` | `false` | When `true`, OK reports are only logged (INFO), not sent to Mattermost. WARNING / CRITICAL reports are always sent. |
| `HEALTHCHECK_BIND` | `0.0.0.0` | Interface the in-process healthcheck HTTP server binds to. Use `127.0.0.1` to restrict to the container's loopback. |
| `HEALTHCHECK_PORT` | `8080` | Port the healthcheck server listens on. The Docker `HEALTHCHECK` directive in the Dockerfile honours the same env var. |
| `HEALTHCHECK_TOKEN` | – | Optional bearer token guarding `/status`. `/health` is always unauthenticated so Docker's `HEALTHCHECK` probe can reach it. |
| `GDRIVE_RETENTION_DAYS` | `0` | Delete physical files in Drive older than this many days (`0` = off) |
| `MATTERMOST_WEBHOOK_URL` | – | Optional. Enables error alerts and the Daily Health Report |
| `MATTERMOST_PREFIX` | – | Optional. String prepended to every Mattermost message |

# Scheduled Jobs

| Interval | Job | Purpose |
|---|---|---|
| Every 10 min | `run_every_x_minutes` | Reconcile source events and clean successful DB markers; separate worker handles retries |
| Every 6 h | `run_every_6_hours` | Log/notify about hard-failed events (legacy) |
| Daily, `HEALTH_REPORT_TIME` (default 09:00) | `daily_health_report` | Mattermost status report (OK / WARNING / CRITICAL) |
| Daily | `cleanup_old_files_on_drive` | Delete Google Drive files older than `GDRIVE_RETENTION_DAYS` (skipped if `0`) |

# Mattermost Health Report

When `MATTERMOST_WEBHOOK_URL` is configured, a daily summary is posted at `HEALTH_REPORT_TIME` (default `09:00`, container timezone):

- :white_check_mark: **OK (green):** all uploads healthy
- :warning: **WARNING (orange):** events pending for 1–3 days
- :rotating_light: **CRITICAL (red):** events pending > 3 days, or no uploads in last 24h while backlog exists

Set `HEALTH_REPORT_ONLY_ON_ISSUES=true` to suppress OK messages — useful if you only want to hear from the tool when something is wrong. WARNING and CRITICAL are always sent.

The CRITICAL message includes copy-paste-ready debug commands.

To trigger the report on demand:
```bash
docker exec -it frigate-gdrive-instant-uploader python -c "from main import daily_health_report; daily_health_report()"
```

# Healthcheck HTTP API

A lightweight HTTP server runs in-process and exposes two endpoints. The
Dockerfile contains a `HEALTHCHECK` directive that probes `/health` from
inside the container, so external port exposure is **optional**.

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /health` | none | Liveness probe. `200 OK` if DB, scheduler and upload worker are up, `503` otherwise. MQTT disconnects do not flunk this — the periodic job is the safety net. |
| `GET /status` | optional bearer token | Detailed JSON: aggregate counts, error-kind breakdown, subsystem state. No sensitive data (no event IDs, no paths, no URLs). |

## Configure

```bash
HEALTHCHECK_BIND=0.0.0.0       # default; use 127.0.0.1 to restrict
HEALTHCHECK_PORT=8080
HEALTHCHECK_TOKEN=             # leave empty to disable auth on /status
```

## Probe from inside the container

```bash
docker exec frigate-gdrive-instant-uploader \
    python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/health').read().decode())"
```

## Probe from outside (optional)

Add a port mapping to `docker-compose.yml`:

```yaml
services:
  frigate-gdrive-instant-uploader:
    ports:
      - "8080:8080"
```

Then:

```bash
curl http://your-host:8080/health
curl -H "Authorization: Bearer $HEALTHCHECK_TOKEN" http://your-host:8080/status
```

## Sample responses

`/health` (healthy):
```json
{"status":"ok","checks":{"db":"ok","scheduler":"ok","mqtt":"ok"}}
```

`/health` (unhealthy — DB unreachable):
```json
{"status":"unhealthy","checks":{"db":"fail","scheduler":"ok","mqtt":"ok","db_reason":"db_unreachable"}}
```

`/status`:
```json
{
  "status": "ok",
  "subsystems": {"db": true, "scheduler": true, "mqtt": true},
  "stats": {
    "uploaded_last_24h": 42,
    "pending_total": 3,
    "pending_lt_1d": 3,
    "pending_1d_2d": 0,
    "pending_2d_3d": 0,
    "pending_gt_3d": 0,
    "oldest_pending_age_days": 0.4,
    "total_uploaded": 12873,
    "pending_error_kinds": [{"kind": "frigate_download_truncated", "count": 2}]
  }
}
```

# Troubleshooting

## Downloads, outages and long events

The worker exports the recorded time ranges rather than requesting a many-hour
event export. A corrupt or truncated part remains pending; completed parts are
not uploaded again. `upload_jobs.error` and `events.last_error_kind` record the
latest failure. Check source retention promptly when failures persist.

MQTT callbacks do no downloads or cloud work. Paho retries initial connections
and subsequent disconnects automatically. The daily report shows the actual
MQTT connection state separately from HTTP reconciliation and uploads.

Do not manually set `uploaded=1` to silence an error. Source-unavailable events
have `uploaded=0`, `retry=0` and an `upload_jobs.state` of `unavailable`; partial
success remains visible in `upload_parts`.

Inspect the local database:
```bash
docker exec -it frigate-gdrive-instant-uploader sqlite3 /app/db/events.db
```

Useful queries:
```sql
-- Total pending in queue
SELECT COUNT(*) AS pending_total FROM events WHERE uploaded = 0 AND retry = 1;

-- Pending events per recorded day
SELECT
  date(datetime(start_time, 'unixepoch', 'localtime')) AS recorded_day,
  COUNT(*) AS pending_count
FROM events
WHERE uploaded = 0 AND retry = 1
GROUP BY recorded_day
ORDER BY recorded_day DESC;

-- Daily overview: uploaded vs pending vs given up
SELECT
  date(datetime(start_time, 'unixepoch', 'localtime')) AS recorded_day,
  SUM(CASE WHEN uploaded = 1 THEN 1 ELSE 0 END) AS uploaded,
  SUM(CASE WHEN uploaded = 0 AND retry = 1 THEN 1 ELSE 0 END) AS pending,
  SUM(CASE WHEN uploaded = 0 AND retry = 0 THEN 1 ELSE 0 END) AS given_up,
  COUNT(*) AS total
FROM events
GROUP BY recorded_day
ORDER BY recorded_day DESC;

-- Oldest pending events
SELECT event_id, tries, datetime(start_time,'unixepoch','localtime') AS recorded, created
FROM events WHERE uploaded = 0 ORDER BY created ASC LIMIT 20;
```

# Notes

- Folder structure in Google Drive is based on the event's **recording time** (`start_time`), not the upload time.
  A clip recorded on May 14 will always land in `/UPLOAD_DIR/2026/05/14/`, even if uploaded later.
- Files manually deleted in Google Drive are **not re-uploaded**, because the SQLite DB still records them as `uploaded=1`.
- Confirmed missing-source records remain in the DB; unavailable does not mean successfully uploaded.
