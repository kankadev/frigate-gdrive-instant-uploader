import logging
import requests


class MattermostHandler(logging.Handler):
    def __init__(self, webhook_url):
        super().__init__(level=logging.ERROR)  # Set handler level to ERROR
        self.webhook_url = webhook_url

    def emit(self, record):
        try:
            log_entry = self.format(record)
            payload = {"text": log_entry}
            response = requests.post(self.webhook_url, json=payload)
            response.raise_for_status()
        except Exception as e:
            print(f"Failed to send log to Mattermost: {e}")


# Mattermost Webhook URL
mattermost_webhook_url = 'https://mattermost.example.com/hooks/your-webhook-url'
