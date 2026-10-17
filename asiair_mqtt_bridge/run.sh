#!/usr/bin/with-contenv bashio
set -euo pipefail

export MQTT_HOST="$(bashio::services mqtt 'host')"
export MQTT_PORT="$(bashio::services mqtt 'port')"
export MQTT_USER="$(bashio::services mqtt 'username')"
export MQTT_PASSWORD="$(bashio::services mqtt 'password')"
export MQTT_SSL="$(bashio::services mqtt 'ssl')"

bashio::log.info "Starting ASIAIR MQTT Bridge"
bashio::log.info "Using Supervisor MQTT service at ${MQTT_HOST}:${MQTT_PORT}"

exec /opt/venv/bin/python3 -u /app/main.py
