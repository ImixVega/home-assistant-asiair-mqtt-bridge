# ASIAIR MQTT Bridge 0.1.26

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
  - id: ed80_asi2600
    name: ASIAIR_1_PRO
    host: 192.168.1.100
poll_interval: 30
keepalive_interval: 8
topic_prefix: asiair
discovery_prefix: homeassistant
allow_control: true
log_level: DEBUG
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


## End-of-night controls (v0.1.26)

This version uses **RPCs captured from the official ASIAIR app**, rather than
speculative generic commands. Set `allow_control: true` to expose the buttons.

| HA device | Control | TCP | ASIAIR method | Params |
|---|---|---|---|---|
| Autorun | Start / Resume active Autorun | 4700 | `start_exposure` | `["light", false]` |
| Autorun | Pause active Autorun / Plan | 4700 | `stop_exposure` | `[true]` |
| Plan | Start / Resume active Plan | 4700 | `start_exposure` | `["light", false]` |
| Plan | Pause active Plan | 4700 | `stop_exposure` | `[true]` |
| Mount | Go Home Position | 4400 | `scope_park` | *omitted* |
| Mount | Tracking | 4400 | `scope_set_track_state` | `[true]` or `[false]` |

**Important: mode selection.** The Start/Resume and Pause RPCs are the same in
both screens. They act on the capture job selected in the *ASIAIR app/device*.
The Plan buttons **do not switch to Plan mode**. Select/configure the intended
Plan (or Autorun sequence) in ASIAIR before using the HA buttons. Neither button
rewrites `set_plan` or clears the saved sequence.

**Telemetry and command acknowledgement are separate.** `Capture command
status` is `start_requested`, `pause_requested`, `start_accepted_unconfirmed`,
`pause_accepted_unconfirmed`, `failed`, or `rpc_timeout_unconfirmed`. It is not
proof that exposure has started/stopped. Wait for the **Capture working** binary
sensor to change from a fresh ASIAIR `get_app_state` read-back or relevant
Sequence/Exposure event. On a disconnected/reconnected port 4700 the retained
session values are invalidated to avoid using yesterday's state.

**Park/home telemetry is deliberately conservative.** `scope_park` accepting
an RPC only produces **Home state = `accepted_unconfirmed`**. Only an appropriate
asynchronous `ScopeHome`/`ScopePark` event may mark `complete`; a failure/error
marks `failed`. A missing RPC reply becomes `rpc_timeout_unconfirmed`; if an
accepted park does not generate a completion event within 300 seconds the sensor
shows `completion_timeout_unconfirmed`. The bridge does not infer "parked" from
Tracking OFF or Slewing OFF. If the ASIAIR firmware does not emit one of those
events, this sensor may stay unconfirmed until the timeout; do not use it as a
completed-state automation trigger until you have observed `complete` on real
hardware. A Go Home request is deferred while capture is
working. It does not abort Plate Solve/AutoGoto and cannot move until fresh
readbacks prove idle capture and a stationary mount.
If capture state is unknown, the new deferred Go Home stays pending and cannot dispatch until two fresh idle readbacks are obtained.

Suggested HA automation after manual hardware validation:

1. Press **Pause active Autorun / Plan** (whichever mode is currently active).
2. Wait with timeout for **Capture working = OFF** (no `unavailable`/`unknown`).
3. Press **Go Home Position** (requests deferred park; the bridge sends `scope_park` only after fresh safety checks).
4. Wait with timeout for **Home state = complete**. If a timeout/unconfirmed
   error occurs, stop the automation and notify the operator.
5. Only after confirmed park: check Tracking switch; send **OFF** if necessary.
6. Check Tracking OFF readback via `scope_get_track_state`.

**Physical movement warning:** Only run the first Go Home test with the mount
under observation, cables clear, safe mechanical limits, and camera capture
paused. The wire capture proves the command used by the app but does not yet
prove reliable completion-event behavior on your particular mount/firmware.


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
`Flat Panel`, and `Other`; `Mount` maps to the `telescope` type observed on the
user's ASIAIR capture.

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


### Autorun Pause troubleshooting

`pause_requested` means the HA/MQTT button queued the control command but the TCP write is not yet confirmed. `pause_sent_waiting_reply` proves the bridge sent `stop_exposure [true]` to TCP 4700; `pause_accepted_unconfirmed` proves ASIAIR accepted the RPC but NOT that the ongoing exposure has actually stopped. `rpc_timeout_unconfirmed` or `failed` indicate missing/negative ACK. The bridge fixes a queue starvation problem during continuous incoming ASIAIR events and accepts `code: 0` acknowledgement packets without `result`. Check physical capture behavior separately. If `pause_accepted_unconfirmed` but Autorun keeps running, record INFO logs and a new ASIAIR packet capture to investigate session/page semantics.


## v0.1.26 — Deferred Go Home request and stale event recovery

**One click, safe wait:** the Mount `Go Home Position` button sends a *request*
only. If the camera is active, `Home state = waiting_for_capture_stop` instead of
`blocked_capture_active`. A fresh two-sample `get_app_state.capture.is_working`
readback (at least two seconds apart) plus a fresh `scope_is_moving=false`
mount response are required before the existing `scope_park` is dispatched.
Known active AutoGoto/ScopeGoto events block parking while the event continues.
Some firmware does not send their terminal event; an orphaned `working` event
can be cleared after 30 seconds with **no new GoTo activity**, but ONLY after
two fresh camera-idle confirmations plus a fresh stationary-mount reply. This
is a conservative recovery for missing events, not positive proof of a Plate
Solve / GoTo completion. An unknown/null mount motion reply does not authorize
a park. Do not rely on unattended park until verified on your hardware.

The wait period is **5 minutes**, independent of the later **5-minute park
completion timeout**. Failure/timeout means **no park sent** and must be handled
as an alarm rather than forced by retrying blindly.

Use the new **Cancel pending Go Home** button to cancel while waiting, resulting
in `cancelled_before_dispatch`. It does NOT reverse or stop a physical Home
movement after `scope_park` has been dispatched. A repeated Go Home click while
waiting is deliberately ignored (no timeout reset or duplicate motion).

This bridge does **not** send `stop_exposure` when Go Home is requested. You must
issue **Pause active Autorun / Plan** yourself, or have an HA automation do it,
and wait for `pause_confirmed`. If ASIAIR keeps Plate Solving or AutoGoto remains
active (and keeps emitting fresh events), Go Home can time out safely instead
of initiating a risky movement.

Tracking confirmation: after a successful `scope_set_track_state` reply, the
bridge asks the mount channel (TCP 4400) for `scope_get_track_state` without
a channel-variable exception or connection reset.

**Status values:** `checking_safe_state`, `waiting_for_capture_stop`,
`waiting_for_mount_idle`, `waiting_for_goto`, `requested`,
`accepted_unconfirmed`, `working`, `complete`, `failed`,
`cancelled_before_dispatch`, `waiting_timeout_no_park`, and explicit offline/
RPC/completion timeout states. Do not treat any state except a new `complete`
for the active park as confirmation of Home.

## Camera control readback and safe automation

On current ASIAIR firmware, `get_control_value` must use **two** positional
arguments, e.g. `{"method":"get_control_value","params":["CoolerOn",true]}`.
The earlier single-element form caused repeating firmware `109 / unexpected param`
errors, about every 5 seconds. Both slow and fast polling, and manual Enter,
now use the two-argument form. If a particular control is still unsupported,
only that control is skipped until reconnect; the other camera readings remain active.

**Capture command status:** `pause_requested` → `pause_sent_waiting_reply` →
`pause_accepted_unconfirmed` → **`pause_confirmed`**. The corresponding resume
status is `start_confirmed`. The final confirmation comes from a NEW
`get_app_state.capture.is_working` response after successful RPC acceptance.
The bridge polls readback approximately every 3 seconds while needed and reports
`confirmation_timeout_unconfirmed` if no matching state is seen within 360 s.
The `Capture working` binary sensor itself is still ASIAIR telemetry; it is
not set ON/OFF optimistically on command ACK.

**HA interlocks:** Automations should wait for both `Capture working: OFF`
and `Capture command status: pause_confirmed` when a pause was issued.
Only then press `Go Home Position`, wait for a fresh `Home state: complete`
from the `ScopeHome` event, and only then check/change mount Tracking.
The `blocked_capture_active` state indicates the Go Home request was **not sent**
because capture was still reported ON.

An optional, **not automatically installed** example is in
`examples/ha_end_of_astronomical_night.yaml`. Replace all placeholder entity
IDs for a single selected ASIAIR. It triggers at astronomical dawn
(solar elevation rising above -18°); confirm your own preferred timing before
activating. Test physically under supervision with mount/cables clear.
