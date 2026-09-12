import json
import logging
import os
import requests
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
import socket

# Erstelle das Log-Verzeichnis, falls es nicht existiert
os.makedirs('logs', exist_ok=True)

# Konfiguriere das Logging zuerst
LOGGING_LEVEL = os.getenv('LOGGING_LEVEL', 'INFO').upper()

# Mögliche Log-Level mit Standardwerten
LOG_LEVELS = {
    'DEBUG': logging.DEBUG,
    'INFO': logging.INFO,
    'WARNING': logging.WARNING,
    'ERROR': logging.ERROR,
    'CRITICAL': logging.CRITICAL
}

# Wähle das Log-Level aus der Umgebungsvariable oder verwende INFO als Standard
NUMERIC_LEVEL = LOG_LEVELS.get(LOGGING_LEVEL, logging.INFO)
print(f"Aktuelles Log-Level: {LOGGING_LEVEL} (numerisch: {NUMERIC_LEVEL})")

# Root-Logger konfigurieren
root_logger = logging.getLogger()
root_logger.setLevel(NUMERIC_LEVEL)  # Wichtig: Dies setzt das minimale Level für den Root-Logger

# Bestehende Handler entfernen
for handler in root_logger.handlers[:]:
    root_logger.removeHandler(handler)
    handler.close()

# Konsole-Handler
console_handler = logging.StreamHandler()
console_handler.setLevel(NUMERIC_LEVEL)  # Level für die Konsole
console_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
console_handler.setFormatter(console_formatter)

# Datei-Handler
log_file = 'logs/app.log'
file_handler = RotatingFileHandler(
    log_file, 
    maxBytes=5 * 1024 * 1024,  # 5 MB
    backupCount=5,
    encoding='utf-8'
)
file_handler.setLevel(NUMERIC_LEVEL)  # Level für die Datei
file_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
file_handler.setFormatter(file_formatter)

# Handler hinzufügen
root_logger.addHandler(console_handler)
root_logger.addHandler(file_handler)

# Deaktiviere die Propagation zu anderen Loggern, um doppelte Logs zu vermeiden
root_logger.propagate = False

# Logger für dieses Modul
logger = logging.getLogger(__name__)
logger.info(f"Logging initialisiert mit Level {LOGGING_LEVEL}")

# Jetzt die restlichen Imports durchführen, nachdem das Logging eingerichtet ist
from dotenv import load_dotenv
import paho.mqtt.client as mqtt
from apscheduler.schedulers.background import BackgroundScheduler

from src import database, google_drive, work_queue, segmented_upload
from src.frigate_api import fetch_all_events, fetch_event, check_frigate_reachable, EventNotFoundError, ClipNotAvailableError, ClipTooLargeError, FrigateUnreachableError
from src.google_drive import cleanup_old_files_on_drive, service
from src.healthcheck import HealthState, start_healthcheck_server
from src.mattermost_handler import MattermostHandler, send_mattermost_notification

# Lade Umgebungsvariablen
try:
    load_dotenv()
    logger.info("Umgebungsvariablen geladen")
except Exception as e:
    logger.error(f"Fehler beim Laden der .env Datei: {e}")

# Konfiguration aus Umgebungsvariablen laden
FRIGATE_URL = os.getenv('FRIGATE_URL')
MAX_RETRY_ATTEMPTS = int(os.getenv('MAX_RETRY_ATTEMPTS', '50'))
MQTT_BROKER_ADDRESS = os.getenv('MQTT_BROKER_ADDRESS')
MQTT_PORT = int(os.getenv('MQTT_PORT', '1883'))
MQTT_TOPIC = os.getenv('MQTT_TOPIC')
MQTT_USER = os.getenv('MQTT_USER')
MQTT_PASSWORD = os.getenv('MQTT_PASSWORD')
MATTERMOST_WEBHOOK_URL = os.getenv('MATTERMOST_WEBHOOK_URL')
HEALTH_REPORT_TIME = os.getenv('HEALTH_REPORT_TIME', '09:00')
HEALTHCHECK_BIND = os.getenv('HEALTHCHECK_BIND', '0.0.0.0')
HEALTHCHECK_PORT_RAW = os.getenv('HEALTHCHECK_PORT', '8080')
HEALTHCHECK_TOKEN = os.getenv('HEALTHCHECK_TOKEN', '').strip()


def _parse_healthcheck_port(value, default=8080):
    """Parse HEALTHCHECK_PORT, falling back to the default on bogus input."""
    try:
        port = int(value)
        if 1 <= port <= 65535:
            return port
        raise ValueError("port out of range")
    except (ValueError, TypeError) as e:
        logger.warning(
            f"Invalid HEALTHCHECK_PORT='{value}' ({e}). Falling back to {default}."
        )
        return default


HEALTHCHECK_PORT = _parse_healthcheck_port(HEALTHCHECK_PORT_RAW)


def _parse_skip_events_longer_than(value):
    """
    Parse SKIP_EVENTS_LONGER_THAN_SECONDS env var. Returns the threshold in
    seconds, or 0 if the limit is disabled (empty / '0' / invalid).
    """
    if not value:
        return 0
    try:
        seconds = int(value)
        if seconds < 0:
            raise ValueError("must be >= 0")
        return seconds
    except (ValueError, TypeError) as e:
        logger.warning(
            f"Invalid SKIP_EVENTS_LONGER_THAN_SECONDS='{value}' ({e}). "
            f"Disabling the duration filter."
        )
        return 0


SKIP_EVENTS_LONGER_THAN_SECONDS = _parse_skip_events_longer_than(
    os.getenv('SKIP_EVENTS_LONGER_THAN_SECONDS')
)
if SKIP_EVENTS_LONGER_THAN_SECONDS > 0:
    logger.info(
        f"SKIP_EVENTS_LONGER_THAN_SECONDS configured: events longer than "
        f"{SKIP_EVENTS_LONGER_THAN_SECONDS}s will be marked non-retriable."
    )


# ---------------------------------------------------------------------------
# Configuration validation
# ---------------------------------------------------------------------------


def validate_config():
    """
    Validate all required configuration variables at startup.

    Collects every problem first, prints one clear error per issue with a
    concrete fix hint, and exits with code 1 so the container fails fast
    instead of crashing deep in a request stack with a cryptic traceback.
    """
    errors = []

    # --- required string variables ------------------------------------------------
    _required_strings = {
        'FRIGATE_URL': FRIGATE_URL,
        'MQTT_BROKER_ADDRESS': MQTT_BROKER_ADDRESS,
        'MQTT_TOPIC': MQTT_TOPIC,
        'MQTT_USER': MQTT_USER,
        'MQTT_PASSWORD': MQTT_PASSWORD,
    }
    for name, value in _required_strings.items():
        if not value or not str(value).strip():
            errors.append(f"CONFIG ERROR: {name} is not set or empty.")

    # --- Google Drive variables (read directly via os.getenv because
    #     google_drive.py initialises the service on module import, so we
    #     cannot rely on its module-level attributes being reachable before
    #     a potential import crash.)
    _service_file = os.getenv('SERVICE_ACCOUNT_FILE', '').strip()
    if not _service_file:
        errors.append("CONFIG ERROR: SERVICE_ACCOUNT_FILE is not set.")
    elif not os.path.isfile(_service_file):
        errors.append(
            f"CONFIG ERROR: SERVICE_ACCOUNT_FILE does not exist: {_service_file}"
        )

    _upload_dir = os.getenv('UPLOAD_DIR', '').strip()
    if not _upload_dir:
        errors.append("CONFIG ERROR: UPLOAD_DIR is not set.")

    # --- numeric / range validations ----------------------------------------------
    if not (1 <= MQTT_PORT <= 65535):
        errors.append(
            f"CONFIG ERROR: MQTT_PORT={MQTT_PORT} is out of range (1-65535)."
        )

    if MAX_RETRY_ATTEMPTS < 0:
        errors.append(
            f"CONFIG ERROR: MAX_RETRY_ATTEMPTS={MAX_RETRY_ATTEMPTS} must be >= 0."
        )

    # --- FRIGATE_URL sanity check -------------------------------------------------
    _frigate = str(FRIGATE_URL).strip()
    if _frigate and not (_frigate.startswith('http://') or _frigate.startswith('https://')):
        errors.append(
            f"CONFIG ERROR: FRIGATE_URL='{_frigate}' must start with http:// or https://."
        )

    # --- print all errors at once, then die ---------------------------------------
    if errors:
        for err in errors:
            logging.error(err)
        logging.error(
            "Please add the missing variables to your .env file and restart the container."
        )
        sys.exit(1)

    # --- log active configuration (secrets masked) --------------------------------
    logging.info("Configuration validated successfully.")
    logging.info(f"  FRIGATE_URL={FRIGATE_URL}")
    logging.info(f"  MQTT_BROKER_ADDRESS={MQTT_BROKER_ADDRESS}")
    logging.info(f"  MQTT_PORT={MQTT_PORT}")
    logging.info(f"  MQTT_TOPIC={MQTT_TOPIC}")
    logging.info(f"  MQTT_USER={MQTT_USER}")
    logging.info(f"  MQTT_PASSWORD={'***' if MQTT_PASSWORD else '(empty)'}")
    logging.info(f"  UPLOAD_DIR={_upload_dir}")
    logging.info(f"  SERVICE_ACCOUNT_FILE={_service_file}")
    _impersonate = os.getenv('GOOGLE_ACCOUNT_TO_IMPERSONATE', '').strip()
    logging.info(f"  GOOGLE_ACCOUNT_TO_IMPERSONATE={_impersonate or '(none)'}")
    _max_clip = os.getenv('MAX_CLIP_SIZE', '').strip()
    logging.info(f"  MAX_CLIP_SIZE={_max_clip or '(unlimited)'}")
    logging.info(f"  MAX_RETRY_ATTEMPTS={MAX_RETRY_ATTEMPTS}")
    logging.info(f"  SKIP_EVENTS_LONGER_THAN_SECONDS={SKIP_EVENTS_LONGER_THAN_SECONDS}")
    logging.info(f"  DB_RETENTION_DAYS={os.getenv('DB_RETENTION_DAYS', '30')}")
    logging.info(f"  GDRIVE_RETENTION_DAYS={os.getenv('GDRIVE_RETENTION_DAYS', '0')}")
    logging.info(f"  HEALTH_REPORT_TIME={HEALTH_REPORT_TIME}")
    logging.info(f"  HEALTH_REPORT_ONLY_ON_ISSUES={HEALTH_REPORT_ONLY_ON_ISSUES}")
    logging.info(f"  HEALTHCHECK_BIND={HEALTHCHECK_BIND}")
    logging.info(f"  HEALTHCHECK_PORT={HEALTHCHECK_PORT}")
    logging.info(f"  HEALTHCHECK_TOKEN={'***' if HEALTHCHECK_TOKEN else '(none)'}")
    logging.info(f"  MATTERMOST_WEBHOOK_URL={'***' if MATTERMOST_WEBHOOK_URL else '(none)'}")


def parse_bool_env(value, default=False):
    """
    Parses a boolean env var. Accepts the usual suspects (case-insensitive):
      true/false, yes/no, on/off, 1/0, y/n, t/f.
    Returns `default` for None or unrecognised values.
    """
    if value is None:
        return default
    v = value.strip().lower()
    if v in ('1', 'true', 'yes', 'on', 'y', 't'):
        return True
    if v in ('0', 'false', 'no', 'off', 'n', 'f', ''):
        return False
    return default


# When True, suppress the Mattermost message of an OK Daily Health Report
# (WARNING / CRITICAL reports are always sent). Default: False (send always).
HEALTH_REPORT_ONLY_ON_ISSUES = parse_bool_env(os.getenv('HEALTH_REPORT_ONLY_ON_ISSUES'), default=False)


def parse_health_report_time(value, default_hour=9, default_minute=0):
    """
    Parse HEALTH_REPORT_TIME env var ('HH:MM' in 24h) into (hour, minute).
    Falls back to the provided defaults on any parse error and logs a warning.
    """
    try:
        parts = value.strip().split(':')
        if len(parts) != 2:
            raise ValueError("expected format 'HH:MM'")
        hour, minute = int(parts[0]), int(parts[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("hour must be 0-23, minute 0-59")
        return hour, minute
    except (ValueError, AttributeError) as e:
        logger.warning(
            f"Invalid HEALTH_REPORT_TIME='{value}' ({e}). "
            f"Falling back to {default_hour:02d}:{default_minute:02d}."
        )
        return default_hour, default_minute

# Mattermost-Handler hinzufügen, falls konfiguriert
if MATTERMOST_WEBHOOK_URL:
    try:
        mattermost_handler = MattermostHandler(MATTERMOST_WEBHOOK_URL)
        mattermost_handler.setLevel(logging.ERROR)
        mattermost_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        mattermost_handler.setFormatter(mattermost_formatter)
        root_logger.addHandler(mattermost_handler)
        logger.info("Mattermost-Benachrichtigungen aktiviert")
    except Exception as e:
        logger.error(f"Fehler beim Initialisieren des Mattermost-Handlers: {e}")
else:
    logger.warning("MATTERMOST_WEBHOOK_URL nicht gesetzt. Mattermost-Benachrichtigungen sind deaktiviert.")


_work_ready = threading.Event()
_upload_worker = None


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        logging.info('MQTT connected; subscribing to event notifications')
        client.subscribe(MQTT_TOPIC)
    else:
        logging.warning('MQTT connection rejected: %s', reason_code)


def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload)
        if payload.get('type') == 'end':
            work_queue.enqueue(payload['after'])
            _work_ready.set()
    except Exception as e:
        # No downloads, cloud calls, sleeps or webhooks in the MQTT callback.
        logging.warning('MQTT message could not be queued (%s); HTTP reconciliation will retry', type(e).__name__)


def format_event_recorded_at(start_time):
    """Returns a human-readable recording timestamp for an event (using TZ env)."""
    try:
        import pytz
        tz = pytz.timezone(os.getenv('TZ', 'UTC'))
        return datetime.fromtimestamp(start_time, pytz.utc).astimezone(tz).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return f"start_time={start_time}"


def handle_single_event(event_data, skip_wait=False, online=None):
    work_queue.enqueue(event_data)
    _work_ready.set()
    return True


def upload_worker():
    while True:
        try:
            jobs = work_queue.due()
            for job in jobs:
                event_id = job['event_id']
                try:
                    event = json.loads(job['metadata']) if job['metadata'] else fetch_event(FRIGATE_URL, event_id)
                    if event.get('end_time') is None:
                        work_queue.defer(event_id, 60)
                        continue
                    # Save metadata before the original Frigate event can expire.
                    work_queue.enqueue(event)
                    with google_drive.upload_lock:
                        segmented_upload.step(event, FRIGATE_URL, google_drive)
                except (EventNotFoundError, segmented_upload.SourceMissing) as e:
                    work_queue.failed(event_id, str(e) if isinstance(e, segmented_upload.SourceMissing) else 'event_missing', missing=True)
                except Exception as e:
                    kind = 'drive_http_' + str(e.resp.status) if isinstance(e, google_drive.HttpError) else type(e).__name__
                    if isinstance(e, google_drive.HttpError) and e.resp.status == 404:
                        google_drive._folder_id_cache.clear()
                    work_queue.failed(event_id, kind)
                    logging.warning('Upload deferred for %s (%s); retry remains enabled', event_id, kind)
        except Exception:
            logging.exception('Upload worker cycle failed; retrying')
        _work_ready.wait(5)
        _work_ready.clear()


_frigate_unreachable_since = None
_PROGRAM_START_TIME = None


def _get_program_start_time():
    """Lazy initialization of PROGRAM_START_TIME to avoid reset on import."""
    global _PROGRAM_START_TIME
    if _PROGRAM_START_TIME is None:
        _PROGRAM_START_TIME = datetime.now()
    return _PROGRAM_START_TIME

def _format_duration(seconds):
    """Format a duration in seconds into a compact 'XhYmZs' string."""
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        return f"{seconds // 3600}h {seconds % 3600 // 60}m {seconds % 60}s"
    if seconds >= 60:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds}s"


def _notify_frigate_unreachable_once():
    """Idempotent: sends a Mattermost warning only on the first detected outage."""
    global _frigate_unreachable_since
    if _frigate_unreachable_since is not None:
        return  # already notified for this outage
    _frigate_unreachable_since = datetime.now()
    ts = _frigate_unreachable_since.strftime('%Y-%m-%d %H:%M:%S')
    send_mattermost_notification(
        title=":warning: Frigate unreachable",
        text=(
            f"Frigate has been unreachable since **{ts}** (container time).\n\n"
            f"Uploads are paused until Frigate responds again. "
            f"You will receive a second message once the connection is restored."
        ),
        color="#ffae42",
    )


def _notify_frigate_recovered_once():
    """Idempotent: sends a Mattermost OK only if we had previously notified about an outage."""
    global _frigate_unreachable_since
    if _frigate_unreachable_since is None:
        return  # nothing to recover from
    downtime_str = _format_duration((datetime.now() - _frigate_unreachable_since).total_seconds())
    _frigate_unreachable_since = None
    send_mattermost_notification(
        title=":white_check_mark: Frigate reachable again",
        text=(
            f"Frigate is responding again. Downtime: **{downtime_str}**.\n\n"
            f"Pending events will be processed in the next job run."
        ),
        color="#36a64f",
    )


def handle_all_events():
    # Reconcile the whole local retention window: MAX(start_time) misses objects
    # that started earlier and only ended while MQTT was disconnected.
    after = time.time() - database.DB_RETENTION_DAYS * 86400
    all_events = fetch_all_events(FRIGATE_URL, after=after, batch_size=500)
    if all_events is None:
        _notify_frigate_unreachable_once()
        return
    _notify_frigate_recovered_once()
    for event in all_events:
        work_queue.enqueue(event)
    _work_ready.set()
    logging.info('HTTP reconciliation completed: %s event records checked', len(all_events))


def on_disconnect(client, userdata, disconnect_flags, rc, properties):
    logging.warning('MQTT disconnected (%s); automatic reconnection continues', rc)


def init_db_and_run_migrations():
    database.init_db()
    database.run_migrations()
    work_queue.initialize()


# Module-level reference to the MQTT client so the healthcheck endpoint can
# query its connection state without importing mqtt internals or relying on
# global singletons. Populated by `mqtt_handler()` once the client is built.
_mqtt_client = None


def _mqtt_is_connected():
    """Return True if the MQTT client exists and reports itself as connected."""
    if _mqtt_client is None:
        return False
    try:
        return bool(_mqtt_client.is_connected())
    except Exception:
        return False


def mqtt_handler():
    global _mqtt_client
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.username_pw_set(MQTT_USER, MQTT_PASSWORD)
    client.on_connect = on_connect
    client.on_message = on_message
    client.on_disconnect = on_disconnect
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    _mqtt_client = client
    client.connect_async(MQTT_BROKER_ADDRESS, MQTT_PORT, 60)
    while True:
        try:
            client.loop_forever(retry_first_connection=True)
        except Exception as e:
            logging.warning('MQTT loop interrupted (%s); restarting in 5 seconds', type(e).__name__)
        time.sleep(5)


def run_every_x_minutes():
    handle_all_events()
    database.cleanup_old_events()


def _get_uptime():
    """Return uptime as a human-readable string (e.g., '2d 14h 30m')."""
    uptime_seconds = (datetime.now() - _get_program_start_time()).total_seconds()
    return _format_duration(uptime_seconds)


def _get_last_successful_upload():
    """
    Return the timestamp of the last successful upload, or None if no uploads yet.
    """
    try:
        timestamp = database.get_last_successful_upload_timestamp()
        if timestamp:
            try:
                import pytz
                tz = pytz.timezone(os.getenv('TZ', 'UTC'))
                dt = datetime.fromtimestamp(timestamp, pytz.utc).astimezone(tz)
                return dt.strftime('%Y-%m-%d %H:%M:%S')
            except Exception:
                return f"timestamp={timestamp}"
        return None
    except Exception as e:
        logging.warning(f"Failed to get last successful upload timestamp: {e}")
        return None


def _get_db_size():
    """Return the DB file size in human-readable format."""
    try:
        db_path = database.DB_PATH
        size_bytes = os.path.getsize(db_path)
        # Convert to human-readable
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size_bytes < 1024:
                return f"{size_bytes:.1f} {unit}"
            size_bytes /= 1024
        return f"{size_bytes:.1f} TB"
    except Exception as e:
        logging.warning(f"Failed to get DB size: {e}")
        return "unknown"


def _get_subsystem_status(scheduler):
    """
    Return subsystem status dict for health report.
    Keys: db (bool), scheduler (bool), mqtt (bool), mqtt_status (str)
    """
    # DB health: try a quick query
    try:
        conn = sqlite3.connect(database.DB_PATH, timeout=2)
        conn.execute("SELECT 1")
        conn.close()
        db_ok = True
    except Exception:
        db_ok = False

    # Scheduler health
    scheduler_ok = scheduler.running if scheduler else False

    # MQTT health
    mqtt_ok = _mqtt_is_connected()
    mqtt_status = "connected" if mqtt_ok else "disconnected"

    return {
        "db": db_ok,
        "scheduler": scheduler_ok,
        "mqtt": mqtt_ok,
        "mqtt_status": mqtt_status,
    }


def _check_clip_availability(event_id):
    """
    Check if a clip is still available on Frigate via HEAD request.
    Returns True if available, False if not (404/400), None on network error.
    """
    try:
        event = fetch_event(FRIGATE_URL, event_id, retries=1, timeout=10)
        if not event.get('end_time'):
            return None
        rows = segmented_upload.recordings(FRIGATE_URL, event['camera'], event['start_time'], event['end_time'])
        return bool(segmented_upload.make_plan(rows, event['start_time'], event['end_time']))
    except EventNotFoundError:
        return False
    except Exception:
        return None


def _get_clip_availability_stats():
    """
    Check availability of clips for retryable pending events (retry > 0).
    Events with retry=0 are non-retriable and should not be marked as "action required".
    Returns dict with counts: available, not_available, unknown (network error), non_retryable.
    Also returns the availability status of the oldest retryable event.
    """
    event_ids = database.select_not_uploaded_yet_retryable()
    non_retryable = len(database.select_not_uploaded_yet_hard())
    if not event_ids:
        return {"available": 0, "not_available": 0, "unknown": 0, "non_retryable": non_retryable, "oldest_available": None}

    available = 0
    not_available = 0
    unknown = 0
    oldest_available = None

    # Check the oldest event first (it's the one we display)
    for i, event_id in enumerate(event_ids):
        status = _check_clip_availability(event_id)
        if status is True:
            available += 1
            if i == 0:  # oldest event
                oldest_available = True
        elif status is False:
            not_available += 1
            if i == 0:
                oldest_available = False
        else:  # None = network error
            unknown += 1
            if i == 0:
                oldest_available = None

    return {
        "available": available,
        "not_available": not_available,
        "unknown": unknown,
        "non_retryable": non_retryable,
        "oldest_available": oldest_available,
    }


def _get_frigate_reachability_status():
    """
    Return Frigate reachability status string based on _frigate_unreachable_since.
    """
    if _frigate_unreachable_since is None:
        return "reachable"
    else:
        downtime = _format_duration((datetime.now() - _frigate_unreachable_since).total_seconds())
        return f"unreachable for {downtime}"


def daily_health_report(scheduler):
    """
    Sends a daily status report to Mattermost.
    Determines OK / WARNING / CRITICAL based on pending event age and upload activity.
    """
    logging.debug("Generating daily health report...")
    try:
        stats = database.get_health_stats()
    except Exception as e:
        logging.error(f"Failed to collect health stats: {e}")
        send_mattermost_notification(
            title=":rotating_light: CRITICAL: Health report failed",
            text=f"Could not read stats from the database.\n\n**Error:** `{e}`",
            color="#d50000",
        )
        return

    # Collect additional metrics
    subsystem = _get_subsystem_status(scheduler)
    uptime = _get_uptime()
    last_upload = _get_last_successful_upload()
    db_size = _get_db_size()
    frigate_status = _get_frigate_reachability_status()
    clip_stats = _get_clip_availability_stats()

    # Determine severity
    is_critical = stats["pending_gt_3d"] > 0 or (
        stats["uploaded_last_24h"] == 0 and stats.get("pending_retryable", 0) > 0
    )
    is_warning = (not is_critical) and (
        stats["pending_2d_3d"] > 0 or stats["pending_1d_2d"] > 10 or not subsystem["mqtt"] or not subsystem["scheduler"] or not subsystem["db"]
    )

    if is_critical:
        title = ":rotating_light: CRITICAL – Frigate Uploader"
        color = "#d50000"
        headline = "**There are events that haven't been uploaded for more than 3 days, or nothing was uploaded in the last 24h at all.**"
    elif is_warning:
        title = ":warning: Warning – Frigate Uploader"
        color = "#ffae42"
        headline = "Pending uploads or a subsystem need attention; see details below."
    else:
        title = ":white_check_mark: Frigate Uploader – all good"
        color = "#36a64f"
        headline = "Daily report: no overdue retryable uploads. Confirmed source-unavailable records are listed separately below."

    oldest = (
        f"`{stats['oldest_pending_event_id']}` (**{stats['oldest_pending_age_days']} days** old)"
        if stats["oldest_pending_event_id"]
        else "_none_"
    )

    # Add clip availability status for oldest event
    if stats["oldest_pending_event_id"] and clip_stats.get("oldest_available") is not None:
        if clip_stats["oldest_available"]:
            oldest += " — **clip available**"
        else:
            oldest += " — **clip no longer available on Frigate**"
    elif stats["oldest_pending_event_id"] and clip_stats.get("oldest_available") is None:
        oldest += " — **availability check failed (network error)**"

    # Subsystem status indicators
    db_icon = ":white_check_mark:" if subsystem["db"] else ":x:"
    scheduler_icon = ":white_check_mark:" if subsystem["scheduler"] else ":x:"
    mqtt_icon = ":white_check_mark:" if subsystem["mqtt"] else ":x:"
    frigate_icon = ":white_check_mark:" if frigate_status == "reachable" else ":x:"

    text = (
        f"{headline}\n\n"
        f"| Metric | Value |\n"
        f"|---|---|\n"
        f"| Uploaded last 24h | **{stats['uploaded_last_24h']}** |\n"
        f"| Pending total | **{stats['pending_total']}** |\n"
        f"| thereof retryable (action required) | **{stats.get('pending_retryable', 0)}** |\n"
        f"| thereof confirmed source unavailable | **{stats.get('pending_non_retryable', 0)}** |\n"
        f"| thereof under 1 day (normal) | {stats['pending_lt_1d']} |\n"
        f"| thereof 1–2 days | {stats['pending_1d_2d']} |\n"
        f"| thereof 2–3 days | {stats['pending_2d_3d']} |\n"
        f"| thereof **over 3 days** | **{stats['pending_gt_3d']}** |\n"
        f"| Oldest retryable event | {oldest} |\n"
        f"| Uploaded events retained in local DB | {stats['total_uploaded']} |\n"
    )

    # Add clip availability statistics
    if stats.get("pending_retryable", 0) > 0:
        text += "\n**Clip Availability (retryable events):**\n"
        text += f"- Clips available on Frigate: **{clip_stats['available']}** (action required)\n"
        text += f"- Clips no longer available: **{clip_stats['not_available']}** (cannot upload)\n"
        if clip_stats['unknown'] > 0:
            text += f"- Availability check failed: **{clip_stats['unknown']}** (network error)\n"

    # Add subsystem status section
    text += "\n**Subsystem Status:**\n"
    text += f"- {db_icon} Database\n"
    text += f"- {scheduler_icon} Scheduler\n"
    text += f"- {mqtt_icon} MQTT ({subsystem['mqtt_status']})\n"
    text += f"- {frigate_icon} Frigate ({frigate_status})\n"

    # Add additional metrics
    text += "\n**Additional Metrics:**\n"
    text += f"- Uptime: {uptime}\n"
    text += f"- Last successful upload: {last_upload or '_never_'}\n"
    text += f"- DB size: {db_size}\n"

    # Surface a breakdown of failure categories among pending events so the
    # user can see at a glance whether the backlog is dominated by network
    # issues, Frigate clip-assembly bugs, Drive quota errors, etc.
    if stats.get("pending_error_kinds"):
        text += "\n**Pending errors by category:**\n"
        for kind, count in stats["pending_error_kinds"]:
            text += f"- `{kind}`: **{count}**\n"

    if is_critical:
        text += (
            "\n**Recommended actions:**\n"
            "- Check container logs: `docker logs frigate-gdrive-instant-uploader --tail 200`\n"
            "- Check DB state: `SELECT date(created), COUNT(*) FROM events WHERE uploaded=0 GROUP BY 1;`\n"
            "- Verify Frigate reachability & internet connectivity\n"
        )

    is_ok = not is_critical and not is_warning
    if is_ok and HEALTH_REPORT_ONLY_ON_ISSUES:
        logging.info(
            "Daily health report: OK (suppressed Mattermost notification — "
            "HEALTH_REPORT_ONLY_ON_ISSUES=true). "
            f"Stats: uploaded_last_24h={stats['uploaded_last_24h']}, "
            f"pending_total={stats['pending_total']}."
        )
        return

    send_mattermost_notification(title=title, text=text, color=color)
    logging.info(f"Health report sent: {title}")


def internet(host="8.8.8.8", port=53, timeout=3):
    """
    Quick connectivity check: TCP-connect to a well-known DNS endpoint.
    Returns True if reachable within `timeout` seconds, else False.

    Uses socket.create_connection with a per-call timeout so it does NOT mutate
    the process-wide socket default timeout (unlike socket.setdefaulttimeout()).
    The socket is closed deterministically via the context manager.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError as ex:
        logging.debug(f"Internet check failed: {ex}")
        return False


def main():
    """
    Main function to initialize services and process events.
    """
    validate_config()

    logging.debug("Initializing database...")
    init_db_and_run_migrations()

    global _upload_worker
    _upload_worker = threading.Thread(target=upload_worker, name='upload-worker', daemon=True)
    _upload_worker.start()
    mqtt_thread = threading.Thread(target=mqtt_handler)
    mqtt_thread.daemon = True
    mqtt_thread.start()

    scheduler = BackgroundScheduler()
    # Run interval jobs shortly after startup so we don't wait a full interval
    # before the first execution (especially important after container restarts).
    # 90s gives MQTT/Google auth a moment to settle first.
    initial_run = datetime.now() + timedelta(seconds=90)
    scheduler.add_job(run_every_x_minutes, 'interval', minutes=10, next_run_time=initial_run)
    scheduler.add_job(lambda: cleanup_old_files_on_drive(service), 'interval', days=1, next_run_time=initial_run)
    health_hour, health_minute = parse_health_report_time(HEALTH_REPORT_TIME)
    scheduler.add_job(lambda: daily_health_report(scheduler), 'cron', hour=health_hour, minute=health_minute)
    scheduler.start()
    logging.info(
        f"Scheduler started. First interval job run at {initial_run.strftime('%H:%M:%S')}. "
        f"Daily health report scheduled at {health_hour:02d}:{health_minute:02d}."
    )

    # Start the HTTP healthcheck server. Runs in its own daemon thread so it
    # never blocks the main loop. The server is intentionally started AFTER
    # the scheduler/MQTT subsystems so the very first /health probe sees a
    # reasonably initialised process. Failure to bind (port in use, perms)
    # is logged but does not crash the app — the rest of the service is
    # functional without the healthcheck endpoint.
    health_state = HealthState(
        db_path=database.DB_PATH,
        scheduler=scheduler,
        mqtt_is_connected=_mqtt_is_connected,
        worker_is_alive=lambda: bool(_upload_worker and _upload_worker.is_alive()),
        status_token=HEALTHCHECK_TOKEN or None,
    )
    health_server = None
    try:
        health_server, _ = start_healthcheck_server(
            state=health_state,
            host=HEALTHCHECK_BIND,
            port=HEALTHCHECK_PORT,
        )
        # If the server is reachable from outside the container and no token
        # is set, remind the user — /status would be public in that case.
        if HEALTHCHECK_BIND != "127.0.0.1" and not HEALTHCHECK_TOKEN:
            logging.warning(
                "Healthcheck server is listening on all interfaces (0.0.0.0) "
                "and no HEALTHCHECK_TOKEN is set. /status will be publicly "
                "accessible if you expose this port. Set HEALTHCHECK_TOKEN "
                "in your .env to require authentication for /status."
            )
    except OSError as e:
        logging.error(
            f"Failed to start healthcheck server on {HEALTHCHECK_BIND}:{HEALTHCHECK_PORT}: {e}. "
            f"Continuing without healthcheck endpoint."
        )

    try:
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        # Flag that we're shutting down so any in-flight healthcheck probes
        # immediately report 503 instead of bouncing the orchestrator into
        # a restart loop while we drain.
        health_state.shutting_down.set()
        if health_server:
            health_server.shutdown()
        scheduler.shutdown()


if __name__ == "__main__":
    main()
