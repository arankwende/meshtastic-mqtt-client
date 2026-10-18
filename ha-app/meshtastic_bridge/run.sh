#!/command/with-contenv bashio
# shellcheck shell=bash
set -e

CONFIG=/config/config.yaml

# Offer the Mosquitto app's broker to config.yaml as !env MQTT_HOST etc.
if bashio::services.available "mqtt"; then
    export MQTT_HOST MQTT_PORT MQTT_USERNAME MQTT_PASSWORD
    MQTT_HOST=$(bashio::services mqtt "host")
    MQTT_PORT=$(bashio::services mqtt "port")
    MQTT_USERNAME=$(bashio::services mqtt "username")
    MQTT_PASSWORD=$(bashio::services mqtt "password")
    bashio::log.info "Found MQTT broker ${MQTT_HOST}:${MQTT_PORT}"
fi

if [ ! -f "${CONFIG}" ]; then
    cp /config.template.yaml "${CONFIG}"
    chmod 600 "${CONFIG}"
    bashio::log.warning "Created ${CONFIG}. Edit it (node IDs, channel and key), then restart this app."
    bashio::exit.nok "Configuration needed"
fi

ARGS=(--headless -c "${CONFIG}")
if bashio::config.true "debug"; then
    ARGS+=(--debug)
fi
exec meshtastic-mqtt-client "${ARGS[@]}"
