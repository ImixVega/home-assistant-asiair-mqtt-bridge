# ASIAIR MQTT Bridge

Home Assistant OS add-on for monitoring ZWO ASIAIR devices over their local TCP protocol and exposing telemetry through MQTT Discovery.

## v0.1.26

The official ASIAIR application performs a challenge/response verification immediately after opening TCP port 4700. v0.1.11 implements that handshake, so the bridge can use normal imager RPC calls rather than relying only on unsolicited events.

After authentication the bridge reads camera state and controls, exact EFW slot names/position, and EAF information. Optional camera controls can be enabled with:

```yaml
allow_control: true
```

This exposes camera controls plus EFW/EAF controls when the corresponding hardware is present:
- Cooler ON/OFF
- Target temperature
- Camera anti-dew heater ON/OFF
- EFW filter selection
- EAF current-position redefine, Slow/Fast step mode, − / + movement and AF
- Mount Tracking ON/OFF

EFW positions are displayed as physical slots 1..N although ASIAIR uses zero-based indices internally.

See [DOCS.md](DOCS.md) for configuration and details.


### Firmware 14.39+ authentication

For full camera/EFW/EAF RPC access, place the official ASIAIR Android APK at `/share/asiair/*.apk` and restart the add-on. The key is extracted locally; it is not included in this project. Without the APK/key the bridge continues in event-only mode.

### EFW filter control (v0.1.13)

When `allow_control: true` and an EFW is detected, Home Assistant gets a **Filter** select entity. Slot names come directly from ASIAIR `get_wheel_setting`; choosing an option sends `set_wheel_position` and confirms the new position with `get_wheel_position`.

### EAF controls (v0.1.14)

With `allow_control: true`, Home Assistant exposes an ASIAIR-like EAF control set:

- **Set current position** — edits the EAF logical current-position counter with `set_focuser_value`.
- **Step mode** — `Slow` uses `fine_step`, `Fast` uses `coarse_step` learned from `get_focuser_setting`.
- **− / +** — moves by the selected step using absolute `move_focuser` targets.
- **AF** — starts ASIAIR autofocus with `start_auto_focuse` (firmware spelling).

The editable position range follows `max_step` reported by `get_focuser_info`.


### ASIAIR Enter button (v0.1.15)

With `allow_control: true`, the root ASIAIR device exposes an **Enter** button.
It mirrors the device-open phase captured from the official ASIAIR app: it opens
the selected main camera, reapplies remembered main/guide camera names and focal
length, opens the detected EAF/EFW, then immediately refreshes camera, cooler,
filter-wheel and focuser telemetry.

This is intentionally manual in v0.1.15. Once it is proven on the user's rigs it
can be promoted to an optional automatic action after authentication.

### Mount tracking + EAF movement fix (v0.1.16)

With `allow_control: true`, the mount device exposes a **Tracking** switch. It
uses the mount/scoping TCP 4400 channel and confirms the result by re-reading
`scope_get_track_state`.

Manual EAF `− / +` moves now mirror the official-app pattern observed in the
capture: `stop_focuser` followed by `move_focuser [absolute_target]`. AF also
stops any current focuser motion before sending `start_auto_focuse`. If EAF is
closed, the bridge first selects USB mode and opens the detected focuser.




### DC output feedback + type selector (v0.1.19)

DC output switches now use ASIAIR `pi_output_get2` as the source of truth and
publish Home Assistant MQTT switch states as `ON` / `OFF`. The icon therefore
follows the physical output state after every command and poll.

When DC outputs are available and `allow_control: true`, each output also gets
a type select with: **Mount, Camera, Focuser, Dew Heater, Flat Panel, Other**.
The selected option is the live status read back from ASIAIR, not an optimistic
local value. Changing type preserves the current output state, value and PWM
flag and then re-reads the complete power-port state. Note that ASIAIR may
reconnect attached equipment when a port type is changed.

### Automatic ASIAIR power capability detection (v0.1.18)

No Pro/Mini/Plus selector is required. The bridge probes the actual RPC capabilities:

- **Mini**: no DC output entities and no supply telemetry.
- **Pro**: four DC output switches are created after `pi_output_get2` succeeds.
- **Plus**: the same four DC output switches plus input voltage, total current, total power, and per-output voltage telemetry when `get_power_supply` supplies it.

The detected variant is published as `Hardware variant`. Unsupported entities are not advertised to Home Assistant, so they do not remain as permanent `unknown` controls. DC writes preserve the ASIAIR-reported port type, PWM flag, and level, and only change the requested on/off state.

### Storage + current-frame preview (v0.1.17)

The root ASIAIR device now exposes storage telemetry read directly from
`get_image_save_path` / `get_disk_volume`: current storage, mount state, physical
disk size, usable total/free/used space and used percentage. The bridge also
performs a read-only recursive image-browser scan at startup to count stored
`.fit` / `.fits` files, then increments that count on each `SaveImage` event.

The camera device now exposes an MQTT **Current frame** image entity and, when
`allow_control: true`, a **Refresh image** button. Current-frame data is fetched
from ASIAIR TCP 4800 with `get_current_img`, unpacked from `raw_data`,
auto-stretched and downsampled to at most 1600 px on the long edge before being
published as PNG. A refresh is also attempted automatically shortly after each
completed `SaveImage` event. The full tens-of-megabytes raw frame is never sent
through MQTT.

## Home Assistant device layout (v0.1.26)

The MQTT discovery layout follows the ASIAIR app more closely:

- **ASIAIR** — name, Station Mode/Wi-Fi, diagnostics, power outputs, Plus power telemetry, Enter, Restart, Shutdown.
- **Main Camera** — camera state, temperature, cooler/dew controls and current-frame preview.
- **Guide** — guiding state and errors.
- **Mount** — coordinates, Tracking, Go Home Position and Home state.
- **Focuser** — EAF state and controls.
- **EFW** — filter wheel state and filter selection.
- **Files** — active storage, capacity/free/used space, FITS count and latest saved file.
- **Autorun** — target, sequence state/frame progress, Start/Resume and Pause (active capture).
- **Plan** — Start/Resume and Pause for the already-selected Plan. The same RPCs are used by Autorun; these buttons **do not switch the ASIAIR mode**.

Restart/Shutdown and other write controls are only exposed when `allow_control: true`.

### End-of-night automation controls (v0.1.26)

**Official-app wire captures** confirm:

- **Autorun/Plan Start or Resume:** `start_exposure` with `["light", false]` (TCP 4700).
- **Autorun/Plan Pause:** `stop_exposure` with `[true]` (TCP 4700).
- **Mount Go Home:** `scope_park` with **no params** (TCP 4400).
- **Tracking ON/OFF:** `scope_set_track_state` with `[true]` or `[false]` (TCP 4400).

With `allow_control: true`, the bridge exposes these as HA buttons/switches. The
old `autorun_stop_button` entity ID is preserved, but it is now **Pause active
Autorun / Plan**, not `stop_capture`. **Plan buttons do not select Plan mode**:
configure/select the job first in the official ASIAIR application.

A safe automation must wait for `Capture working = OFF` before requesting park,
then wait for **Home state = complete** before disabling Tracking. The park RPC
response alone is **not** completion confirmation. If no completion event is
received the Home state remains unconfirmed and eventually times out; your HA
automation must fail safely rather than parking blindly. **Use unattended only after an observed test on your own mount.**

See `DOCS.md` for entity details and exact automation sequence.


### Pause command transport fix (v0.1.26)

Fixed delayed/unsent MQTT capture commands under a continuous ASIAIR TCP event stream. `Capture command status` now distinguishes requested, actually sent, and RPC accepted. Successful RPC acknowledgment does not prove that capture has stopped: always validate the live `Capture working` entity.


### v0.1.26 — Deferred Go Home with recovered GoTo status and Tracking fix

- One **Go Home Position** click requests a park even when camera acquisition or
  mount slewing is active. It **does not** send `scope_park` immediately.
- Home state shows `checking_safe_state`, `waiting_for_capture_stop`,
  `waiting_for_mount_idle`, or `waiting_for_goto` as appropriate.
- Before sending `scope_park`, the bridge requires **two fresh** `get_app_state`
  readings with `capture.is_working=false` at least 2s apart, a fresh positive
  `scope_is_moving=false` response, no current Sequence/AutoGoto activity, and
  connected/authenticated channels. When firmware omits a GoTo completion
  event, a stale `working` flag is only cleared after 30s without further
  active GoTo events AND fresh independently confirmed camera/mount idle. It polls the relevant states ~every 3s.
- **300-second timeout** (`waiting_timeout_no_park`), disconnect and cancellation
  all fail closed: no movement is requested. A second Go Home click does not
  duplicate or extend the waiting request.
- **Cancel pending Go Home** cancels only the *waiting* request; it is not a
  mount-motion abort and will not stop a park already dispatched.
- Neither Go Home nor cancellation pauses the camera automatically. For a
  shutdown automation, request Pause and wait for `pause_confirmed` first.
- v0.1.26 also fixes the `UnboundLocalError: channel` disconnect triggered
  by Tracking ON/OFF confirmation on port 4400.
- Hardware testing confirmed Go Home on the maintainer's mount; still test under direct supervision with
  clear mount/cable travel; a stale GoTo event is not proof of completion.

### Camera control polling and verified capture confirmation

The firmware 109 `unexpected param` warnings caused by one-argument
`get_control_value` reads are addressed using the documented `[name, true]`
format. An unsupported control name is skipped for the remainder of the
connection without disabling other camera telemetry.

After Pause/Resume, `Capture command status` now waits for a **new**
`get_app_state.capture.is_working` readback before reporting
`pause_confirmed` / `start_confirmed`; an accepted RPC alone is not enough.
For an optional interlocked Home Assistant automation template, see
[`examples/ha_end_of_astronomical_night.yaml`](examples/ha_end_of_astronomical_night.yaml).
