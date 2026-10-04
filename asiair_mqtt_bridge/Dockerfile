FROM ghcr.io/home-assistant/base:latest

ARG BUILD_VERSION
ARG BUILD_ARCH

LABEL \
  io.hass.version="${BUILD_VERSION}" \
  io.hass.type="app" \
  io.hass.arch="${BUILD_ARCH}"

COPY requirements.txt /tmp/requirements.txt
RUN apk add --no-cache python3 py3-pip openssl \
    && python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm -f /tmp/requirements.txt

COPY run.sh /
COPY app /app
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
