import logging
import os

import requests

MATTERMOST_PREFIX = os.getenv('MATTERMOST_PREFIX', '')


class MattermostHandler(logging.Handler):
    def __init__(self, webhook_url):
        super().__init__(level=logging.ERROR)
        self.webhook_url = webhook_url

    def emit(self, record):
        try:
            log_entry = self.format(record)
            prefixed_log_entry = f"{MATTERMOST_PREFIX} {log_entry}"
            payload = {"text": prefixed_log_entry}
            response = requests.post(self.webhook_url, json=payload)
            response.raise_for_status()
        except Exception as e:
            logging.error(f"Failed to send log to Mattermost: {e}")
