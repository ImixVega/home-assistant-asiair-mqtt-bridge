## 0.1.20

- Reorganized Home Assistant devices to mirror ASIAIR sections: ASIAIR, Main Camera, Guide, Mount, Focuser, EFW, Files and Autorun.
- Moved DC outputs and Plus power telemetry onto the main ASIAIR device.
- Moved storage/FITS telemetry to a dedicated Files device.
- Moved sequence/target/frame progress to a dedicated Autorun device.
- Added Name, Station mode, Wi-Fi network/IP/frequency entities to the main ASIAIR device.
- Added Restart and Shutdown buttons (only when `allow_control: true`).

# Changelog

## 0.1.19

- Fixed DC output switch state feedback: MQTT switches now publish `ON`/`OFF`, so Home Assistant follows the physical `pi_output_get2` read-back instead of showing a stale icon.
- Added a capability-driven type select for each DC output: Mount, Camera, Focuser, Dew Heater, Flat Panel, Other.
- The selected type is itself the live status: it is published only from `pi_output_get2` read-back after ASIAIR confirms the change.
- DC type writes preserve the output state, value and PWM flag; only the logical ASIAIR type is changed.

## 0.1.18

- automatic capability-based ASIAIR Plus / Pro / Mini detection; no manual selector
- dynamic four-channel DC output switches when `pi_output_get2` is supported
- Plus-style input voltage, total current/power and per-output voltage telemetry via `get_power_supply`
- unsupported power entities are not advertised in Home Assistant
- DC writes preserve port type/PWM/value and verify state with a read-back
- keep Raspberry Pi board model as hardware metadata instead of ASIAIR product model


## 0.1.17

- Added ASIAIR storage sensors from `get_image_save_path` / `get_disk_volume`: current storage/state, physical size, usable total/free/used and used percentage.
- Added a read-only recursive FITS counter using the ASIAIR image-browser RPCs; the baseline is scanned on 4700 connection and updated on `SaveImage`.
- Added MQTT **Current frame** image entity under the main camera.
- Added port 4800 `get_current_img` reader with ZIP `raw_data` extraction, grayscale percentile stretch and max-1600-px PNG preview.
- Added **Refresh image** button when `allow_control: true`; preview also refreshes automatically after each completed `SaveImage`.
- Current-frame MQTT payloads are not retained to avoid large retained broker messages.

## 0.1.16

- Added Home Assistant **Tracking** switch on the mount device; writes use TCP 4400 `scope_set_track_state [true/false]` and confirm with `scope_get_track_state`.
- Changed manual EAF `− / +` moves to mirror the observed official-app sequence: `stop_focuser` immediately followed by absolute `move_focuser [target]`.
- EAF manual/AF commands now reopen the EAF first if its state is closed.
- AF now sends `stop_focuser` before `start_auto_focuse` and logs the currently known mount-tracking state.
- `stop_focuser` responses are logged as accepted EAF commands without spawning an unnecessary confirmation read.

## 0.1.15

- Add Home Assistant **Enter** button to reproduce the official ASIAIR app device-open phase.
- Learn/store main/guide camera names, camera IDs, focal length, EAF ID/state and EFW ID/state.
- Enter opens the selected main camera plus detected EAF/EFW and refreshes camera/peripheral telemetry immediately.
- Track `OpenEAF` / `OpenEFW` events and retain current peripheral open state in memory.
- Keep Enter manual; no automatic hardware initialization is performed on add-on startup.
## 0.1.14

- Added Home Assistant EAF controls when `allow_control: true`.
- Added editable **Set current position** number using `set_focuser_value [position]`; the range follows EAF `max_step`.
- Added bridge-local **Slow / Fast** selector. Slow uses ASIAIR `fine_step`; Fast uses `coarse_step` from `get_focuser_setting`.
- Added **−** and **+** MQTT buttons. They calculate an absolute target from the current/pending EAF position and send `move_focuser [target]`.
- Added **AF** MQTT button using ASIAIR's `start_auto_focuse` RPC.
- Added MQTT Discovery support for stateless `button` entities and runtime number limits.
- EAF commands are confirmed by re-reading `get_focuser_info`; repeated +/- presses accumulate against the pending target instead of collapsing to the same position.

## 0.1.13

- Added Home Assistant MQTT `select` control for EFW filter changes.
- Filter options are learned dynamically from `get_wheel_setting` and shown as physical 1-based slots, e.g. `1 — SHb`.
- EFW writes use `set_wheel_position` with the ASIAIR zero-based wheel index and are followed by `get_wheel_position` confirmation.
- Raw filter names and physical slot numbers are also accepted on the MQTT command topic for service/testing.

## 0.1.12

- Fixes port 4700 authentication: ASIAIR firmware 14.39 expects an RSA PKCS#1 v1.5 **SHA-1 signature** of the challenge using the private key from the official app; v0.1.11 incorrectly performed public-key encryption.
- Does not redistribute the third-party private key. The add-on can extract it locally from a user-provided `/share/asiair/*.apk`, or use `/share/asiair/asiair.pem`.
- Falls back to event-only monitoring when the signing key is unavailable or verification fails instead of reconnect-looping.
- Sends a `pi_is_verified` check after successful `verify_client`.

## 0.1.11
- Added the TCP 4700 `get_verify_str` → `verify_client` challenge/response handshake observed in the official ASIAIR app.
- Ordinary imager RPC reads are enabled after authentication instead of falling back to event-only monitoring.
- Corrected `get_control_value` calls to use `params: ["ControlName"]`.
- Re-enabled optional Cooler, Target temperature and Dew heater controls when `allow_control: true`.
- Filter names now come from `get_wheel_setting`; zero-based wheel positions are still shown as physical slots 1..N.
- EAF telemetry now uses `get_focuser_info`, which supplies position, temperature, state, serial number, model and firmware.
- Added `Imager authenticated`, focuser state and focuser temperature entities.
- Added a lightweight 5 s camera/EFW/EAF state refresh so manual changes made in the ASIAIR app appear in Home Assistant quickly.
- Corrected camera temperature scaling for `get_control_value("Temperature")` (tenths of °C).

## 0.1.10
- Added adaptive EAF position polling using a separate short-lived TCP 4700 probe.
- The probe mirrors observed ASIAIR app traffic: `get_focuser_position` with `params: {"ret_obj": true}`.
- If the probe succeeds, focuser position refreshes every 2 s. If unsupported, it backs off to 30 s and avoids log spam.
- Existing `FocuserMove` event handling remains as a secondary path.

## 0.1.9
- Disabled camera control entities: current tested ASIAIR firmware does not acknowledge `set_control_value` from this client, even with the official app closed.
- Existing retained MQTT Discovery control entities are removed automatically on startup.
- Added `Mount connected` diagnostic entity.
- When no mount is attached, port 4400 now probes only one scope method per poll instead of generating six expected warnings.
- Stale mount coordinates/states are cleared when the mount is known to be disconnected.

## 0.1.8
- Experimental camera controls and filter wheel improvements.
