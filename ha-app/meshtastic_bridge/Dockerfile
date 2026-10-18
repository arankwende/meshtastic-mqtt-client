ARG BUILD_FROM=ghcr.io/home-assistant/base-python:3.14-alpine3.24-2026.10.0
FROM ${BUILD_FROM}

# Installs the released version matching this app's version (see config.yaml).
ARG APP_VERSION=2.0.0
RUN pip install --no-cache-dir \
    "meshtastic-mqtt-client @ https://github.com/arankwende/meshtastic-mqtt-client/archive/refs/tags/v${APP_VERSION}.tar.gz"

COPY run.sh config.template.yaml /
RUN chmod a+x /run.sh

CMD ["/run.sh"]
