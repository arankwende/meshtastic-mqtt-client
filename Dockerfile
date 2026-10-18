# Headless bridge image: docker run -v ./config:/config meshtastic-mqtt-client
FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY meshtastic_mqtt_client ./meshtastic_mqtt_client
RUN pip install --no-cache-dir . \
    && useradd --system --uid 1000 --home-dir /config mesh \
    && mkdir -p /config && chown mesh /config

USER mesh
VOLUME /config
# Exec form, so the client is PID 1 and receives SIGTERM from `docker stop`.
ENTRYPOINT ["meshtastic-mqtt-client", "--headless", "-c", "/config/config.yaml"]
