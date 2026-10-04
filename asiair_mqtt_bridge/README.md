# ASIAIR MQTT Bridge

Experimental Home Assistant add-on that exposes selected ZWO ASIAIR telemetry and controls through MQTT Discovery.

> This is an early-development project, not a complete replacement for the official ASIAIR app. Some functions are experimental, firmware-dependent, or still need wider hardware testing.

## Example configuration

```yaml
devices:
  - id: "asiair_1"
    name: "ASIAIR Observatory"
    host: "192.168.1.100"

poll_interval: 30
keepalive_interval: 8
topic_prefix: "asiair"
discovery_prefix: "homeassistant"
allow_control: false
log_level: "INFO"
```

Start with `allow_control: false` until monitoring is confirmed. Enable it only if you intentionally want write controls.

For firmware requiring authenticated TCP 4700 access, place your own official ASIAIR APK in `/share/asiair/`. The APK and extracted key material are not distributed with this repository.

See [DOCS.md](DOCS.md) for details and current limitations.
