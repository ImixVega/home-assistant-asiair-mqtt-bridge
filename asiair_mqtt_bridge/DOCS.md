# ASIAIR MQTT Bridge 0.1.20

> **Development status:** v0.1.20 is an experimental, incomplete implementation. Many functions work, but the project still needs wider hardware/firmware testing. EAF “Set current position” is currently known to be unreliable.

The add-on connects to ASIAIR TCP 4700 (imager) and TCP 4400 (mount/guider) and publishes Home Assistant entities through MQTT Discovery.

## Port 4700 verification

Current ASIAIR firmware expects the same per-connection challenge/response used by the official application:

1. `get_verify_str`
2. sign the returned challenge with RSA PKCS#1 v1.5 + SHA-1 using the key extracted locally from the official app
3. `verify_client` with `sign` and `data`

Only after that verification does the imager service answer ordinary RPC calls such as `get_app_state`, `get_control_value`, `get_wheel_setting` and `get_focuser_info`.

## Camera telemetry

The bridge reads:
- camera name/state
- gain and exposure
- temperature
- cooler enabled state and cooler power
- target temperature
- anti-dew heater state

ASIAIR returns `get_control_value("Temperature")` in tenths of a degree, while the pushed `Temperature` event is already in °C; the bridge normalizes both to °C.

## Optional camera controls

Controls are disabled by default. Enable them with:

```yaml
allow_control: true
```

Home Assistant then exposes:
- **Cooler** → `set_control_value ["CoolerOn", 0/1]`
- **Target temperature** → `set_control_value ["TargetTemp", value]`
- **Dew heater** → `set_control_value ["AntiDewHeater", 0/1]`

The displayed state is refreshed from ASIAIR; commands are not treated as successful merely because MQTT sent them.

## Filter wheel

The bridge uses `get_wheel_setting` for exact user-defined filter names and `get_wheel_position` for the current index. ASIAIR uses zero-based indices, so raw position `0` is shown as **Slot 1**.

## EAF focuser

The official application reads EAF data with `get_focuser_info`. The bridge uses the same method and publishes:
- position
- state
- temperature

It also reads `get_focuser_setting` so the manual-control buttons use the same **fine** and **coarse** step sizes configured in ASIAIR.

With `allow_control: true`, Home Assistant exposes:
- **Set current position** → `set_focuser_value [position]` (changes the logical position counter; it is not a GoTo)
- **Step mode** → local `Slow` / `Fast` selector; `Slow=fine_step`, `Fast=coarse_step`
- **−** / **+** → `stop_focuser` then `move_focuser [absolute_target]`, calculated from the confirmed current position and selected step
- **AF** → `stop_focuser` then `start_auto_focuse`

The current-position number gets its dynamic maximum from `get_focuser_info.max_step`. After a write/move/AF command the bridge re-reads `get_focuser_info`; normal fast polling continues to track the physical EAF state.

It also updates Home Assistant device metadata with the EAF model, serial number and firmware when ASIAIR supplies them.

## Configuration example

```yaml
devices:
  - id: asiair_1
    name: ASIAIR Observatory
    host: 192.168.1.100
poll_interval: 30
keepalive_interval: 8
topic_prefix: asiair
discovery_prefix: homeassistant
allow_control: false
log_level: INFO
```

For normal operation, use `log_level: INFO` after testing.


## Port 4700 authentication (firmware 14.39+)

The imager channel uses a challenge/response handshake. The server returns a random string from `get_verify_str`; the client signs that string with RSA PKCS#1 v1.5 + SHA-1 and sends the base64 signature in `verify_client`.

This add-on does **not** ship the official application's private key. To enable authenticated RPC reads/writes, provide one of:

- `/share/asiair/*.apk` — the add-on extracts the embedded PEM key locally and stores it under `/data/asiair_auth.pem`; or
- `/share/asiair/asiair.pem` — a PEM private key extracted by the user.

Without a key, port 4700 remains connected in event-only mode so unsolicited telemetry can still be forwarded.

### EFW filter control (v0.1.13)

When `allow_control: true` and an EFW is detected, Home Assistant gets a **Filter** select entity. Slot names come directly from ASIAIR `get_wheel_setting`; choosing an option sends `set_wheel_position` and confirms the new position with `get_wheel_position`.


## Enter / initialize ASIAIR (v0.1.15)

When `allow_control: true`, Home Assistant exposes an **Enter** button on the
root ASIAIR device. Use it after the add-on authenticates if the camera/EAF/EFW
still show `close`/unavailable. The bridge reproduces the confirmed official-app
device-open sequence and then refreshes live state.

The button does not start an exposure, sequence, guiding or mount movement.

## Mount tracking control (v0.1.16)

When `allow_control: true`, the mount device exposes a **Tracking** switch.
The command is sent on TCP 4400 as `scope_set_track_state [true/false]`.
The bridge does not assume success from the write alone; it immediately follows
with `scope_get_track_state` and publishes the confirmed state back to Home
Assistant.


## Storage telemetry and FITS count (v0.1.17)

The bridge reads `get_image_save_path` and `get_disk_volume` and publishes:

- current storage name and state
- physical disk size
- usable total / free / used space
- used percentage

On each imager-channel connection it also performs a serialized, read-only walk
of the ASIAIR image browser using `get_img_file_page_number` followed by
`get_img_file_page_name`. Only `.fit` and `.fits` files are counted. The browser
RPC is stateful, so the bridge intentionally scans one directory/page at a time
and skips log/system folders. After the baseline scan, each completed
`SaveImage` increments the counter locally.

## Current frame over MQTT (v0.1.17)

The camera child device exposes **Current frame** as an MQTT image entity. The
bridge opens TCP 4800 on demand and sends `get_current_img`. For the observed
ASIAIR protocol the response contains a binary header plus a ZIP archive with
`raw_data`. The add-on extracts the 8/16-bit frame, applies a simple percentile
stretch, downsamples to a maximum long edge of 1600 px and publishes a raw PNG
to Home Assistant.

The preview refreshes automatically shortly after `SaveImage: complete`. With
`allow_control: true`, a **Refresh image** button is also available for manual
refresh. The image payload is not retained by MQTT to avoid keeping multi-MB
retained messages on the broker.


## Automatic Pro / Mini / Plus capability detection (v0.1.18)

The add-on intentionally has no manual model selector. On port 4700 it probes `pi_output_get2` and `get_power_supply` and creates only the entities that the connected ASIAIR actually supports.

- `pi_output_get2` available => four DC output switches.
- `get_power_supply` available => input voltage, total current, calculated total power, and up to four DC-output voltage sensors.
- neither available => no power-output UI (Mini-style capability set).

The product label is inferred as Plus / Pro / Mini only after capability probes have resolved. `pi_get_info.model` is kept as hardware-version metadata instead of overwriting the product label with the Raspberry Pi board model.

DC output control uses `pi_output_set2` and preserves the last reported `type`, `is_pwm`, and `value`; only `state` is changed. A read-back with `pi_output_get2` confirms the switch state.

## DC output state and type controls (v0.1.19)

After `pi_output_get2` succeeds, Home Assistant receives the physical ON/OFF
state of each DC output and, when `allow_control: true`, a type select for each
port. Supported UI labels are `Mount`, `Camera`, `Focuser`, `Dew Heater`,
`Flat Panel`, and `Other`; `Mount` maps to the ASIAIR protocol value `telescope`.

The select state is always published from the subsequent `pi_output_get2`
read-back. Type writes preserve `state`, `value`, and `is_pwm` and alter only
the `type` field. ASIAIR itself may disconnect/reconnect attached equipment
when a port type is changed, so treat type changes as configuration rather than
a high-frequency automation action.

## Home Assistant device layout (v0.1.20)

The MQTT discovery layout follows the ASIAIR app more closely:

- **ASIAIR** — name, Station Mode/Wi-Fi, diagnostics, power outputs, Plus power telemetry, Enter, Restart, Shutdown.
- **Main Camera** — camera state, temperature, cooler/dew controls and current-frame preview.
- **Guide** — guiding state and errors.
- **Mount** — coordinates and tracking.
- **Focuser** — EAF state and controls.
- **EFW** — filter wheel state and filter selection.
- **Files** — active storage, capacity/free/used space, FITS count and latest saved file.
- **Autorun** — target, sequence state and frame progress.

Restart/Shutdown and other write controls are only exposed when `allow_control: true`.
