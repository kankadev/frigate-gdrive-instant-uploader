import logging
import requests
from requests.exceptions import ChunkedEncodingError, ConnectionError
from time import sleep


class EventNotFoundError(Exception):
    """Raised when an event no longer exists on the Frigate server (HTTP 404)."""
    pass


def generate_video_url(frigate_url, event_id):
    return f"{frigate_url}/api/events/{event_id}/clip.mp4"


def fetch_event(frigate_url, event_id, retries=2, timeout=30):
    for attempt in range(retries):
        try:
            response = requests.get(f'{frigate_url}/api/events/{event_id}', timeout=timeout)
            if response.status_code == 404:
                raise EventNotFoundError(f"Event {event_id} not found on Frigate")
            response.raise_for_status()
            return response.json()
        except EventNotFoundError:
            raise
        except (ChunkedEncodingError, ConnectionError, requests.HTTPError) as e:
            logging.error(f"Attempt {attempt + 1} failed with error: {e}")
            if attempt < retries - 1:
                sleep(2)
            else:
                logging.error(f"All retries failed for fetching event {event_id}: {e}")
                raise


def fetch_all_events(frigate_url, after=None, batch_size=100, retries=2, timeout=30):
    all_events = []
    before = None

    while True:
        params = {'limit': batch_size, 'has_clip': 1}
        if before:
            params['before'] = before
        elif after:
            params['after'] = after

        for attempt in range(retries):
            try:
                response = requests.get(f'{frigate_url}/api/events', params=params, timeout=timeout)
                response.raise_for_status()  # Raise an HTTPError for bad responses
                break  # If the request was successful, exit the retry loop
            except (ChunkedEncodingError, ConnectionError) as e:
                logging.error(f"Attempt {attempt + 1} failed with error: {e}")
                if attempt < retries - 1:
                    sleep(2)  # Wait a bit before retrying
                else:
                    logging.error(f"All retries failed for fetching events: {e}")
                    return None

        if response.status_code == 200:
            events = response.json()
            if not events:
                break  # No more events to fetch
            all_events.extend(events)
            before = events[-1]['start_time']
            after = None  # Clear after the first successful fetch
            logging.debug(f"Fetched {len(events)} events, next 'before' set to {before}")
        else:
            logging.error(f"Failed to fetch events: {response.status_code} {response.text}")
            break

    return all_events