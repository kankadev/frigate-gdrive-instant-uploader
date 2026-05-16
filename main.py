import json
import logging
import os
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
from src.frigate_api import fetch_all_events, fetch_event, EventNotFoundError, ClipNotAvailableError
from src.google_drive import cleanup_old_files_on_drive, service
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


def handle_single_event(event_data, skip_wait=False):
    """
    Handles a single event. Uploads the video to Google Drive if available and updates the database.
    :param event_data:
    :param skip_wait: If True, skip the 5-second wait (useful for retrying old events).
    :return:
    """
    event_id = event_data['id']
    end_time = event_data['end_time']
    has_clip = event_data['has_clip']

    start_time = event_data['start_time']
    recorded_at = format_event_recorded_at(start_time)

    if not database.is_event_exists(event_id):
        database.insert_event(event_id, start_time)

    if end_time is not None and has_clip is True and internet() is True:
        if database.select_retry(event_id) == 0:
            logging.debug(f"Event {event_id} is marked as non-retriable. Skipping upload.")
        else:
            uploaded_status = database.select_event_uploaded(event_id)
            if uploaded_status == 0 or uploaded_status is None:
                # Wait a few seconds to give Frigate time to finish writing the file to disk
                if not skip_wait:
                    logging.debug("Waiting 5 seconds for Frigate to finalize the clip...")
                    time.sleep(5)
                logging.debug(f"Uploading video {event_id} (recorded {recorded_at}) to Google Drive...")
                try:
                    success = google_drive.upload_to_google_drive(event_data, FRIGATE_URL)
                except ClipNotAvailableError as e:
                    logging.warning(
                        f"Clip for event {event_id} (recorded {recorded_at}) no longer available on Frigate. "
                        f"Removing from database. Reason: {e}"
                    )
                    database.delete_event(event_id)
                    return
                if success:
                    logging.info(f"Video {event_id} (recorded {recorded_at}) successfully uploaded.")
                    database.update_event(event_id, 1)
                else:
                    database.update_event(event_id, 0)
                    tries = database.select_tries(event_id)
                    msg = (
                        f"Failed to upload video {event_id} (recorded {recorded_at}). "
                        f"Attempt {tries}/{MAX_RETRY_ATTEMPTS}."
                    )
                    # Notification policy: send AT MOST two Mattermost messages per event.
                    # - tries < 80% of MAX_RETRY_ATTEMPTS: WARNING (file log only)
                    # - tries == 80% threshold: single ERROR (early heads-up)
                    # - tries == MAX_RETRY_ATTEMPTS: single ERROR (final give-up)
                    warning_threshold = max(1, int(MAX_RETRY_ATTEMPTS * 0.8))
                    if tries >= MAX_RETRY_ATTEMPTS:
                        logging.error(
                            f"Giving up on event {event_id} (recorded {recorded_at}) "
                            f"after {tries} failed attempts. Marked as non-retriable. "
                            f"No further upload attempts will be made for this event."
                        )
                        database.update_event_retry(event_id, 0)
                    elif tries == warning_threshold:
                        logging.error(
                            f"{msg} Heads-up: will give up at {MAX_RETRY_ATTEMPTS} attempts."
                        )
                    else:
                        logging.warning(msg)
            else:
                logging.debug(f"Event {event_id} already uploaded. Skipping...")


def handle_all_events():
    latest_start_time = database.get_latest_event_start_time()
    logging.debug(f"Fetching all events from Frigate since {latest_start_time}...")
    all_events = fetch_all_events(FRIGATE_URL, after=latest_start_time, batch_size=100)

    if all_events is None:
        # This indicates a connection error after retries
        logging.error("Failed to fetch events from Frigate after multiple retries.")
    elif not all_events:
        # This is the normal case where there are no new events
        logging.debug("No new events to process.")
    else:
        # Process the fetched events
        logging.debug(f"Received {len(all_events)} events")
        i = 1
        for event in all_events:
            logging.debug(f"Handling event #{i}: {event['id']} in handle_all_events")
            handle_single_event(event)
            i = i + 1


# MQTT Reconnect settings
FIRST_RECONNECT_DELAY = 1
RECONNECT_RATE = 2
MAX_RECONNECT_COUNT = 12
MAX_RECONNECT_DELAY = 60


def on_disconnect(client, userdata, rc):
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


def mqtt_handler():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.username_pw_set(MQTT_USER, MQTT_PASSWORD)
    client.on_connect = on_connect
    client.on_message = on_message
    client.on_disconnect = on_disconnect
    client.connect(MQTT_BROKER_ADDRESS, MQTT_PORT, 180)
    client.loop_forever()


def handle_not_uploaded_events():
    event_ids = database.select_not_uploaded_yet()
    if event_ids:
        logging.debug(f"Found {len(event_ids)} not uploaded events to retry")
        for event_id in event_ids:
            try:
                event_data = fetch_event(FRIGATE_URL, event_id)
                handle_single_event(event_data, skip_wait=True)
            except EventNotFoundError:
                logging.warning(f"Event {event_id} no longer exists on Frigate. Removing from database.")
                database.delete_event(event_id)
            except Exception as e:
                logging.error(f"Failed to fetch event {event_id} from Frigate for retry: {e}")
                database.update_event(event_id, 0)
    else:
        logging.debug("No not uploaded events found to retry")


def run_every_x_minutes():
    logging.debug("Handling all events and cleaning up old events...")
    # Clean up first so stale entries are not loaded into the retry queue.
    database.cleanup_old_events()
    handle_all_events()
    handle_not_uploaded_events()


def daily_health_report():
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
            title=":rotating_light: KRITISCH: Health-Report fehlgeschlagen",
            text=f"Konnte keine Statistik aus der Datenbank lesen.\n\n**Fehler:** `{e}`",
            color="#d50000",
        )
        return

    # Determine severity
    is_critical = stats["pending_gt_3d"] > 0 or (
        stats["uploaded_last_24h"] == 0 and stats["pending_total"] > 0
    )
    is_warning = (not is_critical) and (
        stats["pending_2d_3d"] > 0 or stats["pending_1d_2d"] > 10
    )

    if is_critical:
        title = ":rotating_light: KRITISCH – Frigate Uploader"
        color = "#d50000"
        headline = "**Es gibt Events, die seit über 3 Tagen nicht hochgeladen wurden, oder es lief in den letzten 24h gar nichts.**"
    elif is_warning:
        title = ":warning: Warnung – Frigate Uploader"
        color = "#ffae42"
        headline = "Es gibt Events, die seit 1–3 Tagen warten. Bitte beobachten."
    else:
        title = ":white_check_mark: Frigate Uploader – alles in Ordnung"
        color = "#36a64f"
        headline = "Tagesreport: alle Uploads laufen normal."

    oldest = (
        f"`{stats['oldest_pending_event_id']}` (**{stats['oldest_pending_age_days']} Tage** alt)"
        if stats["oldest_pending_event_id"]
        else "_keine_"
    )

    text = (
        f"{headline}\n\n"
        f"| Metrik | Wert |\n"
        f"|---|---|\n"
        f"| Hochgeladen letzte 24h | **{stats['uploaded_last_24h']}** |\n"
        f"| Wartend gesamt | **{stats['pending_total']}** |\n"
        f"| davon unter 1 Tag (normal) | {stats['pending_lt_1d']} |\n"
        f"| davon 1–2 Tage | {stats['pending_1d_2d']} |\n"
        f"| davon 2–3 Tage | {stats['pending_2d_3d']} |\n"
        f"| davon **über 3 Tage** | **{stats['pending_gt_3d']}** |\n"
        f"| Ältestes wartendes Event | {oldest} |\n"
        f"| Insgesamt erfolgreich hochgeladen | {stats['total_uploaded']} |\n"
    )

    if is_critical:
        text += (
            "\n**Empfohlene Aktionen:**\n"
            "- Logs des Containers prüfen: `docker logs frigate-gdrive-instant-uploader --tail 200`\n"
            "- DB-Zustand prüfen: `SELECT date(created), COUNT(*) FROM events WHERE uploaded=0 GROUP BY 1;`\n"
            "- Frigate-Erreichbarkeit & Internet überprüfen\n"
        )

    send_mattermost_notification(title=title, text=text, color=color)
    logging.info(f"Health report sent: {title}")


def run_every_6_hours():
    logging.debug("Handling failed events...")
    failed_events = database.select_not_uploaded_yet_hard()
    if failed_events:
        logging.error(
            f"{len(failed_events)} failed events: {failed_events} ... Please check the logs for more information.")
    else:
        logging.debug("No failed events found.")


def internet(host="8.8.8.8", port=53, timeout=3):
    """
    Host: 8.8.8.8 (google-public-dns-a.google.com)
    OpenPort: 53/tcp
    Service: domain (DNS/TCP)
    """
    try:
        socket.setdefaulttimeout(timeout)
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect((host, port))
        return True
    except socket.error as ex:
        print(ex)
        return False


def main():
    """
    Main function to initialize services and process events.
    """
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
    scheduler.add_job(run_every_6_hours, 'interval', hours=6, next_run_time=initial_run)
    scheduler.add_job(lambda: cleanup_old_files_on_drive(service), 'interval', days=1, next_run_time=initial_run)
    scheduler.add_job(daily_health_report, 'cron', hour=9, minute=0)
    scheduler.start()
    logging.info(f"Scheduler started. First interval job run at {initial_run.strftime('%H:%M:%S')}.")

    try:
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()


if __name__ == "__main__":
    main()
