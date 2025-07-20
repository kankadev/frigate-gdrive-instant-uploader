import logging
import os
import ssl
import tempfile
import threading
import requests
from dotenv import load_dotenv
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload
from datetime import datetime
import pytz
from google.oauth2 import service_account
from googleapiclient.discovery import build

from src import database
from src.frigate_api import generate_video_url

load_dotenv()

UPLOAD_DIR = os.getenv('UPLOAD_DIR')
# Prioritize standard 'TZ' env var, but fall back to 'TIMEZONE' for backward compatibility.
TIMEZONE = os.getenv('TZ', os.getenv('TIMEZONE', 'Europe/Istanbul'))
SERVICE_ACCOUNT_FILE = os.getenv('SERVICE_ACCOUNT_FILE')
GOOGLE_ACCOUNT_TO_IMPERSONATE = os.getenv('GOOGLE_ACCOUNT_TO_IMPERSONATE')

SCOPES = ['https://www.googleapis.com/auth/drive']

if GOOGLE_ACCOUNT_TO_IMPERSONATE:
    credentials = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES, subject=GOOGLE_ACCOUNT_TO_IMPERSONATE)
else:
    credentials = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES)

service = build('drive', 'v3', credentials=credentials)

# Lock to prevent race conditions when creating folders
folder_creation_lock = threading.Lock()


def generate_filename(camera_name, start_time, event_id):
    utc_time = datetime.fromtimestamp(start_time, pytz.utc)
    local_time = utc_time.astimezone(pytz.timezone(TIMEZONE))
    return f"{local_time.strftime('%Y-%m-%d-%H-%M-%S')}__{camera_name}__{event_id}.mp4"


def find_or_create_folder(name, parent_id=None):
    # Use a lock to prevent race conditions where multiple threads try to create the same folder.
    with folder_creation_lock:
        try:
            # More specific query to avoid finding similarly named folders or folders in trash.
            query = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
            if parent_id:
                query += f" and '{parent_id}' in parents"

            results = service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
            folders = results.get('files', [])

            if not folders:
                # Folder does not exist, create it.
                folder_metadata = {
                    'name': name,
                    'mimeType': 'application/vnd.google-apps.folder',
                    'parents': [parent_id] if parent_id else []
                }
                folder = service.files().create(body=folder_metadata, fields='id').execute()
                logging.debug(f"Created folder '{name}' with id {folder.get('id')}")
                return folder.get('id')
            else:
                # Folder already exists, return its ID.
                logging.debug(f"Found existing folder '{name}' with id {folders[0]['id']}")
                return folders[0]['id']

        except HttpError as error:
            logging.error(f"An error occurred while finding or creating folder '{name}': {error}")
            return None


def upload_to_google_drive(event, frigate_url):
    camera_name = event['camera']
    start_time = event['start_time']
    event_id = event['id']
    filename = generate_filename(camera_name, start_time, event_id)
    year, month, day = filename.split("__")[0].split("-")[:3]
    video_url = generate_video_url(frigate_url, event_id)

    try:
        frigate_folder_id = find_or_create_folder(UPLOAD_DIR)
        if not frigate_folder_id:
            logging.error(f"Failed to find or create folder: {UPLOAD_DIR}")
            return False

        year_folder_id = find_or_create_folder(year, frigate_folder_id)
        if not year_folder_id:
            logging.error(f"Failed to find or create folder: {year}")
            return False

        month_folder_id = find_or_create_folder(month, year_folder_id)
        if not month_folder_id:
            logging.error(f"Failed to find or create folder: {month}")
            return False

        day_folder_id = find_or_create_folder(day, month_folder_id)
        if not day_folder_id:
            logging.error(f"Failed to find or create folder: {day}")
            return False

        with tempfile.TemporaryFile() as fh:
            response = requests.get(video_url, stream=True, timeout=300)
            if response.status_code == 200:
                for chunk in response.iter_content(chunk_size=8192):
                    fh.write(chunk)
                fh.seek(0)
                media = MediaIoBaseUpload(fh, mimetype='video/mp4', resumable=True)
                file_metadata = {'name': filename, 'parents': [day_folder_id]}
                try:
                    file = service.files().create(body=file_metadata, media_body=media, fields='id').execute()
                    if 'id' in file:
                        logging.info(f"Video {filename} successfully uploaded to Google Drive with ID: {file['id']}.")
                        return True
                    else:
                        logging.error(f"Failed to upload video {filename} to Google Drive. No file ID returned.")
                        return False
                except HttpError as error:
                    logging.error(f"Error uploading to Google Drive: {error}")
                    return False
            elif response.status_code == 500 and response.json().get('message') == "Could not create clip from recordings":
                logging.warning(f"Clip not found for event {event_id}.")
                if database.select_tries(event_id) >= 10:
                    database.update_event(event_id, 0, retry=0)
                    logging.error(f"Clip creation failed for {event_id}. "
                                  f"Couldn't download its clip from {generate_video_url(frigate_url, event_id)}. "
                                  f"Marking as non-retriable.")
                return False
            logging.error(f"Could not download video from {video_url}. Status code: {response.status_code}")
            return False

    except (requests.RequestException, ssl.SSLError) as e:
        logging.error(f"Error downloading video from {video_url}: {e}")
        return False
    except Exception as e:
        logging.error(f"Unexpected error: {e}", exc_info=True)
        return False


