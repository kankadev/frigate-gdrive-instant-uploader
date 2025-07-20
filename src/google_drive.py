import logging
import os
import ssl
import socket
import tempfile
import threading
import requests
from dotenv import load_dotenv
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload
from datetime import datetime, timedelta
import pytz
from google.oauth2 import service_account
from googleapiclient.discovery import build

from src import database
from src.frigate_api import generate_video_url

load_dotenv()
GDRIVE_RETENTION_DAYS = int(os.getenv('GDRIVE_RETENTION_DAYS', 0))

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

# Cache for folder IDs to avoid repeated lookups and improve resilience
_folder_id_cache = {}

# Lock to prevent race conditions when creating folders
folder_creation_lock = threading.Lock()


def generate_filename(camera_name, start_time, event_id):
    utc_time = datetime.fromtimestamp(start_time, pytz.utc)
    local_time = utc_time.astimezone(pytz.timezone(TIMEZONE))
    return f"{local_time.strftime('%Y-%m-%d-%H-%M-%S')}__{camera_name}__{event_id}.mp4"


def find_or_create_folder(name, parent_id=None):
    """
    Finds a folder by name and parent_id, creating it if it doesn't exist.
    Uses a cache to avoid repeated API calls and improve resilience against network errors.
    """
    cache_key = (parent_id, name)
    if cache_key in _folder_id_cache:
        logging.debug(f"Found folder '{name}' in cache with ID: {_folder_id_cache[cache_key]}")
        return _folder_id_cache[cache_key]

    # Use a lock to prevent race conditions where multiple threads try to create the same folder.
    with folder_creation_lock:
        # Double-check the cache inside the lock in case another thread populated it while waiting
        if cache_key in _folder_id_cache:
            logging.debug(f"Found folder '{name}' in cache (after lock) with ID: {_folder_id_cache[cache_key]}")
            return _folder_id_cache[cache_key]

        try:
            query = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
            if parent_id:
                query += f" and '{parent_id}' in parents"

            results = service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
            folders = results.get('files', [])

            if not folders:
                folder_metadata = {
                    'name': name,
                    'mimeType': 'application/vnd.google-apps.folder',
                    'parents': [parent_id] if parent_id else []
                }
                folder = service.files().create(body=folder_metadata, fields='id').execute()
                folder_id = folder.get('id')
                logging.debug(f"Created folder '{name}' with ID: {folder_id}")
                _folder_id_cache[cache_key] = folder_id
                return folder_id
            else:
                folder_id = folders[0]['id']
                logging.debug(f"Found existing folder '{name}' with ID: {folder_id}")
                _folder_id_cache[cache_key] = folder_id
                return folder_id

        except (HttpError, socket.timeout) as error:
            logging.error(f"An error occurred while finding or creating folder '{name}': {error}")
            return None


def get_folder_id(drive_service, folder_name, parent_id):
    try:
        query = f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
        if parent_id:
            query += f" and '{parent_id}' in parents"

        results = drive_service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
        folders = results.get('files', [])

        if not folders:
            return None
        else:
            return folders[0]['id']

    except HttpError as error:
        logging.error(f"An error occurred while finding folder '{folder_name}': {error}")
        return None


def cleanup_old_files_on_drive(drive_service):
    """
    Deletes files older than GDRIVE_RETENTION_DAYS from Google Drive and cleans up empty parent folders.
    """
    if GDRIVE_RETENTION_DAYS == 0:
        logging.info("GDRIVE_RETENTION_DAYS is set to 0, skipping cleanup.")
        return

    logging.info(f"Starting cleanup of files older than {GDRIVE_RETENTION_DAYS} days on Google Drive...")

    try:
        # Calculate the cutoff date
        cutoff_date = datetime.now() - timedelta(days=GDRIVE_RETENTION_DAYS)
        cutoff_iso = cutoff_date.isoformat() + 'Z'

        # Find the root upload folder first
        upload_dir_name = os.getenv('UPLOAD_DIR', 'Frigate')
        folder_id = get_folder_id(drive_service, upload_dir_name, 'root')
        if not folder_id:
            logging.warning(f"Root upload folder '{upload_dir_name}' not found. Cannot perform cleanup.")
            return

        # Find and delete old files recursively. The 'trashed=false' is crucial.
        query = f"mimeType='video/mp4' and trashed=false and createdTime < '{cutoff_iso}'"
        page_token = None
        while True:
            response = drive_service.files().list(q=query,
                                                  spaces='drive',
                                                  fields='nextPageToken, files(id, name, parents)',
                                                  pageToken=page_token).execute()
            for file in response.get('files', []):
                file_id = file.get('id')
                file_name = file.get('name')
                parent_folders = file.get('parents')
                logging.info(f"Deleting old file: {file_name} (ID: {file_id})")
                drive_service.files().delete(fileId=file_id).execute()

                # Cleanup empty parent folders
                if parent_folders:
                    cleanup_empty_parent_folders(drive_service, parent_folders[0])

            page_token = response.get('nextPageToken', None)
            if page_token is None:
                break

        logging.info("Google Drive cleanup finished.")

    except HttpError as error:
        logging.error(f'An error occurred during Google Drive cleanup: {error}')
    except Exception as e:
        logging.error(f'An unexpected error occurred during Google Drive cleanup: {e}')


def cleanup_empty_parent_folders(drive_service, folder_id):
    """
    Recursively deletes a folder and its parents if they become empty.
    """
    try:
        # Check if the folder is empty
        q = f"'{folder_id}' in parents"
        response = drive_service.files().list(q=q, spaces='drive', fields='files(id)').execute()
        if not response.get('files', []):
            # Get folder details to find its parent
            folder_details = drive_service.files().get(fileId=folder_id, fields='name, parents').execute()
            folder_name = folder_details.get('name')
            parent_folders = folder_details.get('parents')

            logging.info(f"Deleting empty folder: {folder_name} (ID: {folder_id})")
            drive_service.files().delete(fileId=folder_id).execute()

            # Recursively check the parent folder
            if parent_folders:
                cleanup_empty_parent_folders(drive_service, parent_folders[0])
    except HttpError as error:
        # It's possible another process deleted it, so we can ignore 'not found' errors
        if error.resp.status == 404:
            logging.warning(f"Folder with ID {folder_id} not found, likely already deleted.")
        else:
            logging.error(f'An error occurred while cleaning up empty folder {folder_id}: {error}')


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


