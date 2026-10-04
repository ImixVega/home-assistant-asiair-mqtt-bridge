import json
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

from asiair_client import AsiairDevice
from mqtt_bridge import MqttBridge, slugify

VERSION = "0.1.20"
OPTIONS_PATH = Path("/data/options.json")


def as_bool(value: str) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def main() -> int:
    options = json.loads(OPTIONS_PATH.read_text())
    log_level = str(options.get("log_level", "INFO")).upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)s [%(threadName)s] %(message)s",
    )
    log = logging.getLogger("asiair-mqtt")

    devices_cfg = options.get("devices") or []
    if not devices_cfg:
        log.error("No ASIAIR devices configured")
        return 2

    seen_ids = set()
    normalized = []
    for item in devices_cfg:
        node_id = slugify(str(item.get("id") or item.get("name") or item.get("host")))
        if node_id in seen_ids:
            raise ValueError(f"Duplicate ASIAIR id: {node_id}")
        seen_ids.add(node_id)
        normalized.append((node_id, str(item["name"]), str(item["host"])))

    requested_control = bool(options.get("allow_control", False))

    bridge = MqttBridge(
        host=os.environ["MQTT_HOST"],
        port=int(os.environ.get("MQTT_PORT", "1883")),
        username=os.environ.get("MQTT_USER", ""),
        password=os.environ.get("MQTT_PASSWORD", ""),
        use_ssl=as_bool(os.environ.get("MQTT_SSL", "false")),
        topic_prefix=str(options.get("topic_prefix", "asiair")),
        discovery_prefix=str(options.get("discovery_prefix", "homeassistant")),
        version=VERSION,
        allow_control=requested_control,
    )

    for node_id, name, host in normalized:
        bridge.register_device(node_id, name, host)

    bridge.connect()
    bridge.publish_all_discovery(clean_stale=True)

    poll_interval = int(options.get("poll_interval", 30))
    keepalive_interval = int(options.get("keepalive_interval", 8))
    clients = [
        AsiairDevice(node_id, name, host, bridge, poll_interval, keepalive_interval)
        for node_id, name, host in normalized
    ]

    stop = threading.Event()

    def request_stop(signum, frame):
        log.info("Shutdown requested")
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    try:
        for client in clients:
            client.start()
        while not stop.wait(1.0):
            pass
    finally:
        for client in clients:
            try:
                client.stop()
            except Exception:
                log.exception("Error stopping %s", client.name)
        bridge.close()

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        logging.exception("Fatal error")
        raise
