# Frigate to Google Drive Instant Uploader with MQTT

Uploads event clips from Frigate to Google Drive **instantly** via MQTT and reliably catches up on missed
uploads via a 10-minute retry scheduler. A SQLite database keeps track of every event so nothing is lost
during internet outages or container restarts.

## Features
- **Instant upload** via MQTT (`event end` triggers upload within seconds)
- **Self-healing retry queue:** events that fail to upload stay in the DB and are retried every 10 minutes
- **Hard-fail cleanup:** events that no longer exist on Frigate (HTTP 404) are removed from the DB automatically – no log spam
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
  password: secret
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

# Scheduled Jobs

| Interval | Job | Purpose |
|---|---|---|
| Every 10 min | `run_every_x_minutes` | Fetch missed events from Frigate, retry failed uploads, clean up old uploaded events |
| Every 6 h | `run_every_6_hours` | Log/notify about failed events (legacy) |
| Daily 09:00 | `daily_health_report` | Mattermost status report (OK / WARNING / CRITICAL) |
| Daily | `cleanup_old_files_on_drive` | Delete Google Drive files older than `GDRIVE_RETENTION_DAYS` (skipped if 0) |

# Mattermost Health Report

When `MATTERMOST_WEBHOOK_URL` is configured, a daily summary is posted at 09:00 (container timezone):

- :white_check_mark: **OK (green):** all uploads healthy
- :warning: **WARNING (orange):** events pending for 1–3 days
- :rotating_light: **CRITICAL (red):** events pending > 3 days, or no uploads in last 24h while backlog exists

The CRITICAL message includes copy-paste-ready debug commands.

To trigger the report on demand:
```bash
docker exec -it frigate-gdrive-instant-uploader python -c "from main import daily_health_report; daily_health_report()"
```

# Troubleshooting

Inspect the local database:
```bash
docker exec -it frigate-gdrive-instant-uploader sqlite3 /app/db/events.db
```

Useful queries:
```sql
-- Overall status (uploaded × retry)
SELECT uploaded, retry, COUNT(*) FROM events GROUP BY uploaded, retry;

-- Pending events grouped by age
SELECT
  CASE
    WHEN created < datetime('now', '-30 days') THEN '> 30 days'
    WHEN created < datetime('now', '-14 days') THEN '14-30 days'
    WHEN created < datetime('now', '-7 days')  THEN '7-14 days'
    ELSE '< 7 days'
  END AS bucket,
  COUNT(*) AS amount
FROM events WHERE uploaded = 0 GROUP BY bucket;

-- Oldest pending events
SELECT event_id, tries, datetime(start_time,'unixepoch','localtime') AS recorded, created
FROM events WHERE uploaded = 0 ORDER BY created ASC LIMIT 20;
```

# Notes

- Folder structure in Google Drive is based on the event's **recording time** (`start_time`), not the upload time.
  A clip recorded on May 14 will always land in `/UPLOAD_DIR/2026/05/14/`, even if uploaded later.
- Files manually deleted in Google Drive are **not re-uploaded**, because the SQLite DB still records them as `uploaded=1`.
- Hard-failed events (Frigate returned 404) are deleted from the DB to keep it clean. They cannot be recovered.