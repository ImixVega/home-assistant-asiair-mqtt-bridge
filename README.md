# ASIAIR MQTT Bridge for Home Assistant

> [!WARNING]
> **Early development / experimental project.** This is **not a complete ASIAIR replacement** and it still needs substantial testing and refinement. A large part of monitoring and several useful controls already work, but some functions are incomplete, firmware-dependent, or still under investigation.

Unofficial Home Assistant OS add-on that connects directly to a ZWO ASIAIR on the local network and exposes its state through MQTT Discovery.

The project is intended for people who want ASIAIR telemetry and selected controls available in Home Assistant without replacing the official ASIAIR app.

## Current status — v0.1.20

What already works on supported/tested setups:

- ASIAIR connection and MQTT Discovery
- authenticated imager RPC on current firmware when the user supplies their own official ASIAIR APK locally
- ASIAIR system / Wi-Fi / Station Mode diagnostics
- Main Camera telemetry: state, temperature, gain, exposure, cooler, target temperature and anti-dew heater
- EFW detection, current filter and filter selection
- EAF telemetry, Slow/Fast step mode, `+`, `-` and AF
- Mount telemetry and Tracking ON/OFF
- ASIAIR DC outputs where the hardware supports them, including physical ON/OFF read-back and output type selection
- automatic capability detection so unsupported power entities are not created on hardware that does not provide them
- storage information and FITS file count
- experimental current-frame preview through TCP 4800
- ASIAIR-like Home Assistant grouping: ASIAIR, Main Camera, Guide, Mount, Focuser, EFW, Files and Autorun
- optional Restart and Shutdown buttons

Known limitations / work in progress:

- **EAF “Set current position” is currently not reliable and needs more reverse-engineering/testing.**
- FITS browsing/counting and current-frame preview are still experimental.
- Pro / Plus / Mini handling is capability-based and needs wider testing on more hardware revisions.
- not every ASIAIR screen, device or workflow is implemented yet.
- the protocol is unofficial and may change with ASIAIR firmware/app updates.
- this project has so far been tested on a limited set of equipment; reports from other setups are welcome.

## Home Assistant device layout

The MQTT Discovery layout follows the official ASIAIR app as closely as practical:

- **ASIAIR** — name, Station Mode, Wi-Fi, diagnostics, supported DC power outputs, supply telemetry, Enter, Restart and Shutdown.
- **Main Camera** — camera state, temperature, gain/exposure, cooler/dew controls and current-frame preview.
- **Guide** — guiding state, SNR and RA/DEC/total errors.
- **Mount** — coordinates, tracking and mount state.
- **Focuser** — EAF state and controls.
- **EFW** — wheel state, current slot/filter and filter selection.
- **Files** — current storage, capacity/free/used space, FITS count and latest saved file.
- **Autorun** — target, sequence state, frame counters and progress.

## Installation — Home Assistant OS / Supervisor

This repository is structured as a Home Assistant add-on repository.

1. In Home Assistant open **Settings → Add-ons → Add-on Store**.
2. Open the **⋮** menu and choose **Repositories**.
3. Add:

   ```text
   https://github.com/ImixVega/home-assistant-asiair-mqtt-bridge
   ```

4. Refresh the Add-on Store and install **ASIAIR MQTT Bridge**.
5. Configure the IP address/name of your ASIAIR.
6. Start with `allow_control: false` until monitoring is confirmed.
7. Start the add-on and check the log.

The add-on uses the Home Assistant Supervisor MQTT service, so an MQTT broker/integration available to Supervisor is required.

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

Multiple ASIAIR units can be added under `devices` with unique `id` values.

## Firmware 14.39+ authentication

Recent ASIAIR firmware uses a challenge/response verification on TCP 4700. This repository **does not include the official ASIAIR APK, private key, credentials, tokens, or any user-specific data**.

To enable full authenticated camera/EFW/EAF RPC access, provide an APK from **your own official ASIAIR app installation** locally to Home Assistant, for example:

```text
/share/asiair/ASIAIR.apk
```

The add-on inspects the APK locally at runtime. The APK and extracted authentication material are not part of this repository and should not be committed to GitHub. `.gitignore` explicitly excludes `*.apk`, `*.pem` and `*.key`.

Without the local authentication material, the bridge can still remain connected in a reduced/event-only mode where supported.

## Controls and safety

Write controls are disabled by default:

```yaml
allow_control: false
```

Set `allow_control: true` only when you want Home Assistant controls such as cooler, filter wheel, focuser movement, Tracking, DC outputs, Restart or Shutdown.

Some commands can interrupt an imaging session or power connected equipment. In particular, changing a DC output type may cause ASIAIR to reconnect attached devices. Use automation around power and shutdown controls carefully.

## Power capability detection

There is no manual Pro / Plus / Mini selector. The bridge probes the functions actually exposed by the connected ASIAIR and only creates supported entities.

- no DC-output RPC → no DC-output UI
- `pi_output_get2` → DC output switches and output type state/control
- `get_power_supply` → input/total power telemetry when the firmware/hardware provides it

This avoids permanent `unknown` entities on ASIAIR models that do not contain the corresponding hardware.

## Documentation

Detailed protocol notes, entity behavior and configuration are in [`asiair_mqtt_bridge/DOCS.md`](asiair_mqtt_bridge/DOCS.md).

The implementation is independent and based on observed network behavior plus publicly available community protocol research. See [`asiair_mqtt_bridge/THIRD_PARTY_NOTICES.md`](asiair_mqtt_bridge/THIRD_PARTY_NOTICES.md).

## Privacy / sensitive data

The repository intentionally contains only generic example addresses and names. Do not commit:

- your ASIAIR APK
- extracted PEM/private keys
- MQTT passwords/tokens
- public IPs, VPN credentials or other private network secrets
- packet captures containing information you do not want public

## Disclaimer

This is an unofficial community project and is not affiliated with or endorsed by ZWO. ASIAIR and ZWO are trademarks of their respective owner.

Use at your own risk. Test controls locally before relying on them for unattended operation.

## License

Copyright (C) 2026 ImixVega and contributors.

This project is licensed under the **GNU General Public License v3.0 or later (GPL-3.0-or-later)**. You may use, modify and redistribute it under the terms of the GPL. See [`LICENSE`](LICENSE).

`SPDX-License-Identifier: GPL-3.0-or-later`
