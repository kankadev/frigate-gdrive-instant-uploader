import logging
import os
import tempfile
from datetime import datetime

import requests
from dotenv import load_dotenv
from googleapiclient.http import MediaIoBaseUpload
from datetime import datetime
import pytz

load_dotenv()

UPLOAD_DIR = os.getenv('UPLOAD_DIR')
TIMEZONE = os.getenv('TIMEZONE', 'Europe/Istanbul')


def generate_filename(camera_name, start_time, event_id):
    utc_time = datetime.fromtimestamp(start_time, pytz.utc)
    local_time = utc_time.astimezone(pytz.timezone(TIMEZONE))
    return f"{local_time.strftime('%Y-%m-%d-%H-%M-%S')}__{camera_name}__{event_id}.mp4"


def find_or_create_folder(service, name, parent_id=None):
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


def upload_to_google_drive(service, event, frigate_url):
    camera_name = event['camera']
    start_time = event['start_time']
    event_id = event['id']
    filename = generate_filename(camera_name, start_time, event_id)
    year, month, day = filename.split("__")[0].split("-")[:3]

    frigate_folder_id = find_or_create_folder(service, UPLOAD_DIR)
    year_folder_id = find_or_create_folder(service, year, frigate_folder_id)
    month_folder_id = find_or_create_folder(service, month, year_folder_id)
    day_folder_id = find_or_create_folder(service, day, month_folder_id)

    video_url = f"{frigate_url}/api/events/{event_id}/clip.mp4"

    with tempfile.TemporaryFile() as fh:
        response = requests.get(video_url, stream=True)
        if response.status_code == 200:
            for chunk in response.iter_content(chunk_size=8192):
                fh.write(chunk)
            fh.seek(0)
            media = MediaIoBaseUpload(fh, mimetype='video/mp4', resumable=True)
            file_metadata = {'name': filename, 'parents': [day_folder_id]}
            file = service.files().create(body=file_metadata, media_body=media, fields='id').execute()
            return True
        else:
            logging.error(f"Could not download video from {video_url}. Status code: {response.status_code}")
            return False
