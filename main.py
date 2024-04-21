import json
import logging
import os
import random
import sys
import time

import paho.mqtt.client as mqtt
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from src import database, google_drive

load_dotenv()

LOGGING_LEVEL = os.getenv('LOGGING_LEVEL', 'INFO').upper()
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

SCOPES = ['https://www.googleapis.com/auth/drive']

logging.basicConfig(level=NUMERIC_LEVEL, format='%(asctime)s - %(levelname)s - %(message)s',
                    handlers=[
                        logging.FileHandler('logs/app.log'),
                        logging.StreamHandler()
                    ])


def create_service():
    """Create and return a Google Drive service client."""
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
    event_data = json.loads(msg.payload)['after']
    logging.debug(f"Event data: {event_data}")

    if event_data['has_clip'] and event_data['end_time']:
        logging.debug(f"Uploading video {event_data['id']} to Google Drive...")
        success = google_drive.upload_to_google_drive(userdata, event_data, FRIGATE_URL)
        if success:
            logging.debug(f"Video {event_data['id']} successfully uploaded.")
        else:
            logging.error(f"Failed to upload video {event_data['id']}.")
            # TODO: save this event in a separate table to retry later?
    else:
        logging.error(f"No video clip available for this event {event_data['id']}.")


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


def main():
    """Main function to initialize services and process events."""
    logging.info("Initializing Google Drive Service...")
    service = create_service()

    logging.info("Initializing database...")
    database.init_db()

    client_id = f'python-mqtt-{random.randint(0, 1000)}'
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.user_data_set(service)
    client.username_pw_set(MQTT_USER, MQTT_PASSWORD)
    client.on_connect = on_connect
    client.on_message = on_message
    client.on_disconnect = on_disconnect
    client.connect(MQTT_BROKER_ADDRESS, MQTT_PORT, 180)

    client.loop_forever()


if __name__ == "__main__":
    main()
