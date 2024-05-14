import json
import logging
import os
import sys
import threading
import time
from logging.handlers import RotatingFileHandler

import paho.mqtt.client as mqtt
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from src import database, google_drive
from src.frigate_api import fetch_all_events
from src.mattermost_handler import MattermostHandler

load_dotenv()

LOGGING_LEVEL = os.getenv('LOGGING_LEVEL', 'DEBUG').upper()
NUMERIC_LEVEL = getattr(logging, LOGGING_LEVEL, None)
if not isinstance(NUMERIC_LEVEL, int):
    raise ValueError(f'invalid logging level: {LOGGING_LEVEL}')

FRIGATE_URL = os.getenv('FRIGATE_URL')
GOOGLE_CREDENTIALS_JSON_FILE = os.getenv('GOOGLE_CREDENTIALS_JSON_FILE', 'credentials/google_drive_credentials.json')
GOOGLE_TOKEN_FILE = os.getenv('GOOGLE_TOKEN_FILE', 'credentials/token.json')
MQTT_BROKER_ADDRESS = os.getenv('MQTT_BROKER_ADDRESS')
MQTT_PORT = int(os.getenv('MQTT_PORT'))
MQTT_TOPIC = os.getenv('MQTT_TOPIC')
MQTT_USER = os.getenv('MQTT_USER')
MQTT_PASSWORD = os.getenv('MQTT_PASSWORD')
MATTERMOST_WEBHOOK_URL = os.getenv('MATTERMOST_WEBHOOK_URL', None)

SCOPES = ['https://www.googleapis.com/auth/drive']

log_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
log_file = 'logs/app.log'
rotating_handler = RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=5)
rotating_handler.setFormatter(log_formatter)

if MATTERMOST_WEBHOOK_URL is None:
    logging.warning("MATTERMOST_WEBHOOK_URL is not set. Mattermost notifications will not be sent.")
    logging.basicConfig(level=NUMERIC_LEVEL, format='%(asctime)s - %(levelname)s - %(message)s',
                        handlers=[
                            rotating_handler,
                            logging.StreamHandler()
                        ])
else:
    mattermost_handler = MattermostHandler(MATTERMOST_WEBHOOK_URL)
    mattermost_handler.setFormatter(log_formatter)
    mattermost_handler.setLevel(logging.ERROR)
    logging.basicConfig(level=NUMERIC_LEVEL, format='%(asctime)s - %(levelname)s - %(message)s',
                        handlers=[
                            rotating_handler,
                            mattermost_handler,
                            logging.StreamHandler()
                        ])


def create_service():
    """Create and return a Google Drive service client."""
    logging.debug("Initializing Google Drive Service...")
    creds = None
    try:
        if os.path.exists(GOOGLE_TOKEN_FILE):
            creds = Credentials.from_authorized_user_file(GOOGLE_TOKEN_FILE, SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not os.path.exists(GOOGLE_CREDENTIALS_JSON_FILE):
                    logging.error(f"Google credentials file not found: {GOOGLE_CREDENTIALS_JSON_FILE}")
                    raise FileNotFoundError(f"Google credentials file not found: {GOOGLE_CREDENTIALS_JSON_FILE}")
                flow = InstalledAppFlow.from_client_secrets_file(GOOGLE_CREDENTIALS_JSON_FILE, SCOPES)
                creds = flow.run_local_server(port=0)
            with open(GOOGLE_TOKEN_FILE, 'w') as token:
                token.write(creds.to_json())
        return build('drive', 'v3', credentials=creds)
    except Exception as e:
        logging.error(f"Failed to create Google Drive service: {e}", exc_info=True)
        sys.exit(1)


def on_connect(client, userdata, flags, reason_code, properties):
    logging.info(f"Connected with result code {reason_code}")
    client.subscribe(MQTT_TOPIC)


def on_message(client, userdata, msg):
    logging.debug(f"Message received `{msg.payload.decode()}` from topic `{msg.topic}`")
    event = json.loads(msg.payload)
    event_type = event.get('type', None)
    end_time = event.get('end_time', None)
    has_clip = event.get('has_clip', False)

    if event_type == 'end' and end_time is not None and has_clip is True:
        event_data = event['after']
        handle_single_event(event_data, userdata)
    else:
        logging.debug(f"Received a MQTT message but event type, end_time or has_clip doesn't interest us. Wait for "
                      f"the full message. Skipping...")


def handle_single_event(event_data, userdata):
    """
    Handles a single event. Uploads the video to Google Drive if available and updates the database.
    :param event_data:
    :param userdata:
    :return:
    """

    event_id = event_data['id']
    event_type = event_data['type']
    end_time = event_data['end_time']
    has_clip = event_data['has_clip']

    if not database.is_event_exists(event_id):
        database.insert_event(event_id)

    if event_type == 'end' and end_time is not None and has_clip is True:
        if database.select_retry(event_id) == 0:
            logging.debug(f"Event {event_id} is marked as non-retriable. Skipping upload.")
        else:
            uploaded_status = database.select_event_uploaded(event_id)
            if uploaded_status == 0 or uploaded_status is None:
                logging.debug(f"Uploading video {event_id} to Google Drive...")
                success = google_drive.upload_to_google_drive(userdata, event_data, FRIGATE_URL)
                if success:
                    logging.info(f"Video {event_id} successfully uploaded.")
                    database.update_event(event_id, 1)
                else:
                    database.update_event(event_id, 0)
                    # to prevent annoying logs / notifications... Notify only after 3 tries
                    if database.select_tries(event_id) >= 3:
                        logging.error(f"Failed to upload video {event_id}.")
            else:
                logging.debug(f"Event {event_id} already uploaded. Skipping...")


def handle_all_events(service):
    logging.debug("Fetching all events from Frigate...")
    all_events = fetch_all_events(FRIGATE_URL, batch_size=100)
    if all_events:
        logging.debug(f"Received {len(all_events)} events")
        i = 1
        for event in all_events:
            logging.debug(f"Handling event #{i}: {event['id']} in handle_all_events")
            handle_single_event(event, service)
            i = i + 1
    else:
        logging.error("Failed to fetch events from Frigate.")


# MQTT Reconnect settings
FIRST_RECONNECT_DELAY = 1
RECONNECT_RATE = 2
MAX_RECONNECT_COUNT = 12
MAX_RECONNECT_DELAY = 60


def on_disconnect(client, userdata, rc):
    logging.info("Disconnected with result code: %s", rc)
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
    client.user_data_set(create_service())
    client.username_pw_set(MQTT_USER, MQTT_PASSWORD)
    client.on_connect = on_connect
    client.on_message = on_message
    client.on_disconnect = on_disconnect
    client.connect(MQTT_BROKER_ADDRESS, MQTT_PORT, 180)
    client.loop_forever()


def run_every_3_minutes():
    logging.debug("Handling all events and cleaning up old events...")
    handle_all_events(create_service())
    database.cleanup_old_events()


def run_every_6_hours():
    logging.debug("Handling failed events...")
    failed_events = database.select_not_uploaded_yet_hard()
    if failed_events:
        logging.error(f"{len(failed_events)} failed events: {failed_events} ... Please check the logs for more information.")
    else:
        logging.debug("No failed events found.")


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
    scheduler.add_job(run_every_3_minutes, 'interval', minutes=3)
    scheduler.add_job(run_every_6_hours, 'interval', hours=6)
    scheduler.start()

    try:
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()


if __name__ == "__main__":
    main()
