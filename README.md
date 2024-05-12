# Frigate to Google Drive Instant Uploader with MQTT
This is a simple script that uploads event clips from Frigate to Google Drive instantly using MQTT (without cronjobs).
It uses a SQLite database to keep track of uploaded events and only uploads new events. In case of an error,
the script will retry to upload the event clip.

Because I'm using Mattermost as chat software, 
I've added a webhook to send a message to a Mattermost channel when an error occurs.

You'll need a MQTT broker like Apache Mosquitto or similar. This script watches for new events from Frigate 
and uploads them to Google Drive within seconds.

In my case I use Apache Mosquitto as MQTT broker and Frigate as NVR software. Frigate, Mosquitto 
and this script are running on the same Proxmox server in LXC containers.

# Requirements
- python 3.8
- MQTT broker
- Frigate with configured MQTT

# Example Frigate configuration
```yaml

mqtt:
  host: 192.168.0.55
  user: username
  password: secret
  port: 1883
  topic_prefix: frigate
  client_id: frigate

# rest of your config.yml
````

Check if your MQTT broker is working by subscribing to the topic `frigate/events` with a MQTT client like MQTT Explorer 
or mosquitto_sub. If so, you should see events from Frigate and can use this script.

# Usage without Docker
1. clone this repository
2. rename `env_example` to `.env` and change values to your needs
3. run `python setup.py` in project root directory to install all required packages
4. create a project in google cloud console and enable drive api
5. download the credentials json file from Google and copy its content to `credentials/google_drive_credentials.json`
6. run `python main.py` in project root directory

# Usage with Docker
1. clone this repository
2. rename `env_example` to `.env` and change values to your needs
3. create a project in google cloud console and enable drive api
4. download the credentials json file from Google and copy its content to `credentials/google_drive_credentials.json`
5. run `docker compose up -d` in project root directory
6. check logs with `docker logs frigate-gdrive-instant-uploader` or see `/logs/app.log`