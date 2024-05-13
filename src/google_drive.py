import logging
import os
import ssl
import tempfile
import requests
from dotenv import load_dotenv
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload
from datetime import datetime
import pytz

from src import database

load_dotenv()

UPLOAD_DIR = os.getenv('UPLOAD_DIR')
TIMEZONE = os.getenv('TIMEZONE', 'Europe/Istanbul')


def generate_filename(camera_name, start_time, event_id):
    utc_time = datetime.fromtimestamp(start_time, pytz.utc)
    local_time = utc_time.astimezone(pytz.timezone(TIMEZONE))
    return f"{local_time.strftime('%Y-%m-%d-%H-%M-%S')}__{camera_name}__{event_id}.mp4"


def find_or_create_folder(service, name, parent_id=None):
    try:
        query = f"name='{name}' and mimeType='application/vnd.google-apps.folder'"
        if parent_id:
            query += f" and parents in '{parent_id}'"
        results = service.files().list(q=query, spaces='drive', fields='files(id)').execute()
        folder = results.get('files', [])
        if not folder:
            folder_metadata = {
                'name': name,
                'mimeType': 'application/vnd.google-apps.folder',
                'parents': [parent_id] if parent_id else []
            }
            folder = service.files().create(body=folder_metadata, fields='id').execute()
            return folder.get('id')
        else:
            return folder[0]['id']

    except HttpError as error:
        logging.error(f"An error occurred: {error}")
        return None


def upload_to_google_drive(service, event, frigate_url):
    camera_name = event['camera']
    start_time = event['start_time']
    event_id = event['id']
    filename = generate_filename(camera_name, start_time, event_id)
    year, month, day = filename.split("__")[0].split("-")[:3]
    video_url = f"{frigate_url}/api/events/{event_id}/clip.mp4"

    try:
        frigate_folder_id = find_or_create_folder(service, UPLOAD_DIR)
        if not frigate_folder_id:
            logging.error(f"Failed to find or create folder: {UPLOAD_DIR}")
            return False

        year_folder_id = find_or_create_folder(service, year, frigate_folder_id)
        if not year_folder_id:
            logging.error(f"Failed to find or create folder: {year}")
            return False

        month_folder_id = find_or_create_folder(service, month, year_folder_id)
        if not month_folder_id:
            logging.error(f"Failed to find or create folder: {month}")
            return False

        day_folder_id = find_or_create_folder(service, day, month_folder_id)
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
                logging.error(f"Clip not found for event {event_id}.")
                if database.select_tries(event_id) >= 10:
                    database.update_event(event_id, 0, retry=0)
                    logging.error(f"Clip creation failed for {event_id}. Marking as non-retriable.")
                return False
            logging.error(f"Could not download video from {video_url}. Status code: {response.status_code}")
            return False

    except (requests.RequestException, ssl.SSLError) as e:
        logging.error(f"Error downloading video from {video_url}: {e}")
        return False
    except Exception as e:
        logging.error(f"Unexpected error: {e}", exc_info=True)
        return False


