#!/usr/bin/with-contenv bashio
set -e

export MQTT_HOST="$(bashio::services mqtt 'host')"
export MQTT_PORT="$(bashio::services mqtt 'port')"
export MQTT_USER="$(bashio::services mqtt 'username')"
export MQTT_PASSWORD="$(bashio::services mqtt 'password')"
export MQTT_SSL="$(bashio::services mqtt 'ssl')"

exec /opt/venv/bin/python3 /app/main.py
