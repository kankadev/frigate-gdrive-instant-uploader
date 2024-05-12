import logging

import requests


def generate_video_url(frigate_url, event_id):
    return f"{frigate_url}/api/events/{event_id}/clip.mp4"


def fetch_all_events(frigate_url, batch_size=100):
    all_events = []
    before = None

    while True:
        params = {'limit': batch_size}
        if before:
            params['before'] = before

        response = requests.get(f'{frigate_url}/api/events', params=params)
        if response.status_code == 200:
            events = response.json()
            if not events:
                break  # No more events to fetch
            all_events.extend(events)
            before = events[-1]['start_time']
            logging.debug(f"Fetched {len(events)} events, next 'before' set to {before}")
        else:
            logging.error(f"Failed to fetch events: {response.status_code} {response.text}")
            break

    return all_events
