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

from src import database, google_drive
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


def on_connect(client, userdata, flags, reason_code, properties):
    logging.info(f"MQTT connected with result code {reason_code}")
    client.subscribe(MQTT_TOPIC)


def on_message(client, userdata, msg):
    logging.debug(f"MQTT message received `{msg.payload.decode()}` from topic `{msg.topic}`")
    event = json.loads(msg.payload)
    event_type = event.get('type', None)
    end_time = event.get('after', {}).get('end_time', None)
    has_clip = event.get('after', {}).get('has_clip', False)

    if event_type == 'end' and end_time is not None and has_clip is True:
        event_data = event['after']
        handle_single_event(event_data)
    else:
        logging.debug(f"Received a MQTT message but event type, end_time or has_clip doesn't interest us. Wait for "
                      f"the full message. Skipping...")


def format_event_recorded_at(start_time):
    """Returns a human-readable recording timestamp for an event (using TZ env)."""
    try:
        import pytz
        tz = pytz.timezone(os.getenv('TZ', 'UTC'))
        return datetime.fromtimestamp(start_time, pytz.utc).astimezone(tz).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return f"start_time={start_time}"


def get_max_retries_for_event(event_data):
    """
    Return max retry attempts based on event duration.
    Long events are more likely to hit systematic Frigate clip assembly bugs
    that don't resolve with more retries, so we give up faster.
    """
    end_time = event_data.get('end_time') or 0
    start_time = event_data.get('start_time') or 0
    duration_sec = end_time - start_time
    if duration_sec > 3 * 3600:       # > 3 hours
        return 3   # 3 retries × 10 min = 30 min total wait
    elif duration_sec > 1 * 3600:     # > 1 hour
        return 10  # 10 retries × 10 min = 100 min total wait
    return MAX_RETRY_ATTEMPTS


def handle_single_event(event_data, skip_wait=False, online=None):
    """
    Handles a single event. Uploads the video to Google Drive if available and updates the database.
    :param event_data:
    :param skip_wait: If True, skip the 5-second wait (useful for retrying old events).
    :param online: Tri-state. If True/False, skip the per-event internet() check and use
        the provided value (callers in batch jobs should pre-check once). If None, fall
        back to calling internet() inline (used by the MQTT single-event path).
    :return: bool. False if an upload was attempted in this call and failed for
        potentially-network reasons (caller may want to re-check connectivity).
        True otherwise (skipped, succeeded, hard-fail like ClipNotAvailable, etc.).
    """
    event_id = event_data['id']
    end_time = event_data['end_time']
    has_clip = event_data['has_clip']

    start_time = event_data['start_time']
    recorded_at = format_event_recorded_at(start_time)
    event_max_retries = get_max_retries_for_event(event_data)
    duration_sec = int((end_time or 0) - start_time)

    if not database.is_event_exists(event_id):
        database.insert_event(event_id, start_time)

    # Duration filter: skip events that exceed SKIP_EVENTS_LONGER_THAN_SECONDS.
    # Checked here so it applies on both the MQTT path and the retry-loop path,
    # and before any clip-roundtrip to Frigate (the `/clip.mp4` endpoint can
    # hang for minutes assembling long clips — exactly the case we want to skip).
    if (
        SKIP_EVENTS_LONGER_THAN_SECONDS > 0
        and end_time is not None
        and (end_time - start_time) > SKIP_EVENTS_LONGER_THAN_SECONDS
    ):
        if database.select_retry(event_id) != 0:
            duration_sec_actual = int(end_time - start_time)
            logging.warning(
                f"Skipping event {event_id} (recorded {recorded_at}): duration "
                f"{duration_sec_actual}s exceeds SKIP_EVENTS_LONGER_THAN_SECONDS="
                f"{SKIP_EVENTS_LONGER_THAN_SECONDS}s. Marking as non-retriable."
            )
            database.update_event_retry(
                event_id, 0, last_error_kind=google_drive.ERR_EVENT_TOO_LONG
            )
        else:
            logging.debug(
                f"Event {event_id} already marked non-retriable (duration filter). Skipping."
            )
        return True

    if online is None:
        online = internet()

    if end_time is not None and has_clip is True and online is True:
        if database.select_retry(event_id) == 0:
            logging.debug(f"Event {event_id} is marked as non-retriable. Skipping upload.")
        else:
            uploaded_status = database.select_event_uploaded(event_id)
            if uploaded_status == 0 or uploaded_status is None:
                # Wait a few seconds to give Frigate time to finish writing the file to disk
                if not skip_wait:
                    logging.debug("Waiting 5 seconds for Frigate to finalize the clip...")
                    time.sleep(5)
                logging.info(f"Starting upload for event {event_id} (recorded {recorded_at})...")
                try:
                    success, error_kind = google_drive.upload_to_google_drive(event_data, FRIGATE_URL)
                except ClipNotAvailableError as e:
                    logging.warning(
                        f"Clip for event {event_id} (recorded {recorded_at}) no longer available on Frigate. "
                        f"Removing from database. Reason: {e}"
                    )
                    database.delete_event(event_id)
                    return True
                except ClipTooLargeError as e:
                    logging.warning(
                        f"Skipping clip for event {event_id} (recorded {recorded_at}). "
                        f"Reason: {e} Marking as non-retriable."
                    )
                    database.update_event_retry(event_id, 0, last_error_kind=google_drive.ERR_CLIP_TOO_LARGE)
                    return True
                if success:
                    logging.info(f"Video {event_id} (recorded {recorded_at}) successfully uploaded.")
                    database.update_event(event_id, 1)
                else:
                    database.update_event(event_id, 0, last_error_kind=error_kind)
                    tries = database.select_tries(event_id)
                    msg = (
                        f"Failed to upload video {event_id} (recorded {recorded_at}). "
                        f"Attempt {tries}/{event_max_retries}."
                    )
                    # Notification policy: send AT MOST one Mattermost message per event.
                    # - tries < 80% of event_max_retries: WARNING (file log only)
                    # - tries == 80% threshold: ERROR heads-up, but ONLY when it gives
                    #   meaningful advance warning (>= 2 attempts before give-up). For
                    #   events with few retries (e.g. long events get only 3) the heads-up
                    #   would fire back-to-back with the give-up, so it's suppressed.
                    # - tries >= event_max_retries: file-log only WARNING + the rich
                    #   give-up card below. The card IS the Mattermost notification, so we
                    #   deliberately avoid an extra plain ERROR line going to Mattermost.
                    warning_threshold = max(1, int(event_max_retries * 0.8))
                    heads_up_has_lead_time = (event_max_retries - warning_threshold) >= 2
                    if tries >= event_max_retries:
                        logging.warning(
                            f"Giving up on event {event_id} (recorded {recorded_at}) "
                            f"after {tries} failed attempts. Marked as non-retriable. "
                            f"No further upload attempts will be made for this event."
                        )
                        # Preserve the most recent error category when permanently
                        # marking the event non-retriable.
                        database.update_event_retry(event_id, 0, last_error_kind=error_kind)

                        # Notify Mattermost with details so the user can grab the clip manually
                        if duration_sec >= 3600:
                            duration_str = f"{duration_sec // 3600}h {duration_sec % 3600 // 60}m {duration_sec % 60}s"
                        elif duration_sec >= 60:
                            duration_str = f"{duration_sec // 60}m {duration_sec % 60}s"
                        else:
                            duration_str = f"{duration_sec}s"

                        camera = event_data.get('camera', 'unknown')
                        label = event_data.get('label', 'unknown')
                        clip_url = f"{FRIGATE_URL}/api/events/{event_id}/clip.mp4"
                        snapshot_url = f"{FRIGATE_URL}/api/events/{event_id}/snapshot.jpg"

                        mm_text = (
                            f"| Metric | Value |\n"
                            f"|---|---|\n"
                            f"| **Event ID** | `{event_id}` |\n"
                            f"| **Camera** | {camera} |\n"
                            f"| **Label** | {label} |\n"
                            f"| **Recorded** | {recorded_at} |\n"
                            f"| **Duration** | {duration_str} |\n"
                            f"| **Failed attempts** | {tries} |\n"
                            f"| **Clip URL** | [{clip_url}]({clip_url}) |\n"
                            f"| **Snapshot URL** | [{snapshot_url}]({snapshot_url}) |\n\n"
                            f"This event has been marked as non-retriable. "
                            f"You can try downloading the clip manually before it expires on Frigate."
                        )
                        send_mattermost_notification(
                            title=":warning: Upload permanently failed — manual action required",
                            text=mm_text,
                            color="#ffae42"
                        )
                    elif tries == warning_threshold and heads_up_has_lead_time:
                        logging.error(
                            f"{msg} Heads-up: will give up at {event_max_retries} attempts."
                        )
                    else:
                        logging.warning(msg)
                    # Upload was attempted and failed — possibly network related.
                    # Signal caller so it can re-check connectivity before continuing.
                    return False
            else:
                logging.debug(f"Event {event_id} already uploaded. Skipping...")
    return True


# --- Edge-triggered Frigate-reachability notifications -----------------------
# Module-level state to avoid spamming Mattermost while Frigate stays down.
# A notification is sent ONCE when Frigate transitions reachable -> unreachable
# and ONCE when it recovers. While the state is unchanged, no notification.
_frigate_unreachable_since = None  # datetime when the outage was first detected

# Program start time for uptime tracking
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
    logging.info("=== handle_all_events started ===")

    # One internet check at job start instead of one per event. Uploads will fail
    # naturally if connectivity drops mid-loop; we re-check after each failure below.
    online = internet()
    if not online:
        logging.warning("No internet connectivity at handle_all_events start. Skipping job.")
        logging.info("=== handle_all_events completed (skipped, offline) ===")
        return

    # One Frigate reachability check at job start. fetch_all_events would
    # eventually return None on its own, but only after several long retries.
    # Skipping early keeps logs clean and the job slot free.
    if not check_frigate_reachable(FRIGATE_URL):
        logging.warning("Frigate not reachable at handle_all_events start. Skipping job.")
        _notify_frigate_unreachable_once()
        logging.info("=== handle_all_events completed (skipped, Frigate unreachable) ===")
        return
    # Frigate is reachable; if we previously notified about an outage, send recovery.
    _notify_frigate_recovered_once()

    latest_start_time = database.get_latest_event_start_time()
    logging.debug(f"Fetching all events from Frigate since {latest_start_time}...")
    all_events = fetch_all_events(FRIGATE_URL, after=latest_start_time, batch_size=100)

    if all_events is None:
        # This indicates a connection error after retries
        logging.error("Failed to fetch events from Frigate after multiple retries.")
    elif not all_events:
        # This is the normal case where there are no new events
        logging.info("No new events to process from Frigate API.")
    else:
        # Process the fetched events
        logging.info(f"Received {len(all_events)} new events from Frigate API.")
        i = 1
        for event in all_events:
            logging.debug(f"Handling event #{i}: {event['id']} in handle_all_events")
            ok = handle_single_event(event, online=True)
            if ok is False and not internet():
                logging.warning(
                    f"Lost internet connectivity after event {event.get('id')}. "
                    f"Aborting handle_all_events loop after {i} of {len(all_events)} events."
                )
                break
            i = i + 1
        logging.info(f"=== handle_all_events completed. Processed {i - 1} new events. ===")


# MQTT Reconnect settings
FIRST_RECONNECT_DELAY = 1
RECONNECT_RATE = 2
MAX_RECONNECT_COUNT = 12
MAX_RECONNECT_DELAY = 60


def on_disconnect(client, userdata, disconnect_flags, rc, properties):
    logging.info("MQTT disconnected with result code: %s", rc)
    reconnect_count, reconnect_delay = 0, FIRST_RECONNECT_DELAY
    while reconnect_count < MAX_RECONNECT_COUNT:
        logging.info("Reconnecting in %d seconds...", reconnect_delay)
        time.sleep(reconnect_delay)

        try:
            client.reconnect()
            logging.info("Reconnected successfully!")
            return
        except Exception as err:
            logging.error("%s. Reconnect failed. Retrying...", err)

        reconnect_delay *= RECONNECT_RATE
        reconnect_delay = min(reconnect_delay, MAX_RECONNECT_DELAY)
        reconnect_count += 1
    logging.info("Reconnect failed after %s attempts. Exiting...", reconnect_count)


def init_db_and_run_migrations():
    database.init_db()
    database.run_migrations()


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
    _mqtt_client = client
    client.connect(MQTT_BROKER_ADDRESS, MQTT_PORT, 180)  # 180s keepalive → disconnect detected in ~270s
    client.loop_forever()


def handle_not_uploaded_events():
    logging.info("=== handle_not_uploaded_events started ===")

    # One internet check at job start. Saves up to (event_count × 3s) timeouts
    # in case of a full outage. The loop re-checks on individual failures below
    # to handle the rare race where connectivity drops mid-job.
    if not internet():
        logging.warning("No internet connectivity at handle_not_uploaded_events start. Skipping retry loop.")
        logging.info("=== handle_not_uploaded_events completed (skipped, offline) ===")
        return

    # One Frigate reachability check at job start. Fails fast (10s) if the
    # Frigate host is down (LXC restart, network outage, ...). The per-event
    # check inside the loop still protects against transient busy moments.
    if not check_frigate_reachable(FRIGATE_URL):
        logging.warning("Frigate not reachable at handle_not_uploaded_events start. Skipping retry loop.")
        _notify_frigate_unreachable_once()
        logging.info("=== handle_not_uploaded_events completed (skipped, Frigate unreachable) ===")
        return
    # Frigate is reachable; if we previously notified about an outage, send recovery.
    _notify_frigate_recovered_once()

    event_ids = database.select_not_uploaded_yet()
    if not event_ids:
        logging.info("No pending events to retry.")
        logging.info("=== handle_not_uploaded_events completed ===")
        return

    logging.info(f"Found {len(event_ids)} pending events to retry (oldest first).")
    consecutive_timeouts = 0
    processed = 0
    for event_id in event_ids:
        # Check reachability before every individual event so one slow/busy
        # moment on Frigate does not abort the entire retry queue.
        if not check_frigate_reachable(FRIGATE_URL):
            consecutive_timeouts += 1
            if consecutive_timeouts >= 3:
                logging.warning("Frigate unreachable for 3 consecutive events. Aborting retry loop.")
                break
            logging.debug(f"Frigate not reachable for event {event_id}, skipping...")
            continue
        consecutive_timeouts = 0

        logging.info(f"Retrying event {event_id}...")
        try:
            event_data = fetch_event(FRIGATE_URL, event_id)
            ok = handle_single_event(event_data, skip_wait=True, online=True)
        except EventNotFoundError:
            logging.warning(f"Event {event_id} no longer exists on Frigate. Removing from database.")
            database.delete_event(event_id)
            processed += 1
            continue
        except FrigateUnreachableError:
            logging.warning(f"Frigate became unreachable during retry for event {event_id}. Skipping to next.")
            continue

        processed += 1

        # Race-condition safety net: an upload failed in this iteration. Verify
        # internet is still up; if not, abort instead of burning through the
        # entire backlog with guaranteed-failing uploads.
        if ok is False and not internet():
            logging.warning(
                f"Lost internet connectivity after event {event_id}. "
                f"Aborting retry loop after {processed} of {len(event_ids)} events."
            )
            break
    logging.info(f"=== handle_not_uploaded_events completed. Retried {processed} of {len(event_ids)} events. ===")


def run_every_x_minutes():
    logging.info("=== Periodic job started ===")
    logging.info("Step 1/3: Cleaning up old events from database...")
    database.cleanup_old_events()
    logging.info("Step 2/3: Retrying old pending events (oldest first)...")
    handle_not_uploaded_events()
    logging.info("Step 3/3: Fetching and processing new events from Frigate API...")
    handle_all_events()
    logging.info("=== Periodic job completed ===")


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
        clip_url = f"{FRIGATE_URL}/api/events/{event_id}/clip.mp4"
        response = requests.head(clip_url, timeout=10)
        if response.status_code in (200, 206):
            return True
        elif response.status_code in (400, 404):
            return False
        else:
            logging.debug(f"Unexpected status {response.status_code} for clip {event_id}")
            return None
    except (requests.RequestException, Timeout):
        logging.debug(f"Network error checking clip {event_id}")
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
        stats["uploaded_last_24h"] == 0 and stats["pending_total"] > 0
    )
    is_warning = (not is_critical) and (
        stats["pending_2d_3d"] > 0 or stats["pending_1d_2d"] > 10
    )

    if is_critical:
        title = ":rotating_light: CRITICAL – Frigate Uploader"
        color = "#d50000"
        headline = "**There are events that haven't been uploaded for more than 3 days, or nothing was uploaded in the last 24h at all.**"
    elif is_warning:
        title = ":warning: Warning – Frigate Uploader"
        color = "#ffae42"
        headline = "There are events that have been pending for 1–3 days. Please keep an eye on it."
    else:
        title = ":white_check_mark: Frigate Uploader – all good"
        color = "#36a64f"
        headline = "Daily report: all uploads are running normally."

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
        f"| thereof non-retriable (gave up) | **{stats.get('pending_non_retryable', 0)}** |\n"
        f"| thereof under 1 day (normal) | {stats['pending_lt_1d']} |\n"
        f"| thereof 1–2 days | {stats['pending_1d_2d']} |\n"
        f"| thereof 2–3 days | {stats['pending_2d_3d']} |\n"
        f"| thereof **over 3 days** | **{stats['pending_gt_3d']}** |\n"
        f"| Oldest retryable event | {oldest} |\n"
        f"| Total uploaded ever | {stats['total_uploaded']} |\n"
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
