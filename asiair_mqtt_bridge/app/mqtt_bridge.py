import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable

import paho.mqtt.client as mqtt

from entities import ENTITIES, EntityDef

LOG = logging.getLogger(__name__)

# These entities are only discovered after the ASIAIR has actually supplied a value.
# This keeps unsupported hardware capabilities (for example input V/A/W on some models)
# out of Home Assistant instead of showing permanent "unknown" entities.
OPTIONAL_ENTITY_KEYS = {
    "input_voltage", "input_current", "input_power",
    "dc_output_1", "dc_output_2", "dc_output_3", "dc_output_4",
    "dc_output_1_type", "dc_output_2_type", "dc_output_3_type", "dc_output_4_type",
    "dc_output_1_voltage", "dc_output_2_voltage", "dc_output_3_voltage", "dc_output_4_voltage",
}
CONTROL_ENTITY_KEYS = {
    "enter_button", "autorun_start_button", "autorun_stop_button", "plan_start_button", "plan_pause_button", "mount_home_button", "mount_cancel_pending_home_button", "mount_tracking_control",
    "cooler_control", "target_temperature_control", "dew_heater_control", "filter_control",
    "focuser_current_position_control", "focuser_step_mode_control",
    "focuser_minus_button", "focuser_plus_button", "focuser_af_button",
    "refresh_image_button", "restart_button", "shutdown_button",
    "dc_output_1_control", "dc_output_2_control", "dc_output_3_control", "dc_output_4_control",
    "dc_output_1_type_control", "dc_output_2_type_control", "dc_output_3_type_control", "dc_output_4_type_control",
}


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9_-]+", "_", value)
    return re.sub(r"_+", "_", value).strip("_") or "asiair"


class MqttBridge:
    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        use_ssl: bool,
        topic_prefix: str,
        discovery_prefix: str,
        version: str,
        allow_control: bool = False,
    ) -> None:
        self.topic_prefix = topic_prefix.rstrip("/")
        self.discovery_prefix = discovery_prefix.rstrip("/")
        self.version = version
        self.allow_control = allow_control
        self._command_handlers: Dict[str, Callable[[str, str], None]] = {}
        self._connected = threading.Event()
        self._devices: Dict[str, Dict[str, Any]] = {}
        self._last_values: Dict[str, Any] = {}
        self._last_times: Dict[str, float] = {}
        self._state_lock = threading.Lock()
        self._topics_file = Path("/data/discovery_topics.json")
        # Dynamic MQTT select options keyed by (node_id, entity_key).  Filter
        # names are learned from get_wheel_setting at runtime, so they cannot
        # be hard-coded in Home Assistant discovery.
        self._select_options: Dict[str, list[str]] = {}
        # Runtime number bounds keyed by (node_id, entity_key).  EAF max_step
        # is device/configuration dependent and is learned from get_focuser_info.
        self._number_limits: Dict[str, Dict[str, float]] = {}

        client_id = f"asiair_mqtt_bridge_{os.getpid()}"
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        if username:
            self.client.username_pw_set(username, password)
        if use_ssl:
            self.client.tls_set()
        self.client.reconnect_delay_set(min_delay=2, max_delay=60)
        self.bridge_availability_topic = f"{self.topic_prefix}/bridge/availability"
        self.client.will_set(self.bridge_availability_topic, "offline", qos=1, retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self._host = host
        self._port = port

    def connect(self, timeout: int = 30) -> None:
        LOG.info("Connecting to MQTT broker %s:%s", self._host, self._port)
        self.client.connect_async(self._host, self._port, keepalive=60)
        self.client.loop_start()
        if not self._connected.wait(timeout):
            raise RuntimeError("Timed out connecting to MQTT broker")

    def close(self) -> None:
        try:
            if self._connected.is_set():
                self.client.publish(self.bridge_availability_topic, "offline", qos=1, retain=True).wait_for_publish(3)
        except Exception:
            pass
        self.client.disconnect()
        self.client.loop_stop()

    @staticmethod
    def _reason_code_failed(reason_code: Any) -> bool:
        """Return True when a Paho MQTT callback reason code indicates failure.

        Paho MQTT 2.x passes a ReasonCode object to VERSION2 callbacks instead
        of the integer return code used by the older callback API.
        """
        is_failure = getattr(reason_code, "is_failure", None)
        if is_failure is not None:
            return bool(is_failure)
        try:
            return reason_code != 0
        except Exception:
            return True

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if self._reason_code_failed(reason_code):
            LOG.error("MQTT connection failed: %s", reason_code)
            return
        LOG.info("MQTT connected: %s", reason_code)
        self._connected.set()
        client.publish(self.bridge_availability_topic, "online", qos=1, retain=True)
        if self.allow_control:
            client.subscribe(f"{self.topic_prefix}/+/command/+", qos=0)
        if self._devices:
            self.publish_all_discovery(clean_stale=False)

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties) -> None:
        self._connected.clear()
        if self._reason_code_failed(reason_code):
            LOG.warning("MQTT disconnected unexpectedly: %s", reason_code)
        else:
            LOG.info("MQTT disconnected: %s", reason_code)

    def register_command_handler(self, node_id: str, handler: Callable[[str, str], None]) -> None:
        self._command_handlers[node_id] = handler

    def _on_message(self, client, userdata, message) -> None:
        if not self.allow_control:
            return
        try:
            topic = str(message.topic)
            prefix = f"{self.topic_prefix}/"
            if not topic.startswith(prefix):
                return
            rest = topic[len(prefix):].split("/")
            if len(rest) != 3 or rest[1] != "command":
                return
            node_id, _command_word, command = rest
            payload = message.payload.decode("utf-8", errors="replace").strip()
            handler = self._command_handlers.get(node_id)
            if handler is None:
                LOG.warning("No command handler registered for ASIAIR %s", node_id)
                return
            LOG.info("MQTT command %s/%s payload=%r", node_id, command, payload)
            handler(command, payload)
        except Exception:
            LOG.exception("Failed to process MQTT command")

    def register_device(self, node_id: str, name: str, host: str) -> None:
        root_ident = f"asiair_{node_id}"
        self._devices[node_id] = {
            "name": name,
            "host": host,
            "root_ident": root_ident,
            "metadata": {
                "root": {"model": "ASIAIR"},
                "camera": {"model": "Main Camera"},
                "mount": {"model": "Mount"},
                "guider": {"model": "Guider"},
                "focuser": {"model": "Focuser"},
                "filterwheel": {"model": "EFW"},
                "files": {"model": "Files"},
                "autorun": {"model": "Autorun"},
                "plan": {"model": "Plan"},
            },
        }

    def update_metadata(self, node_id: str, group: str, **values: Any) -> None:
        device = self._devices[node_id]
        target = device["metadata"].setdefault(group, {})
        changed = False
        for key, value in values.items():
            if value is not None and target.get(key) != value:
                target[key] = value
                changed = True
        if changed and self._connected.is_set():
            self.publish_discovery_for_device(node_id)


    def set_select_options(self, node_id: str, entity_key: str, options: Iterable[str]) -> None:
        values = [str(x) for x in options if str(x)]
        cache_key = f"{node_id}:{entity_key}"
        if self._select_options.get(cache_key) == values:
            return
        self._select_options[cache_key] = values
        if self._connected.is_set():
            entity = next((e for e in ENTITIES if e.key == entity_key), None)
            if entity is not None:
                if entity.key in CONTROL_ENTITY_KEYS and not self.allow_control:
                    return
                topic = self._discovery_topic(node_id, entity)
                payload = self._discovery_payload(node_id, entity)
                self.client.publish(topic, json.dumps(payload, separators=(",", ":")), qos=1, retain=True)

    def set_number_limits(
        self,
        node_id: str,
        entity_key: str,
        *,
        min_value: float,
        max_value: float,
        step: float = 1.0,
    ) -> None:
        values = {"min": float(min_value), "max": float(max_value), "step": float(step)}
        cache_key = f"{node_id}:{entity_key}"
        if self._number_limits.get(cache_key) == values:
            return
        self._number_limits[cache_key] = values
        if self._connected.is_set():
            entity = next((e for e in ENTITIES if e.key == entity_key), None)
            if entity is not None:
                topic = self._discovery_topic(node_id, entity)
                payload = self._discovery_payload(node_id, entity)
                self.client.publish(topic, json.dumps(payload, separators=(",", ":")), qos=1, retain=True)

    def _device_info(self, node_id: str, group: str) -> Dict[str, Any]:
        device = self._devices[node_id]
        root_ident = device["root_ident"]
        meta = device["metadata"].get(group, {})
        if group == "root":
            info = {
                "identifiers": [root_ident],
                "name": device["name"],
                "manufacturer": "ZWO",
                "model": meta.get("model", "ASIAIR"),
                "suggested_area": "Observatory",
            }
        else:
            label = {
                "camera": "Main Camera",
                "mount": "Mount",
                "guider": "Guide",
                "focuser": "Focuser",
                "filterwheel": "EFW",
                "files": "Files",
                "autorun": "Autorun",
                "plan": "Plan",
            }.get(group, group.title())
            info = {
                "identifiers": [f"{root_ident}_{group}"],
                "name": f"{device['name']} {label}",
                "manufacturer": "ZWO",
                "model": meta.get("model", label),
                "via_device": root_ident,
            }
        for field in ("serial_number", "sw_version", "hw_version"):
            if meta.get(field):
                info[field] = str(meta[field])
        return info

    def _availability_for(self, node_id: str, entity: EntityDef) -> list:
        topics = [
            {"topic": self.bridge_availability_topic, "payload_available": "online", "payload_not_available": "offline"},
        ]
        if entity.key != "connected":
            topics.append({
                "topic": f"{self.topic_prefix}/{node_id}/availability",
                "payload_available": "online",
                "payload_not_available": "offline",
            })
        if entity.group in ("mount", "guider") and entity.key != "mount_channel_connected":
            topics.append({
                "topic": f"{self.topic_prefix}/{node_id}/port4400/availability",
                "payload_available": "online",
                "payload_not_available": "offline",
            })
        return topics

    def _discovery_payload(self, node_id: str, entity: EntityDef) -> Dict[str, Any]:
        state_key = entity.state_key or entity.key
        state_topic = f"{self.topic_prefix}/{node_id}/{state_key}"
        payload: Dict[str, Any] = {
            "name": entity.name,
            "unique_id": f"asiair_{node_id}_{entity.key}",
            "device": self._device_info(node_id, entity.group),
            "availability": self._availability_for(node_id, entity),
            "availability_mode": "all",
            "origin": {"name": "ASIAIR MQTT Bridge", "sw": self.version},
        }
        # MQTT buttons are stateless. MQTT image entities receive raw image
        # bytes on their dedicated image topic. All other entity types use the
        # retained state topic.
        if entity.component == "image":
            payload["image_topic"] = f"{self.topic_prefix}/{node_id}/image/current"
            payload["content_type"] = "image/png"
        elif entity.component != "button":
            payload["state_topic"] = state_topic
        if entity.icon:
            payload["icon"] = entity.icon
        if entity.unit:
            payload["unit_of_measurement"] = entity.unit
        if entity.device_class:
            payload["device_class"] = entity.device_class
        if entity.state_class:
            payload["state_class"] = entity.state_class
        if entity.entity_category:
            payload["entity_category"] = entity.entity_category
        if entity.suggested_display_precision is not None:
            payload["suggested_display_precision"] = entity.suggested_display_precision
        if entity.component in ("binary_sensor", "switch"):
            payload["payload_on"] = entity.payload_on or "ON"
            payload["payload_off"] = entity.payload_off or "OFF"
        if entity.component in ("switch", "number", "select", "button"):
            payload["command_topic"] = f"{self.topic_prefix}/{node_id}/command/{entity.command_key or entity.key}"
        if entity.component == "button":
            payload["payload_press"] = entity.payload_press or "PRESS"
        if entity.component == "select":
            payload["options"] = self._select_options.get(f"{node_id}:{entity.key}", [])
        if entity.component == "number":
            limits = self._number_limits.get(f"{node_id}:{entity.key}", {})
            min_value = limits.get("min", entity.min_value)
            max_value = limits.get("max", entity.max_value)
            step = limits.get("step", entity.step)
            if min_value is not None:
                payload["min"] = min_value
            if max_value is not None:
                payload["max"] = max_value
            if step is not None:
                payload["step"] = step
            if entity.mode:
                payload["mode"] = entity.mode
        return payload

    def _discovery_topic(self, node_id: str, entity: EntityDef) -> str:
        return f"{self.discovery_prefix}/{entity.component}/asiair_{node_id}/{entity.key}/config"

    def publish_discovery_for_device(self, node_id: str) -> set[str]:
        topics = set()
        for entity in ENTITIES:
            if entity.key in CONTROL_ENTITY_KEYS and not self.allow_control:
                continue
            state_key = entity.state_key or entity.key
            cache_key = f"{node_id}:{state_key}"
            if entity.component == "select" and not self._select_options.get(f"{node_id}:{entity.key}"):
                # Do not create an unusable select before the ASIAIR has told us
                # how many wheel slots it has and what they are called. Keep
                # its discovery topic in the saved current-topic set, though,
                # so toggling allow_control on/off can still clean stale config.
                topics.add(self._discovery_topic(node_id, entity))
                continue
            if state_key in OPTIONAL_ENTITY_KEYS and cache_key not in self._last_values:
                continue
            topic = self._discovery_topic(node_id, entity)
            topics.add(topic)
            payload = self._discovery_payload(node_id, entity)
            self.client.publish(topic, json.dumps(payload, separators=(",", ":")), qos=1, retain=True)
        return topics

    def publish_all_discovery(self, clean_stale: bool = True) -> None:
        current = set()
        for node_id in self._devices:
            current.update(self.publish_discovery_for_device(node_id))
        if clean_stale:
            old = set()
            try:
                if self._topics_file.exists():
                    old = set(json.loads(self._topics_file.read_text()))
            except Exception as exc:
                LOG.warning("Could not read old discovery topic list: %s", exc)
            for topic in old - current:
                LOG.info("Removing stale MQTT discovery topic %s", topic)
                self.client.publish(topic, "", qos=1, retain=True)
            try:
                self._topics_file.parent.mkdir(parents=True, exist_ok=True)
                self._topics_file.write_text(json.dumps(sorted(current), indent=2))
            except Exception as exc:
                LOG.warning("Could not save discovery topic list: %s", exc)

    @staticmethod
    def _encode_value(value: Any, binary: bool) -> str:
        if binary:
            return "ON" if bool(value) else "OFF"
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (dict, list)):
            return json.dumps(value, separators=(",", ":"))
        return str(value)

    def _remember_discovery_topic(self, topic: str) -> None:
        """Persist dynamically-created discovery topics for stale cleanup on restart."""
        try:
            current = set()
            if self._topics_file.exists():
                current = set(json.loads(self._topics_file.read_text()))
            if topic not in current:
                current.add(topic)
                self._topics_file.parent.mkdir(parents=True, exist_ok=True)
                self._topics_file.write_text(json.dumps(sorted(current), indent=2))
        except Exception as exc:
            LOG.debug("Could not persist dynamic discovery topic %s: %s", topic, exc)

    def publish_state(self, node_id: str, key: str, value: Any, *, min_interval: float = 0.0, force: bool = False) -> None:
        if value is None:
            return
        entity = next((e for e in ENTITIES if e.key == key), None)
        if entity is None:
            entity = next((e for e in ENTITIES if e.state_key == key), None)
        if entity is None:
            LOG.debug("Ignoring unknown entity key %s", key)
            return
        cache_key = f"{node_id}:{key}"
        now = time.monotonic()
        with self._state_lock:
            if not force:
                if self._last_values.get(cache_key) == value:
                    return
                last_time = self._last_times.get(cache_key, 0.0)
                if min_interval and now - last_time < min_interval:
                    return
            self._last_values[cache_key] = value
            self._last_times[cache_key] = now
        if key in OPTIONAL_ENTITY_KEYS:
            # Capability-driven entities are advertised only after a real value
            # was received. Optional control entities must still respect the
            # global allow_control safety switch.
            if entity.key not in CONTROL_ENTITY_KEYS or self.allow_control:
                topic = self._discovery_topic(node_id, entity)
                discovery = self._discovery_payload(node_id, entity)
                self.client.publish(topic, json.dumps(discovery, separators=(",", ":")), qos=1, retain=True)
                self._remember_discovery_topic(topic)
        payload = self._encode_value(value, entity.component in ("binary_sensor", "switch"))
        self.client.publish(f"{self.topic_prefix}/{node_id}/{key}", payload, qos=0, retain=True)

    def publish_image(self, node_id: str, png_bytes: bytes) -> None:
        """Publish one raw PNG frame for the MQTT image entity.

        Images are deliberately not retained: a 1-2 MiB retained payload per
        ASIAIR would unnecessarily consume broker memory. The bridge refreshes
        after every SaveImage and on the manual Refresh image button.
        """
        if not png_bytes:
            return
        topic = f"{self.topic_prefix}/{node_id}/image/current"
        self.client.publish(topic, payload=png_bytes, qos=0, retain=False)

    def publish_unknown(self, node_id: str, key: str) -> None:
        """Invalidate a retained state without pretending a value is known.

        Home Assistant treats a payload other than payload_on/payload_off as unknown
        for MQTT binary sensors, and the literal state ``unknown`` is also correct for
        ordinary sensors.  Retaining it prevents an old observing session from being
        restored after an ASIAIR or add-on reconnect.
        """
        entity = next((e for e in ENTITIES if e.key == key), None)
        if entity is None:
            entity = next((e for e in ENTITIES if e.state_key == key), None)
        if entity is None:
            return
        cache_key = f"{node_id}:{key}"
        with self._state_lock:
            self._last_values[cache_key] = "__unknown__"
            self._last_times[cache_key] = time.monotonic()
        self.client.publish(f"{self.topic_prefix}/{node_id}/{key}", "unknown", qos=0, retain=True)

    def publish_device_availability(self, node_id: str, online: bool) -> None:
        self.client.publish(
            f"{self.topic_prefix}/{node_id}/availability",
            "online" if online else "offline",
            qos=1,
            retain=True,
        )
        self.publish_state(node_id, "connected", online, force=True)

    def publish_port4400_availability(self, node_id: str, online: bool) -> None:
        self.client.publish(
            f"{self.topic_prefix}/{node_id}/port4400/availability",
            "online" if online else "offline",
            qos=1,
            retain=True,
        )
        self.publish_state(node_id, "mount_channel_connected", online, force=True)
