import logging
import os

import requests

MATTERMOST_PREFIX = os.getenv('MATTERMOST_PREFIX', '')
MATTERMOST_WEBHOOK_URL = os.getenv('MATTERMOST_WEBHOOK_URL')


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


def send_mattermost_notification(title, text, color="#36a64f", webhook_url=None):
    """
    Sends a structured notification to Mattermost using attachment formatting.
    Color codes:
      - "#36a64f" green (OK)
      - "#ffae42" orange (WARNING)
      - "#d50000" red (CRITICAL)
    """
    url = webhook_url or MATTERMOST_WEBHOOK_URL
    if not url:
        logging.debug("MATTERMOST_WEBHOOK_URL not configured. Skipping notification.")
        return False

    prefix = f"{MATTERMOST_PREFIX} " if MATTERMOST_PREFIX else ""
    payload = {
        "attachments": [
            {
                "color": color,
                "title": f"{prefix}{title}",
                "text": text,
            }
        ]
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        return True
    except Exception as e:
        logging.error(f"Failed to send notification to Mattermost: {e}")
        return False
