import requests


def generate_video_url(frigate_url, event_id):
    return f"{frigate_url}/api/events/{event_id}/clip.mp4"


# TODO: not in use yet
def fetch_events(frigate_url, days=3):
    params = {}
    if days is not None:
        params['days'] = days
    response = requests.get(f'{frigate_url}/api/events', params=params)
    if response.status_code == 200:
        return response.json()
    return None
