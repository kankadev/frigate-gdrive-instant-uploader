import logging
import os
import ssl
import socket
import tempfile
import threading
import time
import random
import requests
from dotenv import load_dotenv
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
from datetime import datetime, timedelta
import pytz
from google.oauth2 import service_account
from googleapiclient.discovery import build
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src import database
from src.frigate_api import generate_video_url

load_dotenv()
GDRIVE_RETENTION_DAYS = int(os.getenv('GDRIVE_RETENTION_DAYS', 0))

UPLOAD_DIR = os.getenv('UPLOAD_DIR')
# Prioritize standard 'TZ' env var, but fall back to 'TIMEZONE' for backward compatibility.
TIMEZONE = os.getenv('TZ', os.getenv('TIMEZONE', 'Europe/Istanbul'))
SERVICE_ACCOUNT_FILE = os.getenv('SERVICE_ACCOUNT_FILE')
GOOGLE_ACCOUNT_TO_IMPERSONATE = os.getenv('GOOGLE_ACCOUNT_TO_IMPERSONATE')

# Configure retry strategy for Google Drive API
MAX_RETRIES = 5
INITIAL_RETRY_DELAY = 1  # seconds
MAX_RETRY_DELAY = 60  # seconds
UPLOAD_CHUNK_SIZE = 1024 * 1024 * 10  # 10MB chunks for resumable uploads
DOWNLOAD_TIMEOUT = 300  # 5 minutes for video download

SCOPES = ['https://www.googleapis.com/auth/drive']

def get_google_service():
    """Initialize and return a Google Drive service with retry support."""
    # Debug: Print the service account file path and check if it exists
    logging.info(f"Loading service account from: {SERVICE_ACCOUNT_FILE}")
    
    if not os.path.isfile(SERVICE_ACCOUNT_FILE):
        logging.error(f"Service account file not found at: {SERVICE_ACCOUNT_FILE}")
        logging.error(f"Current working directory: {os.getcwd()}")
        logging.error(f"Directory contents: {os.listdir(os.path.dirname(SERVICE_ACCOUNT_FILE))}")
        raise FileNotFoundError(f"Service account file not found at: {SERVICE_ACCOUNT_FILE}")
    
    try:
        if GOOGLE_ACCOUNT_TO_IMPERSONATE:
            credentials = service_account.Credentials.from_service_account_file(
                SERVICE_ACCOUNT_FILE, scopes=SCOPES, subject=GOOGLE_ACCOUNT_TO_IMPERSONATE)
            logging.info(f"Using service account with impersonation: {GOOGLE_ACCOUNT_TO_IMPERSONATE}")
        else:
            credentials = service_account.Credentials.from_service_account_file(
                SERVICE_ACCOUNT_FILE, scopes=SCOPES)
            logging.info("Using service account without impersonation")
    except Exception as e:
        logging.error(f"Error loading service account credentials: {str(e)}")
        raise

    # Authorize the credentials with a custom session
    from google.auth.transport.requests import AuthorizedSession
    
    # Configure retry strategy
    retry_strategy = Retry(
        total=MAX_RETRIES,
        backoff_factor=1,
        status_forcelist=[500, 502, 503, 504, 429],
        allowed_methods=["GET", "POST", "PUT", "DELETE"]
    )
    
    # Create an HTTP adapter with retry strategy
    adapter = HTTPAdapter(max_retries=retry_strategy)
    
    # Create an authorized session with the credentials
    session = AuthorizedSession(credentials)
    
    # Mount the adapter to the session
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    
    # Build the service with the authorized session
    return build('drive', 'v3', cache_discovery=False, requestBuilder=lambda *args, **kwargs: session)

# Initialize the service
service = get_google_service()

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


def exponential_backoff(retries):
    """Calculate exponential backoff with jitter."""
    if retries == 0:
        return 0
    jitter = random.uniform(0, 1)
    return min(INITIAL_RETRY_DELAY * (2 ** (retries - 1)) + jitter, MAX_RETRY_DELAY)

def download_video_with_retry(video_url, max_retries=3):
    """Download video with retry logic and proper timeout handling."""
    retry_count = 0
    last_error = None
    
    while retry_count <= max_retries:
        try:
            with requests.Session() as session:
                # Configure retry strategy for the download
                retry_strategy = Retry(
                    total=3,
                    backoff_factor=1,
                    status_forcelist=[500, 502, 503, 504],
                    allowed_methods=["GET"]
                )
                adapter = HTTPAdapter(max_retries=retry_strategy)
                session.mount("https://", adapter)
                session.mount("http://", adapter)
                
                with session.get(video_url, stream=True, timeout=DOWNLOAD_TIMEOUT) as response:
                    response.raise_for_status()
                    
                    with tempfile.TemporaryFile() as fh:
                        for chunk in response.iter_content(chunk_size=8192):
                            if chunk:  # filter out keep-alive new chunks
                                fh.write(chunk)
                        fh.seek(0)
                        return fh.read()
                        
        except (requests.RequestException, ssl.SSLError, socket.timeout) as e:
            last_error = e
            retry_count += 1
            if retry_count <= max_retries:
                wait_time = exponential_backoff(retry_count)
                logging.warning(f"Attempt {retry_count}/{max_retries} failed. Retrying in {wait_time:.2f}s. Error: {e}")
                time.sleep(wait_time)
    
    logging.error(f"Failed to download video after {max_retries} attempts. Last error: {last_error}")
    return None

def upload_to_google_drive(event, frigate_url):
    """Upload a video to Google Drive with retry logic and proper error handling."""
    camera_name = event['camera']
    start_time = event['start_time']
    event_id = event['id']
    filename = generate_filename(camera_name, start_time, event_id)
    year, month, day = filename.split("__")[0].split("-")[:3]
    video_url = generate_video_url(frigate_url, event_id)

    for attempt in range(MAX_RETRIES + 1):
        try:
            # 1. Ensure folder structure exists
            frigate_folder_id = find_or_create_folder(UPLOAD_DIR)
            if not frigate_folder_id:
                raise Exception(f"Failed to find or create folder: {UPLOAD_DIR}")

            year_folder_id = find_or_create_folder(year, frigate_folder_id)
            if not year_folder_id:
                raise Exception(f"Failed to find or create folder: {year}")

            month_folder_id = find_or_create_folder(month, year_folder_id)
            if not month_folder_id:
                raise Exception(f"Failed to find or create folder: {month}")

            day_folder_id = find_or_create_folder(day, month_folder_id)
            if not day_folder_id:
                raise Exception(f"Failed to find or create folder: {day}")

            # 2. Download video with retry logic
            video_data = download_video_with_retry(video_url)
            if video_data is None:
                raise Exception(f"Failed to download video from {video_url}")

            # 3. Upload to Google Drive with resumable upload
            media = MediaIoBaseUpload(
                io.BytesIO(video_data),
                mimetype='video/mp4',
                resumable=True,
                chunksize=UPLOAD_CHUNK_SIZE
            )
            
            file_metadata = {
                'name': filename,
                'parents': [day_folder_id]
            }

            request = service.files().create(
                body=file_metadata,
                media_body=media,
                fields='id',
                supportsAllDrives=True
            )
            
            response = None
            while response is None:
                status, response = request.next_chunk()
                if status:
                    logging.debug(f"Upload progress: {int(status.progress() * 100)}%")
            
            if 'id' in response:
                logging.info(f"Video {filename} successfully uploaded to Google Drive with ID: {response['id']}.")
                return True
            else:
                raise Exception("No file ID returned from Google Drive")

        except HttpError as error:
            if attempt < MAX_RETRIES and error.resp.status in [500, 502, 503, 504, 429]:
                wait_time = exponential_backoff(attempt + 1)
                logging.warning(f"Attempt {attempt + 1}/{MAX_RETRIES} failed with status {error.resp.status}. "
                              f"Retrying in {wait_time:.2f}s. Error: {error}")
                time.sleep(wait_time)
                continue
            logging.error(f"HTTP error uploading to Google Drive: {error}")
            return False
            
        except (requests.RequestException, ssl.SSLError, socket.timeout, socket.error) as e:
            if attempt < MAX_RETRIES:
                wait_time = exponential_backoff(attempt + 1)
                logging.warning(f"Attempt {attempt + 1}/{MAX_RETRIES} failed. Retrying in {wait_time:.2f}s. Error: {e}")
                time.sleep(wait_time)
                continue
            logging.error(f"Error in upload process: {e}", exc_info=True)
            return False
            
        except Exception as e:
            logging.error(f"Unexpected error: {e}", exc_info=True)
            return False
    
    logging.error(f"Failed to upload after {MAX_RETRIES + 1} attempts")
    return False


