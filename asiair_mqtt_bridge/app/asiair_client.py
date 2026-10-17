import base64
import io
import json
import logging
import re
import socket
import struct
import subprocess
import zipfile
import zlib
from pathlib import Path
import threading
import time
from collections import deque
from datetime import datetime
from typing import Any, Deque, Dict, Optional, Tuple

from mqtt_bridge import MqttBridge

LOG = logging.getLogger(__name__)

DC_OUTPUT_TYPE_OPTIONS = ["Mount", "Camera", "Focuser", "Dew Heater", "Flat Panel", "Other"]
DC_OUTPUT_TYPE_TO_RPC = {
    "Mount": "telescope",
    "Camera": "camera",
    "Focuser": "focuser",
    "Dew Heater": "dew_heater",
    "Flat Panel": "flat_panel",
    "Other": "other",
}
DC_OUTPUT_RPC_TO_TYPE = {
    "telescope": "Mount",
    "mount": "Mount",
    "camera": "Camera",
    "focuser": "Focuser",
    "dew_heater": "Dew Heater",
    "heater": "Dew Heater",
    "flat_panel": "Flat Panel",
    "flatpanel": "Flat Panel",
    "other": "Other",
}

# Values below describe the *current* observing session or live peripheral state.
# They must never survive a reconnect as if they were fresh telemetry.
VOLATILE_4700_KEYS = (
    "app_page",
    "target", "sequence_state", "frame_type", "frame_current",
    "frame_completed", "frame_total", "sequence_progress", "capture_working",
    "capture_command_state",
    "camera_state", "camera_temperature", "camera_target_temperature",
    "cooler_power", "cooler_enabled", "dew_heater_enabled", "gain",
    "exposure_seconds", "exposure_state",
    "guide_state", "guide_snr", "guide_star_mass", "guide_ra_error",
    "guide_dec_error", "guide_total_error",
    "focuser_position", "focuser_state", "focuser_temperature", "filter_position", "filter_name", "filter_selection",
)


def _png_grayscale(width: int, height: int, pixels: bytes) -> bytes:
    """Encode an 8-bit grayscale buffer as PNG using only the stdlib."""
    if width <= 0 or height <= 0 or len(pixels) != width * height:
        raise ValueError("PNG pixel buffer size does not match dimensions")

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    rows = bytearray()
    for y in range(height):
        start = y * width
        rows.append(0)
        rows.extend(pixels[start:start + width])
    return b"".join((
        b"\x89PNG\r\n\x1a\n",
        chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)),
        chunk(b"IDAT", zlib.compress(bytes(rows), level=6)),
        chunk(b"IEND", b""),
    ))


def _hist_percentile(histogram: list[int], total: int, fraction: float) -> int:
    threshold = max(1, int(total * fraction))
    running = 0
    for value, count in enumerate(histogram):
        running += count
        if running >= threshold:
            return value
    return 255


def _detect_high_byte_offset(raw_data: bytes) -> int:
    """Guess which byte carries the useful 16-bit signal (ASIAIR varies by path)."""
    pixel_count = len(raw_data) // 2
    if pixel_count <= 0:
        return 1
    sample_step = max(1, pixel_count // 100000)
    even = [0] * 256
    odd = [0] * 256
    for pixel in range(0, pixel_count, sample_step):
        base = pixel * 2
        even[raw_data[base]] += 1
        odd[raw_data[base + 1]] += 1

    def score(hist: list[int]) -> float:
        mean = sum(hist) / 256.0
        return sum((count - mean) ** 2 for count in hist)

    return 1 if score(odd) > score(even) else 0


def _build_preview_png(raw_data: bytes, width: int, height: int, bytes_per_pixel: int, max_edge: int = 1600) -> tuple[bytes, int, int]:
    """Downsample ASIAIR raw_data, percentile-stretch it, and return a PNG preview."""
    if bytes_per_pixel not in (1, 2):
        raise ValueError(f"Unsupported image bytes-per-pixel: {bytes_per_pixel}")
    longest = max(width, height)
    step = max(1, (longest + max_edge - 1) // max_edge)
    out_width = max(1, (width + step - 1) // step)
    out_height = max(1, (height + step - 1) // step)
    high_byte_offset = _detect_high_byte_offset(raw_data) if bytes_per_pixel == 2 else 0

    values = bytearray(out_width * out_height)
    histogram = [0] * 256
    out_index = 0
    for y in range(0, height, step):
        row = y * width
        for x in range(0, width, step):
            src = (row + x) * bytes_per_pixel + high_byte_offset
            value = raw_data[src]
            values[out_index] = value
            histogram[value] += 1
            out_index += 1

    # Guard against round-off if dimensions are not exact multiples of step.
    if out_index != len(values):
        values = values[:out_index]
        out_height = max(1, out_index // out_width)

    total = len(values)
    low = _hist_percentile(histogram, total, 0.01)
    high = _hist_percentile(histogram, total, 0.995)
    if high <= low:
        present = [i for i, count in enumerate(histogram) if count]
        low = present[0] if present else 0
        high = present[-1] if present else 255
    if high <= low:
        stretched = bytes(255 if value >= high else 0 for value in values)
    else:
        scale = 255.0 / (high - low)
        lut = [0] * 256
        for value in range(256):
            lut[value] = 0 if value <= low else 255 if value >= high else int((value - low) * scale)
        stretched = bytes(lut[value] for value in values)
    return _png_grayscale(out_width, out_height, stretched), out_width, out_height


class AsiairChannel(threading.Thread):
    def __init__(self, owner: "AsiairDevice", port: int) -> None:
        super().__init__(name=f"{owner.node_id}:{port}", daemon=True)
        self.owner = owner
        self.port = port
        self._sock: Optional[socket.socket] = None
        self._buffer = b""
        self._request_id = 2 if port == 4700 else 1
        self._pending: Dict[int, tuple[str, Optional[str], float]] = {}
        self._queue: Deque[Tuple[str, Optional[Any], Optional[str]]] = deque()
        self._rpc_timeout = 2.0
        self._rpc_authenticated = port != 4700

    def run(self) -> None:
        delay = 3.0
        while not self.owner.stop_event.is_set():
            try:
                LOG.info("[%s] Connecting to %s:%s", self.owner.name, self.owner.host, self.port)
                self._sock = socket.create_connection((self.owner.host, self.port), timeout=8)
                self._sock.settimeout(1.0)
                if self.port == 4700:
                    if not self._authenticate_4700():
                        raise ConnectionError("ASIAIR imager authentication failed")
                LOG.info("[%s] Connected to ASIAIR port %s", self.owner.name, self.port)
                self.owner.on_channel_status(self.port, True)
                delay = 3.0
                self._connected_loop()
            except Exception as exc:
                if not self.owner.stop_event.is_set():
                    LOG.warning("[%s] Port %s disconnected: %s", self.owner.name, self.port, exc)
            finally:
                self.owner.on_channel_status(self.port, False)
                self._pending.clear()
                self._queue.clear()
                self._buffer = b""
                if self._sock:
                    try:
                        self._sock.close()
                    except Exception:
                        pass
                self._sock = None
            if not self.owner.stop_event.wait(delay):
                delay = min(delay * 1.5, 60.0)

    def stop(self) -> None:
        if self._sock:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass

    # Firmware 14.39 authenticates port 4700 by asking the client to SIGN a
    # random challenge with the private RSA key embedded in the official app.
    # We deliberately do not ship that third-party private key. Instead the
    # add-on can use a user-provided PEM or extract it locally from a user-
    # provided ASIAIR APK in /share/asiair/.
    _AUTH_PEM_PATHS = (
        Path("/data/asiair_auth.pem"),
        Path("/share/asiair/seestar.pem"),
        Path("/share/asiair/asiair.pem"),
    )
    _AUTH_APK_PATHS = (
        Path("/share/asiair/ASIAIR.apk"),
        Path("/share/asiair/asiair.apk"),
    )

    @classmethod
    def _extract_private_key_from_apk(cls, apk_path: Path, output_path: Path) -> Optional[Path]:
        patterns = (
            re.compile(rb"-----BEGIN PRIVATE KEY-----.*?-----END PRIVATE KEY-----", re.DOTALL),
            re.compile(rb"-----BEGIN RSA PRIVATE KEY-----.*?-----END RSA PRIVATE KEY-----", re.DOTALL),
        )
        try:
            with zipfile.ZipFile(apk_path, "r") as zf:
                names = zf.namelist()
                # The key observed by Seestar-Py lives in libopenssllib.so.
                # Scan that first, then other native libraries as a fallback.
                ordered = [n for n in names if n.endswith("/libopenssllib.so")]
                ordered += [n for n in names if n.endswith(".so") and n not in ordered]
                for member in ordered:
                    try:
                        blob = zf.read(member)
                    except Exception:
                        continue
                    for pattern in patterns:
                        match = pattern.search(blob)
                        if match:
                            output_path.parent.mkdir(parents=True, exist_ok=True)
                            output_path.write_bytes(match.group(0).replace(b"\r\n", b"\n") + b"\n")
                            try:
                                output_path.chmod(0o600)
                            except Exception:
                                pass
                            LOG.info("Extracted ASIAIR authentication key locally from %s (%s)", apk_path, member)
                            return output_path
        except Exception as exc:
            LOG.warning("Could not inspect ASIAIR APK %s: %s", apk_path, exc)
        return None

    @classmethod
    def _find_auth_private_key(cls) -> Optional[Path]:
        for path in cls._AUTH_PEM_PATHS:
            if path.is_file():
                return path
        apk_candidates = list(cls._AUTH_APK_PATHS)
        share_dir = Path("/share/asiair")
        if share_dir.is_dir():
            for apk_path in sorted(share_dir.glob("*.apk")):
                if apk_path not in apk_candidates:
                    apk_candidates.append(apk_path)
        for apk_path in apk_candidates:
            if apk_path.is_file():
                extracted = cls._extract_private_key_from_apk(apk_path, Path("/data/asiair_auth.pem"))
                if extracted:
                    return extracted
        return None

    @staticmethod
    def _sign_verify_challenge(private_key: Path, challenge: str) -> str:
        # The official client performs RSA PKCS#1 v1.5 SHA-1 signing, not
        # RSA public-key encryption. `openssl dgst` emits the raw signature.
        proc = subprocess.run(
            ["openssl", "dgst", "-sha1", "-sign", str(private_key)],
            input=challenge.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout:
            err = proc.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"OpenSSL signing failed: {err or proc.returncode}")
        return base64.b64encode(proc.stdout).decode("ascii")

    def _sync_request(self, request_id: int, method: str, params: Optional[Any] = None, timeout: float = 3.0) -> Optional[Dict[str, Any]]:
        """Send one RPC before the normal async queue starts and wait for its reply."""
        if not self._sock:
            return None
        payload: Dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        raw = (json.dumps(payload, separators=(",", ":")) + "\r\n").encode("utf-8")
        LOG.debug("[%s] AUTH TX %s port=%s id=%s params=%r", self.owner.name, method, self.port, request_id, params)
        self._sock.sendall(raw)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            while b"\n" in self._buffer:
                line, self._buffer = self._buffer.split(b"\n", 1)
                line = line.rstrip(b"\r")
                if not line:
                    continue
                try:
                    msg = json.loads(line.decode("utf-8", errors="replace"))
                except Exception:
                    continue
                if msg.get("Event"):
                    self.owner.handle_event(str(msg.get("Event")), msg, self.port)
                    continue
                if msg.get("id") == request_id:
                    LOG.debug("[%s] AUTH RX %s port=%s id=%s response=%r", self.owner.name, method, self.port, request_id, msg)
                    return msg

            try:
                chunk = self._sock.recv(65536)
                if not chunk:
                    return None
                self._buffer += chunk
            except socket.timeout:
                continue
        return None

    def _authenticate_4700(self) -> bool:
        """Perform the challenge/response handshake used by the official ASIAIR application."""
        challenge_reply = self._sync_request(0, "get_verify_str", timeout=3.0)
        if challenge_reply is None:
            LOG.warning("[%s] No reply to get_verify_str on port 4700", self.owner.name)
            return False

        code = challenge_reply.get("code", 0)
        if code == 103:
            LOG.info("[%s] ASIAIR port 4700 does not require verification", self.owner.name)
            self._rpc_authenticated = True
            self._request_id = 1
            return True
        if code not in (0, None):
            LOG.warning("[%s] get_verify_str failed: %r", self.owner.name, challenge_reply)
            return False

        result = challenge_reply.get("result")
        challenge = result.get("str") if isinstance(result, dict) else None
        if not challenge:
            LOG.warning("[%s] get_verify_str returned no challenge: %r", self.owner.name, challenge_reply)
            return False

        private_key = self._find_auth_private_key()
        if private_key is None:
            LOG.warning(
                "[%s] ASIAIR port 4700 requires signed authentication, but no private key is available. "
                "Continuing in event-only mode. Put the official ASIAIR APK at /share/asiair/ASIAIR.apk "
                "or a PEM key at /share/asiair/asiair.pem, then restart the add-on.",
                self.owner.name,
            )
            self._rpc_authenticated = False
            self._request_id = 2
            self.owner.pub("imager_verified", False, force=True)
            return True

        try:
            sign = self._sign_verify_challenge(private_key, str(challenge))
        except Exception as exc:
            LOG.warning("[%s] Could not sign ASIAIR verification challenge: %s", self.owner.name, exc)
            self._rpc_authenticated = False
            self._request_id = 2
            self.owner.pub("imager_verified", False, force=True)
            return True

        verify_params = {"sign": sign, "data": str(challenge)}
        verify_reply = self._sync_request(1, "verify_client", verify_params, timeout=3.0)
        if not verify_reply or verify_reply.get("code", 0) not in (0, None):
            LOG.warning("[%s] verify_client failed: %r", self.owner.name, verify_reply)
            self._rpc_authenticated = False
            self._request_id = 2
            self.owner.pub("imager_verified", False, force=True)
            return True

        result = verify_reply.get("result")
        if result not in (0, True, None):
            LOG.warning("[%s] verify_client returned unexpected result: %r", self.owner.name, result)
            self._rpc_authenticated = False
            self._request_id = 2
            self.owner.pub("imager_verified", False, force=True)
            return True

        # The official app later checks pi_is_verified. Do the same once here
        # while the connection is still in synchronous handshake mode.
        verified_reply = self._sync_request(2, "pi_is_verified", timeout=3.0)
        if verified_reply and verified_reply.get("code", 0) in (0, None):
            LOG.debug("[%s] AUTH RX pi_is_verified response=%r", self.owner.name, verified_reply)

        self._rpc_authenticated = True
        self._request_id = 3
        LOG.info("[%s] ASIAIR port 4700 authenticated", self.owner.name)
        self.owner.pub("imager_verified", True, force=True)
        return True

    def _send(self, method: str, params: Optional[Any] = None, tag: Optional[str] = None) -> None:
        if not self._sock:
            return
        request_id = self._request_id
        self._request_id += 1

        # Match the wire format used by the current ASIAIR app / asiair-dashboard:
        # omit `params` entirely for no-argument methods.  Some current firmware
        # accepts test_connection with [], but silently ignores other reads when
        # an empty params array is present.
        payload: Dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            payload["params"] = params

        raw = (json.dumps(payload, separators=(",", ":")) + "\r\n").encode("utf-8")
        LOG.debug("[%s] TX %s port=%s id=%s params=%r", self.owner.name, method, self.port, request_id, params)
        self._sock.sendall(raw)
        self._pending[request_id] = (method, tag, time.monotonic())
        if self.port == 4700 and method in ("start_exposure", "stop_exposure"):
            action = "start" if method == "start_exposure" else "pause"
            # Transport confirmation, NOT ASIAIR acceptance.
            LOG.warning("[%s] CAPTURE RPC SENT to %s:%s method=%s params=%r id=%s tag=%s",
                        self.owner.name, self.owner.host, self.port, method, params, request_id, tag)
            self.owner.pub("capture_command_state", f"{action}_sent_waiting_reply", force=True)

    def _enqueue(self, method: str, params: Optional[Any] = None, tag: Optional[str] = None) -> None:
        self._queue.append((method, params, tag))

    def enqueue_command(self, method: str, params: Optional[Any] = None, tag: Optional[str] = None) -> None:
        """Queue a user-requested command ahead of background probes."""
        self._queue.appendleft((method, params, tag))
        LOG.info("[%s] Queued ASIAIR command %s params=%r", self.owner.name, method, params)

    def enqueue_command_sequence(self, commands: list[Tuple[str, Optional[Any], Optional[str]]]) -> None:
        """Queue an ordered user-requested RPC sequence ahead of background probes."""
        for method, params, tag in reversed(commands):
            self._queue.appendleft((method, params, tag))
        LOG.info("[%s] Queued ASIAIR command sequence (%s RPCs)", self.owner.name, len(commands))

    def _enqueue_poll(self, *, initial: bool = False) -> None:
        if self.port == 4700:
            if not self._rpc_authenticated:
                return
            if initial:
                for method in (
                    "get_svr_version",
                    "pi_get_info",
                    "get_app_state",
                    "get_app_setting",
                    "get_connected_cameras",
                    "get_camera_state",
                    "get_focal_length",
                    "get_controls",
                ):
                    self._enqueue(method)
                for control in (
                    "Gain", "Exposure", "CoolerOn", "Temperature",
                    "TargetTemp", "CoolPowerPerc", "AntiDewHeater",
                ):
                    self._enqueue("get_control_value", [control, True], tag=control)
                for method in (
                    "get_connected_wheels",
                    "get_wheel_state",
                    "get_wheel_setting",
                    "get_wheel_position",
                    "get_connected_focuser",
                    "get_focuser_state",
                    "get_focuser_info",
                    "get_focuser_setting",
                    "pi_station_state",
                    "pi_output_get2",
                    "get_power_supply",
                    "get_image_save_path",
                    "get_disk_volume",
                ):
                    self._enqueue(method)
                # Read-only recursive FITS count. Keep it at the end of the
                # startup queue so camera/EAF/EFW telemetry is available first.
                if self.owner._prepare_image_scan():
                    self._enqueue(
                        "get_img_file_page_number",
                        [""],
                        tag="scan:page_count",
                    )
            else:
                for method in (
                    "get_app_state", "get_camera_state", "get_focal_length",
                    "pi_get_info", "pi_station_state", "get_disk_volume",
                ):
                    self._enqueue(method)
                if self.owner._has_dc_outputs is not False:
                    self._enqueue("pi_output_get2")
                if self.owner._has_power_monitor is not False:
                    self._enqueue("get_power_supply")
                for control in (
                    "Gain", "Exposure", "CoolerOn", "TargetTemp",
                    "CoolPowerPerc", "AntiDewHeater",
                ):
                    self._enqueue("get_control_value", [control, True], tag=control)
                self._enqueue("get_wheel_setting")
                self._enqueue("get_wheel_position")
                self._enqueue("get_focuser_info")
                self._enqueue("get_focuser_setting")
        elif self.port == 4400:
            if self.owner._mount_missing:
                self._enqueue("scope_get_horiz_coord")
            else:
                for method in (
                    "scope_get_horiz_coord",
                    "scope_get_ra_dec",
                    "scope_get_pierside",
                    "scope_get_track_mode",
                    "scope_get_track_state",
                    "scope_is_moving",
                ):
                    self._enqueue(method)

    def _enqueue_fast_4700_poll(self) -> None:
        if not self._rpc_authenticated:
            return
        # CameraControlChange carries no changed value on this firmware, and EAF
        # movement does not emit FocuserMove. Poll the exact read methods used by
        # the official app so HA follows manual changes within a few seconds.
        for control in ("CoolerOn", "TargetTemp", "CoolPowerPerc", "AntiDewHeater"):
            self._enqueue("get_control_value", [control, True], tag=control)
        self._enqueue("get_wheel_position")
        self._enqueue("get_focuser_info")
        if self.owner._has_dc_outputs is True:
            self._enqueue("pi_output_get2")
        if self.owner._has_power_monitor is True:
            self._enqueue("get_power_supply")

    def _send_next_queued(self) -> None:
        if self._pending:
            return
        while self._queue:
            method, params, tag = self._queue.popleft()
            if method == "get_control_value" and tag in self.owner._unsupported_camera_controls:
                continue
            self._send(method, params, tag)
            return

    def _connected_loop(self) -> None:
        self._queue.clear()
        self._pending.clear()
        self._enqueue_poll(initial=True)
        self._send_next_queued()

        next_poll = time.monotonic() + self.owner.poll_interval
        next_fast_poll = time.monotonic() + 5.0 if self.port == 4700 else float("inf")
        next_keepalive = time.monotonic() + self.owner.keepalive_interval

        while not self.owner.stop_event.is_set():
            now = time.monotonic()
            if self.port == 4400:
                self.owner._service_deferred_park(now)
            if self.port == 4400 and self.owner._mount_park_requested_at is not None:
                if now - self.owner._mount_park_requested_at > 300.0:
                    self.owner._mount_park_requested_at = None
                    self.owner.pub("mount_home_state", "completion_timeout_unconfirmed", force=True)
                    LOG.warning("[%s] Mount park not confirmed within 300 s; no automatic tracking change", self.owner.name)

            # Expire a request that this firmware silently ignored, then move
            # on to the next queued read.  Local replies normally arrive in a
            # few milliseconds, so 2 s is deliberately generous.
            expired = []
            for req_id, (method, tag, sent_at) in list(self._pending.items()):
                if now - sent_at >= self._rpc_timeout:
                    LOG.warning(
                        "[%s] No reply to %s on port %s (id=%s tag=%s) after %.1fs",
                        self.owner.name, method, self.port, req_id, tag, self._rpc_timeout,
                    )
                    expired.append(req_id)
            for req_id in expired:
                expired_method, expired_tag, _ = self._pending.pop(req_id)
                if expired_method == "scope_park":
                    # A completion event may have arrived before the RPC reply.
                    # In that case, do not overwrite confirmed park completion.
                    if self.owner._mount_park_requested_at is not None:
                        self.owner._mount_park_requested_at = None
                        self.owner.pub("mount_home_state", "rpc_timeout_unconfirmed", force=True)
                elif expired_method in ("start_exposure", "stop_exposure"):
                    self.owner._clear_capture_confirmation()
                    self.owner.pub("capture_command_state", "rpc_timeout_unconfirmed", force=True)
            if expired:
                self._send_next_queued()

            # v0.1.23: MQTT commands are added by another thread. Continuous
            # incoming ASIAIR events may prevent socket.recv() from timing out;
            # v0.1.22 could then leave a new command queued forever.
            self._send_next_queued()

            # While an active capture command awaits device-readback, poll its
            # actual state every 3 s instead of relying on a generic 30 s poll.
            # Never mark the command confirmed on RPC acceptance alone.
            if (self.port == 4700 and self.owner._capture_command_accepted_at is not None
                    and not self._pending and not self._queue):
                if now - self.owner._capture_command_accepted_at > 360.0:
                    LOG.warning("[%s] Capture %s readback not confirmed within 360s",
                                self.owner.name, self.owner._capture_command_action)
                    self.owner._clear_capture_confirmation()
                    self.owner.pub("capture_command_state", "confirmation_timeout_unconfirmed", force=True)
                elif (self.owner._capture_confirmation_next_poll is not None
                      and now >= self.owner._capture_confirmation_next_poll):
                    self.owner._capture_confirmation_next_poll = now + 3.0
                    self._enqueue("get_app_state", tag="cmd:Capture:confirmation_poll")
                    self._send_next_queued()

            if now >= next_fast_poll and not self._pending and not self._queue:
                self._enqueue_fast_4700_poll()
                next_fast_poll = now + 5.0
                self._send_next_queued()

            # Only start a new poll when the previous poll has drained.
            if now >= next_poll and not self._pending and not self._queue:
                self._enqueue_poll(initial=False)
                next_poll = now + self.owner.poll_interval
                self._send_next_queued()

            # Keepalive is also serialized; never interleave it with an active
            # read request.
            if now >= next_keepalive and not self._pending and not self._queue:
                self._send("test_connection")
                next_keepalive = now + self.owner.keepalive_interval

            try:
                chunk = self._sock.recv(65536) if self._sock else b""
                if not chunk:
                    raise ConnectionError("EOF")
                self._buffer += chunk
                if len(self._buffer) > 4 * 1024 * 1024:
                    raise RuntimeError("ASIAIR JSON receive buffer exceeded 4 MiB")
                self._drain_lines()
            except socket.timeout:
                self._send_next_queued()
                continue

    def _drain_lines(self) -> None:
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            line = line.rstrip(b"\r")
            if not line:
                continue
            try:
                try:
                    text = line.decode("utf-8")
                except UnicodeDecodeError:
                    text = line.decode("iso-8859-1", errors="replace")
                message = json.loads(text)
            except Exception as exc:
                LOG.debug("[%s] Could not parse ASIAIR message on %s: %s", self.owner.name, self.port, exc)
                continue
            self._handle_message(message)

    def _handle_message(self, message: Dict[str, Any]) -> None:
        event = message.get("Event")
        if event:
            # Keep DEBUG useful without drowning in high-rate health/guiding
            # events.  Log every other event payload so we can extend the
            # reverse-engineered parser from real firmware traffic.
            if event not in {"PiStatus", "Station", "GuideStep", "Temperature"}:
                LOG.debug("[%s] EVENT %s port=%s payload=%r", self.owner.name, event, self.port, message)
            self.owner.handle_event(event, message, self.port)

        request_id = message.get("id")
        if request_id is not None and ("result" in message or "error" in message) and request_id not in self._pending:
            LOG.debug("[%s] RX unmatched response port=%s id=%r method=%r response=%r",
                      self.owner.name, self.port, request_id, message.get("method"), message)
        if request_id in self._pending:
            method, tag, _sent_at = self._pending.pop(request_id)
            code = message.get("code", 0)
            error = message.get("error")
            if code not in (0, None) or error not in (None, "", 0, False):
                handled = self.owner.handle_rpc_error(method, code, error, self.port, tag=tag)
                if handled:
                    LOG.debug(
                        "[%s] ASIAIR %s unavailable on port %s: code=%r error=%r",
                        self.owner.name, method, self.port, code, error,
                    )
                else:
                    LOG.warning(
                        "[%s] ASIAIR %s failed on port %s: code=%r error=%r response=%r",
                        self.owner.name, method, self.port, code, error, message,
                    )
                self._send_next_queued()
                return
            if "result" in message:
                LOG.debug("[%s] RX %s port=%s tag=%s result=%r", self.owner.name, method, self.port, tag, message["result"])
                self.owner.handle_response(method, tag, message["result"], self.port)
            elif ("code" in message and code in (0, None) and method in
                  ("start_exposure", "stop_exposure", "scope_park", "scope_set_track_state")):
                # Valid write ACKs can have code=0 without a `result` member.
                LOG.info("[%s] ASIAIR write ACK without result: method=%s reply=%r",
                         self.owner.name, method, message)
                self.owner.handle_response(method, tag, None, self.port)
            else:
                LOG.warning(
                    "[%s] ASIAIR %s returned no result on port %s: %r",
                    self.owner.name, method, self.port, message,
                )
            self._send_next_queued()


class AsiairDevice:
    def __init__(
        self,
        node_id: str,
        name: str,
        host: str,
        bridge: MqttBridge,
        poll_interval: int,
        keepalive_interval: int,
    ) -> None:
        self.node_id = node_id
        self.name = name
        self.host = host
        self.bridge = bridge
        self.poll_interval = poll_interval
        self.keepalive_interval = keepalive_interval
        self.stop_event = threading.Event()
        self.channels = [AsiairChannel(self, 4700), AsiairChannel(self, 4400)]
        self._wheel_names: list[str] = []
        self._wheel_position: Optional[int] = None
        self._wheel_id: Optional[int] = None
        self._wheel_state: Optional[str] = None
        self._focuser_position: Optional[int] = None
        self._focuser_id: Optional[int] = None
        self._focuser_state: Optional[str] = None
        self._main_camera_name: Optional[str] = None
        self._guide_camera_name: Optional[str] = None
        self._camera_ids_by_name: Dict[str, int] = {}
        self._main_camera_id: Optional[int] = None
        self._camera_state: Optional[str] = None
        self._focal_length: Optional[int] = None
        self._focuser_pending_target: Optional[int] = None
        self._focuser_max_step: Optional[int] = None
        self._focuser_fine_step = 10
        self._focuser_coarse_step = 30
        self._focuser_step_mode = "Slow"
        self._sequence_active = False
        self._capture_working: Optional[bool] = None
        self._seen_event_types: set[str] = set()
        self._mount_missing = False
        self._mount_tracking: Optional[bool] = None
        self._mount_park_requested_at: Optional[float] = None
        # Deferred Go Home is deliberately NOT an automatic abort/capture pause.
        # The operator (or HA automation) must stop the session first.  We only
        # park after two fresh idle app-state readbacks and a fresh stationary
        # mount readback; stale MQTT/event values cannot authorize movement.
        self._deferred_park_started_at: Optional[float] = None
        self._deferred_park_last_capture_probe: float = 0.0
        self._deferred_park_last_mount_probe: float = 0.0
        self._deferred_park_last_idle_read: float = 0.0
        self._deferred_park_idle_read_count: int = 0
        self._deferred_park_mount_still_at: Optional[float] = None
        # AutoGoto / ScopeGoto may emit ``working`` without a terminal event.
        # Never let a single orphaned event latch the safety gate forever.
        # A stale flag can be cleared ONLY after the source has been quiet for
        # a substantial interval *and* fresh independent capture/mount idle
        # reads confirm the equipment is no longer operating.
        self._goto_active_by_event: Dict[str, bool] = {}
        self._goto_last_active_at: Dict[str, float] = {}
        self._goto_stale_quiet_seconds = 30.0
        self._unsupported_camera_controls: set[str] = set()
        # A user command is only CONFIRMED by a fresh get_app_state readback.
        # RPC code=0 is merely acceptance, not the physical capture state.
        self._capture_command_action: Optional[str] = None
        self._capture_command_expected: Optional[bool] = None
        self._capture_command_accepted_at: Optional[float] = None
        self._capture_confirmation_next_poll: Optional[float] = None

        # Capability-driven ASIAIR power profile. None means not probed yet.
        # Pro exposes the four DC outputs; Plus additionally exposes supply
        # telemetry; Mini exposes neither. We key the HA UI from actual RPC
        # capability instead of asking the user to choose a model.
        self._has_dc_outputs: Optional[bool] = None
        self._has_power_monitor: Optional[bool] = None
        self._dc_ports: list[Dict[str, Any]] = []

        # Storage/image browser state. The page browser is stateful on the
        # ASIAIR connection, so the scan deliberately processes one directory
        # and one page at a time.
        self._image_scan_active = False
        self._image_scan_queue: Deque[str] = deque()
        self._image_scan_seen: set[str] = set()
        self._image_scan_current_dir = ""
        self._image_scan_page = 0
        self._image_scan_pages = 0
        self._image_scan_count = 0
        self._image_file_count: Optional[int] = None

        # Port 4800 image fetches run outside the JSON-RPC thread because a
        # full ASI2600 frame can be tens of megabytes.
        self._image_fetch_lock = threading.Lock()
        self._last_image_fetch = 0.0

        self.bridge.register_command_handler(self.node_id, self.handle_command)
        self.bridge.set_select_options(self.node_id, "focuser_step_mode_control", ["Slow", "Fast"])
        self.pub("device_name", self.name, force=True)
        self.pub("focuser_step_mode", self._focuser_step_mode, force=True)

    def start(self) -> None:
        self.bridge.publish_device_availability(self.node_id, False)
        self.bridge.publish_port4400_availability(self.node_id, False)
        for channel in self.channels:
            channel.start()

    def stop(self) -> None:
        self.stop_event.set()
        for channel in self.channels:
            channel.stop()
        for channel in self.channels:
            channel.join(timeout=5)
        self.bridge.publish_port4400_availability(self.node_id, False)
        self.bridge.publish_device_availability(self.node_id, False)

    def _channel(self, port: int) -> Optional[AsiairChannel]:
        return next((channel for channel in self.channels if channel.port == port), None)

    @staticmethod
    def _parse_switch_payload(payload: str) -> bool:
        value = str(payload).strip().lower()
        if value in ("1", "true", "on", "yes", "enable", "enabled"):
            return True
        if value in ("0", "false", "off", "no", "disable", "disabled"):
            return False
        raise ValueError(f"Unsupported switch value: {payload!r}")

    @staticmethod
    def _friendly_dc_output_type(raw_type: Any) -> str:
        raw = str(raw_type or "other").strip().lower()
        return DC_OUTPUT_RPC_TO_TYPE.get(raw, "Other")

    @staticmethod
    def _rpc_dc_output_type(label: str) -> str:
        requested = str(label).strip()
        for option, rpc_value in DC_OUTPUT_TYPE_TO_RPC.items():
            if requested.casefold() == option.casefold():
                return rpc_value
        # Accept the raw protocol values too, which is useful for service/MQTT
        # calls and future firmware additions not yet represented in the HA UI.
        raw = requested.casefold().replace(" ", "_")
        if raw in DC_OUTPUT_RPC_TO_TYPE:
            return "telescope" if raw == "mount" else raw
        raise ValueError(f"Unsupported DC output type: {label!r}")

    def _enable_dc_type_selects(self) -> None:
        if self._has_dc_outputs is not True:
            return
        for idx in range(min(4, len(self._dc_ports))):
            self.bridge.set_select_options(
                self.node_id,
                f"dc_output_{idx + 1}_type_control",
                DC_OUTPUT_TYPE_OPTIONS,
            )

    def _update_hardware_profile(self) -> None:
        """Infer ASIAIR variant from capabilities, not a user-supplied selector."""
        variant = None
        if self._has_dc_outputs is True and self._has_power_monitor is True:
            variant = "ASIAIR Plus"
        elif self._has_dc_outputs is True and self._has_power_monitor is False:
            variant = "ASIAIR Pro"
        elif self._has_dc_outputs is False and self._has_power_monitor is False:
            variant = "ASIAIR Mini"
        elif self._has_dc_outputs is False and self._has_power_monitor is True:
            # Not expected for current ZWO variants, but keep capability-first
            # behavior rather than lying about a product model.
            variant = "ASIAIR (power monitor)"
        if variant is None:
            return
        self.pub("hardware_variant", variant, force=True)
        self.bridge.update_metadata(self.node_id, "root", model=variant)
        
    @staticmethod
    def _power_pair(item: Any) -> tuple[Optional[float], Optional[float]]:
        voltage = current = None
        if isinstance(item, dict):
            voltage = item.get("voltage", item.get("volt", item.get("v")))
            current = item.get("current", item.get("amp", item.get("a")))
        elif isinstance(item, (list, tuple)):
            if len(item) > 0:
                voltage = item[0]
            if len(item) > 1:
                current = item[1]
        try:
            voltage = float(voltage) if voltage is not None else None
            if voltage is not None and abs(voltage) > 100.0:
                voltage /= 1000.0
        except (TypeError, ValueError):
            voltage = None
        try:
            current = float(current) if current is not None else None
            if current is not None and abs(current) > 50.0:
                current /= 1000.0
        except (TypeError, ValueError):
            current = None
        return voltage, current

    def _publish_power_supply(self, result: Any) -> None:
        """Publish Plus input totals and per-output voltages from get_power_supply."""
        pairs: list[tuple[Optional[float], Optional[float]]] = []
        if isinstance(result, list):
            pairs = [self._power_pair(item) for item in result]
        elif isinstance(result, dict):
            outputs = result.get("outputs") or result.get("ports")
            if isinstance(outputs, list):
                pairs.extend(self._power_pair(item) for item in outputs)
            input_obj = result.get("input") or result.get("input_supply") or result.get("supply")
            if input_obj is not None:
                pairs.append(self._power_pair(input_obj))
            elif not pairs:
                pairs.append(self._power_pair(result))

        usable = [(v, a) for v, a in pairs if v is not None or a is not None]
        if not usable:
            self._has_power_monitor = False
            self._update_hardware_profile()
            return

        self._has_power_monitor = True
        self._update_hardware_profile()

        # On Plus captures the last lane is the input/total supply, while the
        # preceding lanes correspond to the four DC outputs. This also degrades
        # safely if a firmware returns input-only telemetry.
        input_voltage, input_current = usable[-1]
        if input_voltage is not None:
            self.pub("input_voltage", round(input_voltage, 3))
        if input_current is not None:
            self.pub("input_current", round(input_current, 3))
        if input_voltage is not None and input_current is not None:
            self.pub("input_power", round(input_voltage * input_current, 2))

        output_pairs = pairs[:-1] if len(pairs) > 1 else []
        for idx, (voltage, _current) in enumerate(output_pairs[:4]):
            if voltage is not None:
                self.pub(f"dc_output_{idx + 1}_voltage", round(voltage, 3))

    @staticmethod
    def _mb_to_gb(value: Any) -> Optional[float]:
        try:
            return round(float(value) / 1024.0, 3)
        except (TypeError, ValueError):
            return None

    def _publish_storage_numbers(self, total_mb: Any, free_mb: Any, disk_size_mb: Any = None) -> None:
        try:
            total = float(total_mb)
            free = float(free_mb)
        except (TypeError, ValueError):
            return
        used = max(0.0, total - free)
        self.pub("storage_total", self._mb_to_gb(total))
        self.pub("storage_free", self._mb_to_gb(free))
        self.pub("storage_used", self._mb_to_gb(used))
        if total > 0:
            self.pub("storage_used_percent", round(used * 100.0 / total, 1))
        if disk_size_mb is not None:
            self.pub("storage_disk_size", self._mb_to_gb(disk_size_mb))

    def _prepare_image_scan(self) -> bool:
        """Reset the read-only FITS counter scan. Called on each 4700 reconnect."""
        self._image_scan_active = True
        self._image_scan_queue = deque()
        self._image_scan_seen = {""}
        self._image_scan_current_dir = ""
        self._image_scan_page = 0
        self._image_scan_pages = 0
        self._image_scan_count = 0
        return True

    def _queue_next_image_scan_dir(self, channel: AsiairChannel) -> None:
        while self._image_scan_queue:
            directory = self._image_scan_queue.popleft()
            normalized = str(directory).strip("/")
            if normalized in self._image_scan_seen:
                continue
            if len(self._image_scan_seen) >= 500:
                LOG.warning("[%s] FITS scan stopped after 500 directories", self.name)
                break
            self._image_scan_seen.add(normalized)
            self._image_scan_current_dir = "" if not normalized else normalized + "/"
            self._image_scan_page = 0
            self._image_scan_pages = 0
            channel.enqueue_command(
                "get_img_file_page_number",
                [self._image_scan_current_dir],
                tag="scan:page_count",
            )
            return
        self._image_scan_active = False
        self._image_file_count = int(self._image_scan_count)
        self.pub("image_file_count", self._image_file_count, force=True)
        LOG.info("[%s] FITS file scan complete: %s files", self.name, self._image_file_count)

    def _begin_image_scan(self, channel: AsiairChannel) -> None:
        if self._image_scan_active:
            return
        self._prepare_image_scan()
        channel.enqueue_command(
            "get_img_file_page_number",
            [""],
            tag="scan:page_count",
        )

    def _handle_image_scan_page_count(self, result: Any, channel: AsiairChannel) -> None:
        try:
            pages = int(self._scalar(result, "pages", "page_count", "count"))
        except (TypeError, ValueError):
            LOG.debug("[%s] FITS scan: invalid page count for %r: %r", self.name, self._image_scan_current_dir, result)
            self._queue_next_image_scan_dir(channel)
            return
        self._image_scan_pages = max(0, pages)
        self._image_scan_page = 0
        if self._image_scan_pages <= 0:
            self._queue_next_image_scan_dir(channel)
            return
        channel.enqueue_command(
            "get_img_file_page_name",
            [0],
            tag="scan:page",
        )

    def _handle_image_scan_page(self, result: Any, channel: AsiairChannel) -> None:
        entries = result if isinstance(result, list) else []
        current = self._image_scan_current_dir
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").strip()
            if not name:
                continue
            is_dir = bool(entry.get("is_dir"))
            if is_dir:
                # The image browser also exposes logs/system folders. They are
                # irrelevant to a stored-FITS count and can be safely skipped.
                if name.casefold() in {"log", "logs", "lost+found"}:
                    continue
                child = f"{current}{name}/" if current else f"{name}/"
                child_key = child.strip("/")
                if child_key not in self._image_scan_seen:
                    self._image_scan_queue.append(child)
                continue
            lower = name.casefold()
            if lower.endswith((".fit", ".fits")):
                self._image_scan_count += 1

        self._image_scan_page += 1
        if self._image_scan_page < self._image_scan_pages:
            channel.enqueue_command(
                "get_img_file_page_name",
                [self._image_scan_page],
                tag="scan:page",
            )
        else:
            self._queue_next_image_scan_dir(channel)

    def _request_current_image(self, reason: str, delay: float = 0.0) -> None:
        """Fetch port-4800 current image in a worker so telemetry never blocks."""
        def worker() -> None:
            if delay > 0 and self.stop_event.wait(delay):
                return
            if not self._image_fetch_lock.acquire(blocking=False):
                LOG.debug("[%s] Current-frame refresh already running; skipping %s", self.name, reason)
                return
            try:
                now = time.monotonic()
                if reason != "manual" and now - self._last_image_fetch < 1.0:
                    return
                self._last_image_fetch = now
                png, source_width, source_height, preview_width, preview_height = self._fetch_current_image_png()
                self.bridge.publish_image(self.node_id, png)
                self.pub("current_frame_resolution", f"{source_width}×{source_height} → {preview_width}×{preview_height}", force=True)
                self.pub("current_frame_updated", datetime.now().astimezone().isoformat(timespec="seconds"), force=True)
                LOG.info(
                    "[%s] Current frame refreshed (%s): %sx%s -> %sx%s, PNG %.1f KiB",
                    self.name, reason, source_width, source_height, preview_width, preview_height, len(png) / 1024.0,
                )
            except Exception as exc:
                # "there is no image now" is normal before the first exposure.
                if "no image" in str(exc).lower():
                    LOG.info("[%s] Current frame unavailable: %s", self.name, exc)
                else:
                    LOG.warning("[%s] Could not refresh current frame: %s", self.name, exc)
            finally:
                self._image_fetch_lock.release()

        threading.Thread(target=worker, name=f"{self.node_id}:4800-image", daemon=True).start()

    def _fetch_current_image_png(self) -> tuple[bytes, int, int, int, int]:
        request_id = 50000 + (int(time.monotonic() * 1000) % 1000000)
        request = {"id": request_id, "method": "get_current_img"}
        payload = (json.dumps(request, separators=(",", ":")) + "\r\n").encode("utf-8")
        packet = bytearray()
        started = time.monotonic()
        last_data: Optional[float] = None
        with socket.create_connection((self.host, 4800), timeout=5.0) as sock:
            sock.settimeout(1.0)
            sock.sendall(payload)
            while len(packet) < 160 * 1024 * 1024:
                try:
                    chunk = sock.recv(65536)
                except socket.timeout:
                    now = time.monotonic()
                    if last_data is not None and now - last_data >= 2.0:
                        break
                    if now - started >= 40.0:
                        break
                    continue
                if not chunk:
                    break
                packet.extend(chunk)
                last_data = time.monotonic()

        raw_packet = bytes(packet)
        if not raw_packet:
            raise TimeoutError("no image data returned from ASIAIR:4800")
        if b"there is no image now" in raw_packet.lower():
            raise ValueError("ASIAIR reported there is no image now")
        zip_offset = raw_packet.find(b"PK\x03\x04")
        if zip_offset < 0:
            raise ValueError("ASIAIR image response did not contain ZIP/raw_data")
        header = raw_packet[:zip_offset]
        if len(header) < 32:
            raise ValueError(f"ASIAIR image header too short: {len(header)} bytes")
        width = int.from_bytes(header[16:18], "big")
        height = int.from_bytes(header[18:20], "big")
        if width <= 0 or height <= 0:
            raise ValueError(f"invalid ASIAIR image dimensions {width}x{height}")
        with zipfile.ZipFile(io.BytesIO(raw_packet[zip_offset:])) as archive:
            names = archive.namelist()
            member = "raw_data" if "raw_data" in names else (names[0] if names else None)
            if member is None:
                raise ValueError("ASIAIR image ZIP is empty")
            raw_data = archive.read(member)
        pixel_count = width * height
        if len(raw_data) == pixel_count * 2:
            bytes_per_pixel = 2
        elif len(raw_data) == pixel_count:
            bytes_per_pixel = 1
        else:
            raise ValueError(
                f"unexpected raw_data size {len(raw_data)} for {width}x{height}"
            )
        png, preview_width, preview_height = _build_preview_png(
            raw_data, width, height, bytes_per_pixel, max_edge=1600
        )
        return png, width, height, preview_width, preview_height

    def _filter_options(self) -> list[str]:
        options: list[str] = []
        for index, raw_name in enumerate(self._wheel_names):
            name = str(raw_name).strip() or f"Slot {index + 1}"
            options.append(f"{index + 1} — {name}")
        return options

    def _filter_index_from_payload(self, payload: str) -> int:
        text = str(payload).strip()
        options = self._filter_options()
        if not options:
            raise ValueError("Filter wheel slot names are not known yet")

        # Normal Home Assistant select payload: exact display option.
        if text in options:
            return options.index(text)

        # Also accept a raw filter name (case-insensitive) for manual MQTT use.
        lowered = text.casefold()
        for index, raw_name in enumerate(self._wheel_names):
            if str(raw_name).strip().casefold() == lowered:
                return index

        # And accept a physical 1-based slot number for testing / service use.
        try:
            slot = int(text)
        except ValueError as exc:
            raise ValueError(f"Unknown filter {payload!r}; available: {options}") from exc
        if not 1 <= slot <= len(options):
            raise ValueError(f"Filter slot must be between 1 and {len(options)}")
        return slot - 1

    def _focuser_step(self) -> int:
        return self._focuser_coarse_step if self._focuser_step_mode == "Fast" else self._focuser_fine_step

    def _focuser_prepare_sequence(self) -> list[Tuple[str, Optional[Any], Optional[str]]]:
        """Return the minimal EAF-open prefix if the focuser is currently closed."""
        commands: list[Tuple[str, Optional[Any], Optional[str]]] = []
        if str(self._focuser_state or "").lower() in ("", "close", "closed", "disconnected", "unknown"):
            commands.append(("set_focuser_connect_mode", ["USB"], "cmd:EAFModeUSB"))
            if self._focuser_id is not None:
                commands.append(("open_focuser", [self._focuser_id], "cmd:EAFOpen"))
        return commands

    def _focuser_move_relative(self, delta_sign: int, channel: AsiairChannel) -> None:
        base = self._focuser_pending_target
        if base is None:
            base = self._focuser_position
        if base is None:
            raise ValueError("EAF position is not known yet")
        step = max(1, int(self._focuser_step()))
        maximum = self._focuser_max_step if self._focuser_max_step is not None else 600000
        target = int(base) + (step if delta_sign > 0 else -step)
        target = max(0, min(int(maximum), target))
        if target == int(base):
            raise ValueError(f"EAF already at position limit {target}")
        self._focuser_pending_target = target
        LOG.info(
            "[%s] EAF %s: %s -> %s (%s step=%s)",
            self.name, "+" if delta_sign > 0 else "-", base, target, self._focuser_step_mode, step,
        )
        # Captured official-app manual moves use stop_focuser immediately
        # before the absolute move_focuser target. Mirror that sequence.
        commands = self._focuser_prepare_sequence()
        commands.extend([
            ("stop_focuser", None, "cmd:FocuserStopBeforeMove"),
            ("move_focuser", [target], f"cmd:FocuserMove:{target}"),
        ])
        channel.enqueue_command_sequence(commands)

    def _queue_enter_initialization(self, channel: AsiairChannel) -> None:
        """Mirror the confirmed ASIAIR app Enter/device-open sequence."""
        camera_id = self._main_camera_id if self._main_camera_id is not None else 0
        focuser_id = self._focuser_id
        wheel_id = self._wheel_id

        commands: list[Tuple[str, Optional[Any], Optional[str]]] = []

        if str(self._camera_state or "").lower() in ("", "close", "closed", "disconnected", "unknown"):
            commands.append(("open_camera", [camera_id], "enter:open_camera"))
        if self._main_camera_name:
            commands.append(("set_app_setting", [{"main_camera_name": self._main_camera_name}], "enter:main_camera"))
        commands.append(("get_camera_state", None, "enter:camera_state"))
        if self._guide_camera_name:
            commands.append(("set_app_setting", [{"guide_camera_name": self._guide_camera_name}], "enter:guide_camera"))
        commands.extend([
            ("get_control_value", ["Gain", True], "Gain"),
            ("get_camera_info", None, "enter:camera_info"),
            ("get_gain_segment", None, "enter:gain_segment"),
        ])
        if self._focal_length is not None:
            commands.append(("set_focal_length", [int(self._focal_length)], "enter:focal_length"))
        commands.extend([
            ("get_controls", None, "enter:controls"),
            ("get_control_value", ["Exposure", True], "Exposure"),
        ])

        if focuser_id is not None:
            commands.append(("set_focuser_connect_mode", ["USB"], "enter:focuser_mode"))
            if str(self._focuser_state or "").lower() in ("", "close", "closed", "disconnected", "unknown"):
                commands.append(("open_focuser", [focuser_id], "enter:open_focuser"))
        if wheel_id is not None and str(self._wheel_state or "").lower() in ("", "close", "closed", "disconnected", "unknown"):
            commands.append(("open_wheel", [wheel_id], "enter:open_wheel"))

        # Immediate read-back after the device-open phase.
        commands.extend([
            ("get_focal_length", None, "enter:get_focal_length"),
            ("get_app_state", None, "enter:app_state"),
            ("get_camera_state", None, "enter:camera_state_confirm"),
            ("get_controls", None, "enter:controls_confirm"),
        ])
        for control in (
            "Gain", "Exposure", "CoolerOn", "Temperature",
            "TargetTemp", "CoolPowerPerc", "AntiDewHeater",
        ):
            commands.append(("get_control_value", [control, True], control))
        commands.extend([
            ("get_connected_wheels", None, "enter:wheels"),
            ("get_wheel_state", None, "enter:wheel_state"),
            ("get_wheel_setting", None, "enter:wheel_setting"),
            ("get_wheel_position", None, "enter:wheel_position"),
            ("get_connected_focuser", None, "enter:focuser"),
            ("get_focuser_state", None, "enter:focuser_state"),
            ("get_focuser_setting", None, "enter:focuser_setting"),
            ("get_focuser_info", None, "enter:focuser_info"),
        ])

        LOG.info(
            "[%s] Enter: camera=%s id=%s focal=%s guide=%s EAF=%s state=%s EFW=%s state=%s",
            self.name, self._main_camera_name or "unknown", camera_id, self._focal_length,
            self._guide_camera_name or "unknown", focuser_id, self._focuser_state,
            wheel_id, self._wheel_state,
        )
        channel.enqueue_command_sequence(commands)

    def handle_command(self, command: str, payload: str) -> None:
        # Mount tracking is handled by the separate scope channel on TCP 4400.
        # Do not make it depend on imager-channel (4700) authentication.
        if command == "mount_tracking":
            mount_channel = self._channel(4400)
            if mount_channel is None or mount_channel._sock is None:
                LOG.warning("[%s] Ignoring mount tracking command because ASIAIR port 4400 is offline", self.name)
                return
            try:
                enabled = self._parse_switch_payload(payload)
                LOG.info("[%s] Setting mount tracking to %s", self.name, enabled)
                mount_channel.enqueue_command(
                    "scope_set_track_state",
                    [enabled],
                    tag=f"cmd:MountTracking:{enabled}",
                )
            except Exception as exc:
                LOG.warning("[%s] Rejected mount tracking payload=%r: %s", self.name, payload, exc)
            return

        # Directly observed from the official ASIAIR app on 2026-10-17:
        # Go Home uses scope_park on TCP 4400, NOT go_home.
        # Do not consider RPC acceptance proof that the mount is safely parked.
        if command == "mount_cancel_go_home":
            if self._deferred_park_started_at is not None:
                self._reset_deferred_park()
                self.pub("mount_home_state", "cancelled_before_dispatch", force=True)
                LOG.warning("[%s] Pending Go Home cancelled; no scope_park sent", self.name)
            elif self._mount_park_requested_at is not None:
                LOG.warning("[%s] Cannot cancel Go Home: scope_park already dispatched", self.name)
            else:
                LOG.info("[%s] Cancel Go Home: no pending request", self.name)
            return

        if command == "mount_go_home":
            mount_channel = self._channel(4400)
            imager_channel = self._channel(4700)
            if mount_channel is None or mount_channel._sock is None or self._mount_missing:
                LOG.warning("[%s] Go Home rejected: mount offline or not connected", self.name)
                self.pub("mount_home_state", "failed_mount_offline", force=True)
                return
            if imager_channel is None or imager_channel._sock is None or not imager_channel._rpc_authenticated:
                LOG.warning("[%s] Go Home rejected: authenticated capture-state readback unavailable", self.name)
                self.pub("mount_home_state", "failed_imager_offline", force=True)
                return
            if self._mount_park_requested_at is not None or self._deferred_park_started_at is not None:
                LOG.info("[%s] Go Home already pending/running; ignoring duplicate button press", self.name)
                return
            # No immediate park, even if last state says 'off': those old
            # readings may be from an inter-frame gap or a previous session.
            self._reset_deferred_park()
            self._deferred_park_started_at = time.monotonic()
            initial = (
                "waiting_for_capture_stop" if self._capture_working is True
                else "waiting_for_goto" if any(self._goto_active_by_event.values())
                else "checking_safe_state"
            )
            self.pub("mount_home_state", initial, force=True)
            LOG.warning("[%s] Deferred Go Home requested; validating fresh capture and mount states (300s timeout)", self.name)
            imager_channel.enqueue_command("get_app_state", tag="park:verify_capture")
            mount_channel.enqueue_command("scope_is_moving", tag="park:verify_mount")
            self._deferred_park_last_capture_probe = self._deferred_park_started_at
            self._deferred_park_last_mount_probe = self._deferred_park_started_at
            return

        channel = self._channel(4700)
        if channel is None or channel._sock is None:
            LOG.warning("[%s] Ignoring command %s because ASIAIR port 4700 is offline", self.name, command)
            return
        if not channel._rpc_authenticated:
            LOG.warning("[%s] Ignoring command %s because ASIAIR port 4700 is not authenticated", self.name, command)
            return
        try:
            if command == "enter":
                LOG.info("[%s] ASIAIR Enter requested from Home Assistant", self.name)
                self._queue_enter_initialization(channel)
            elif command in ("autorun_start", "plan_start", "stop_autorun_plan", "plan_pause"):
                # ASIAIR official app traces (Autorun + Plan):
                # start_exposure(["light", false]) and stop_exposure([true]).
                # The RPCs affect the active in-device capture mode; the button
                # group does NOT select a new mode or overwrite the saved plan.
                starting = command in ("autorun_start", "plan_start")
                method = "start_exposure" if starting else "stop_exposure"
                params = ["light", False] if starting else [True]
                action = "start" if starting else "pause"
                LOG.warning("[%s] ASIAIR active capture %s requested (%s); active mode must be preselected in ASIAIR", self.name, action, command)
                self._capture_command_action = action
                self._capture_command_expected = starting
                self._capture_command_accepted_at = None
                self._capture_confirmation_next_poll = None
                self.pub("capture_command_state", f"{action}_requested", force=True)
                channel.enqueue_command(method, params, tag=f"cmd:Capture:{action}:{command}")
            elif command == "restart":
                LOG.warning("[%s] ASIAIR reboot requested from Home Assistant", self.name)
                channel.enqueue_command("pi_reboot", tag="cmd:Reboot")
            elif command == "shutdown":
                LOG.warning("[%s] ASIAIR shutdown requested from Home Assistant", self.name)
                channel.enqueue_command("pi_shutdown", tag="cmd:Shutdown")
            elif command == "refresh_image":
                LOG.info("[%s] Current-frame refresh requested from Home Assistant", self.name)
                self._request_current_image("manual")
            elif re.fullmatch(r"dc_output_[1-4]_type", command):
                port_index = int(command.split("_")[2]) - 1
                if self._has_dc_outputs is not True or port_index >= len(self._dc_ports):
                    raise ValueError("This ASIAIR does not expose that DC output")
                current_port = dict(self._dc_ports[port_index])
                new_type = self._rpc_dc_output_type(payload)
                params = {
                    f"port{port_index}": {
                        # Preserve all electrical settings; this action changes
                        # only ASIAIR's logical device label/type.
                        "is_pwm": bool(current_port.get("is_pwm", False)),
                        "state": bool(current_port.get("state", False)),
                        "type": new_type,
                        "value": current_port.get("value", 100),
                    }
                }
                LOG.warning(
                    "[%s] Changing DC output %s type to %s; ASIAIR may reconnect attached equipment",
                    self.name, port_index + 1, new_type,
                )
                channel.enqueue_command(
                    "pi_output_set2", params,
                    tag=f"cmd:DCOutputType:{port_index + 1}:{new_type}",
                )
            elif re.fullmatch(r"dc_output_[1-4]", command):
                port_index = int(command.rsplit("_", 1)[1]) - 1
                if self._has_dc_outputs is not True or port_index >= len(self._dc_ports):
                    raise ValueError("This ASIAIR does not expose that DC output")
                enabled = self._parse_switch_payload(payload)
                current_port = dict(self._dc_ports[port_index])
                params = {
                    f"port{port_index}": {
                        "is_pwm": bool(current_port.get("is_pwm", False)),
                        "state": enabled,
                        "type": str(current_port.get("type") or "other"),
                        "value": current_port.get("value", 100),
                    }
                }
                LOG.info("[%s] Setting DC output %s to %s (preserving type=%s value=%s pwm=%s)",
                         self.name, port_index + 1, enabled, params[f"port{port_index}"]["type"],
                         params[f"port{port_index}"]["value"], params[f"port{port_index}"]["is_pwm"])
                channel.enqueue_command("pi_output_set2", params, tag=f"cmd:DCOutput:{port_index + 1}:{enabled}")
            elif command == "cooler":
                value = 1 if self._parse_switch_payload(payload) else 0
                channel.enqueue_command("set_control_value", ["CoolerOn", value], tag=f"cmd:CoolerOn:{value}")
            elif command == "dew_heater":
                value = 1 if self._parse_switch_payload(payload) else 0
                channel.enqueue_command("set_control_value", ["AntiDewHeater", value], tag=f"cmd:AntiDewHeater:{value}")
            elif command == "target_temperature":
                value = float(payload)
                if value < -40 or value > 30:
                    raise ValueError("Target temperature must be between -40 and +30 °C")
                channel.enqueue_command("set_control_value", ["TargetTemp", value], tag=f"cmd:TargetTemp:{value}")
            elif command == "filter":
                index = self._filter_index_from_payload(payload)
                label = self._filter_options()[index]
                LOG.info("[%s] Changing EFW to %s (ASIAIR index %s)", self.name, label, index)
                channel.enqueue_command("set_wheel_position", [index], tag=f"cmd:WheelPosition:{index}")
            elif command == "focuser_set_current":
                value = int(round(float(payload)))
                maximum = self._focuser_max_step if self._focuser_max_step is not None else 600000
                if value < 0 or value > maximum:
                    raise ValueError(f"EAF current position must be between 0 and {maximum}")
                LOG.info("[%s] Redefining EAF current position to %s", self.name, value)
                # ASIAIR's settings UI uses set_focuser_value to redefine the
                # logical current counter without commanding a motor move.
                channel.enqueue_command("set_focuser_value", [value], tag=f"cmd:FocuserSetCurrent:{value}")
            elif command == "focuser_step_mode":
                mode = str(payload).strip().capitalize()
                if mode not in ("Slow", "Fast"):
                    raise ValueError("EAF step mode must be Slow or Fast")
                self._focuser_step_mode = mode
                self.pub("focuser_step_mode", mode, force=True)
                LOG.info("[%s] EAF step mode set to %s (step=%s)", self.name, mode, self._focuser_step())
            elif command == "focuser_minus":
                self._focuser_move_relative(-1, channel)
            elif command == "focuser_plus":
                self._focuser_move_relative(+1, channel)
            elif command == "focuser_af":
                LOG.info("[%s] Starting ASIAIR autofocus (mount tracking=%r)", self.name, self._mount_tracking)
                commands = self._focuser_prepare_sequence()
                commands.extend([
                    ("stop_focuser", None, "cmd:FocuserStopBeforeAF"),
                    ("start_auto_focuse", None, "cmd:AutoFocus"),
                ])
                channel.enqueue_command_sequence(commands)
            else:
                LOG.warning("[%s] Unknown MQTT command %s", self.name, command)
        except Exception as exc:
            LOG.warning("[%s] Rejected MQTT command %s payload=%r: %s", self.name, command, payload, exc)

    def _clear_capture_confirmation(self) -> None:
        self._capture_command_action = None
        self._capture_command_expected = None
        self._capture_command_accepted_at = None
        self._capture_confirmation_next_poll = None

    def _reset_deferred_park(self) -> None:
        """Remove ONLY waiting intent; does not abort an already-sent park."""
        self._deferred_park_started_at = None
        self._deferred_park_last_capture_probe = 0.0
        self._deferred_park_last_mount_probe = 0.0
        self._deferred_park_last_idle_read = 0.0
        self._deferred_park_idle_read_count = 0
        self._deferred_park_mount_still_at = None

    def _deferred_park_capture_readback(self, capture_working: bool) -> None:
        """Count *fresh* ASIAIR get_app_state readbacks, never MQTT state."""
        if self._deferred_park_started_at is None:
            return
        now = time.monotonic()
        if capture_working or self._sequence_active:
            self._deferred_park_idle_read_count = 0
            self._deferred_park_last_idle_read = 0.0
            self.pub("mount_home_state", "waiting_for_capture_stop", force=True)
        else:
            # Two independent reads >=2 seconds apart are required.  A single
            # OFF in a gap between frames is not sufficient to park.
            if self._deferred_park_idle_read_count == 0:
                self._deferred_park_idle_read_count = 1
                self._deferred_park_last_idle_read = now
            elif now - self._deferred_park_last_idle_read >= 2.0:
                self._deferred_park_idle_read_count = min(2, self._deferred_park_idle_read_count + 1)
                self._deferred_park_last_idle_read = now

    def _deferred_park_mount_readback(self, moving: Optional[bool]) -> None:
        if self._deferred_park_started_at is None:
            return
        self._deferred_park_mount_still_at = time.monotonic() if moving is False else None
        if moving is True:
            self.pub("mount_home_state", "waiting_for_mount_idle", force=True)

    def _service_deferred_park(self, now: Optional[float] = None) -> None:
        """Poll until safe, then dispatch scope_park exactly once (port 4400 thread).

        Never send stop_exposure here: a Go Home click is not permission to
        interrupt the active ASIAIR observing sequence or a Plate Solve.
        """
        started = self._deferred_park_started_at
        if started is None:
            return
        now = time.monotonic() if now is None else now
        if now - started >= 300.0:
            self._reset_deferred_park()
            self.pub("mount_home_state", "waiting_timeout_no_park", force=True)
            LOG.warning("[%s] Deferred Go Home timed out after 300 s without proven safe state; no scope_park sent", self.name)
            return
        mount = self._channel(4400)
        imager = self._channel(4700)
        if (mount is None or mount._sock is None or self._mount_missing or
                imager is None or imager._sock is None or not imager._rpc_authenticated):
            self._reset_deferred_park()
            self.pub("mount_home_state", "failed_offline_no_park", force=True)
            LOG.warning("[%s] Deferred Go Home cancelled due to channel loss", self.name)
            return

        # A second idle capture read is required; keep checking every 3s.
        # Queue control checks before background FITS/telemetry reads.
        if now - self._deferred_park_last_capture_probe >= 3.0:
            imager.enqueue_command("get_app_state", tag="park:verify_capture")
            self._deferred_park_last_capture_probe = now
        if now - self._deferred_park_last_mount_probe >= 3.0:
            mount.enqueue_command("scope_is_moving", tag="park:verify_mount")
            self._deferred_park_last_mount_probe = now

        # Cross-check any still-latched GoTo events against CURRENT physical
        # telemetry. Some ASIAIR versions emit only a ``working`` event and
        # never a matching ``complete``. Keep genuinely recent GoTo activity
        # as a blocker; accept a stale flag only after a sustained quiet period
        # with two fresh idle capture reads plus fresh motion=false.
        idle_verified = (
            self._deferred_park_idle_read_count >= 2
            and self._capture_working is False
            and not self._sequence_active
            and self._deferred_park_mount_still_at is not None
            and now - self._deferred_park_mount_still_at <= 4.0
            and now - self._deferred_park_last_idle_read <= 4.0
        )
        if idle_verified:
            for event, active in list(self._goto_active_by_event.items()):
                if not active:
                    continue
                last_active = self._goto_last_active_at.get(event)
                if (last_active is not None and now - last_active >= self._goto_stale_quiet_seconds):
                    self._goto_active_by_event[event] = False
                    self._goto_last_active_at.pop(event, None)
                    LOG.warning(
                        "[%s] Expiring stale %s working event after %.0fs without updates; "
                        "two fresh idle camera samples and stationary mount verified",
                        self.name, event, now - last_active,
                    )

        if (idle_verified and not any(self._goto_active_by_event.values())
                and self._mount_park_requested_at is None):
            self._reset_deferred_park()
            self._mount_park_requested_at = now
            self.pub("mount_home_state", "requested", force=True)
            LOG.warning("[%s] Deferred Go Home safe conditions confirmed; dispatching scope_park", self.name)
            mount.enqueue_command("scope_park", tag="cmd:GoHome")

    def _invalidate_volatile_4700_state(self) -> None:
        self._goto_active_by_event.clear()
        self._goto_last_active_at.clear()
        self._clear_capture_confirmation()
        self._unsupported_camera_controls.clear()
        LOG.info("[%s] Invalidating retained session state after port 4700 reconnect", self.name)
        self._sequence_active = False
        self._capture_working = None
        for key in VOLATILE_4700_KEYS:
            self.bridge.publish_unknown(self.node_id, key)

    def on_channel_status(self, port: int, online: bool) -> None:
        if port == 4700:
            if online:
                # An MQTT retained value may be from yesterday's plan.  Until this
                # connection receives a fresh event, show unknown rather than stale data.
                self._invalidate_volatile_4700_state()
            else:
                if self._deferred_park_started_at is not None:
                    self._reset_deferred_park()
                    self.pub("mount_home_state", "failed_imager_offline_no_park", force=True)
                self._clear_capture_confirmation()
                self._sequence_active = False
                self._capture_working = None
                self.pub("imager_verified", False, force=True)
            self.bridge.publish_device_availability(self.node_id, online)
        elif port == 4400:
            if self._deferred_park_started_at is not None:
                self._reset_deferred_park()
                self.pub("mount_home_state", "failed_mount_offline_no_park", force=True)
            self._mount_park_requested_at = None
            # A retained `complete` from the previous night must not look
            # like a fresh homing result after a reconnect / disconnect.
            self.bridge.publish_unknown(self.node_id, "mount_home_state")
            self.bridge.publish_port4400_availability(self.node_id, online)

    def pub(self, key: str, value: Any, *, min_interval: float = 0.0, force: bool = False) -> None:
        if key == "capture_working" and value is not None:
            self._capture_working = bool(value)
            if self._deferred_park_started_at is not None and self._capture_working:
                self._deferred_park_idle_read_count = 0
                self._deferred_park_last_idle_read = 0.0
        self.bridge.publish_state(self.node_id, key, value, min_interval=min_interval, force=force)

    @staticmethod
    def _get(mapping: Any, *path: str) -> Any:
        cur = mapping
        for part in path:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(part)
            if cur is None:
                return None
        return cur

    def _publish_sequence(self, payload: Dict[str, Any]) -> None:
        state = payload.get("state")
        if state is not None:
            self.pub("sequence_state", state)

        target = payload.get("target") or {}
        target_name = target.get("current_name")
        if target_name:
            self.pub("target", target_name)

        seq = target.get("seq") if isinstance(target, dict) else None
        seq = seq if isinstance(seq, dict) else {}
        frame_info = seq.get("frame") if isinstance(seq, dict) else None
        frame_info = frame_info if isinstance(frame_info, dict) else {}
        summary = payload.get("frame_summary") if isinstance(payload.get("frame_summary"), dict) else {}

        frame_type = seq.get("current_type")
        if frame_type:
            self.pub("frame_type", frame_type)

        current = payload.get("frame")
        total = payload.get("total_frame")
        completed = frame_info.get("complete_num")
        if completed is None:
            completed = summary.get("complete_num")
        if total is None:
            total = frame_info.get("total")
        if total is None:
            total = summary.get("total")
        if current is None:
            current = summary.get("current_index")

        if current is not None:
            self.pub("frame_current", current)
        if completed is not None:
            self.pub("frame_completed", completed)
        if total is not None:
            self.pub("frame_total", total)
        try:
            if total and completed is not None:
                self.pub("sequence_progress", 100.0 * float(completed) / float(total))
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    def _publish_camera_control(self, name: Any, value: Any) -> None:
        if name is None or value is None:
            return
        control = str(name)
        mapping = {
            "Temperature": "camera_temperature",
            "CoolPowerPerc": "cooler_power",
            "TargetTemp": "camera_target_temperature",
            "Gain": "gain",
        }
        if control in mapping:
            self.pub(mapping[control], value)
            return
        if control == "CoolerOn":
            try:
                self.pub("cooler_enabled", bool(int(value)))
            except (TypeError, ValueError):
                self.pub("cooler_enabled", bool(value))
            return
        if control == "AntiDewHeater":
            try:
                self.pub("dew_heater_enabled", bool(int(value)))
            except (TypeError, ValueError):
                self.pub("dew_heater_enabled", bool(value))
            return
        if control == "Exposure":
            try:
                raw = float(value)
                # ASI exposure controls are normally microseconds.
                self.pub("exposure_seconds", raw / 1_000_000.0 if raw > 1000 else raw)
            except (TypeError, ValueError):
                pass

    def _handle_camera_control_change(self, payload: Dict[str, Any]) -> None:
        # Firmware variants observed in the wild wrap control changes
        # differently.  Accept the common flat and nested forms.
        candidates = [payload]
        for key in ("data", "control", "camera", "result"):
            obj = payload.get(key)
            if isinstance(obj, dict):
                candidates.append(obj)

        for obj in candidates:
            name = obj.get("name") or obj.get("control_name") or obj.get("control")
            value = obj.get("value")
            if name is not None and value is not None:
                self._publish_camera_control(name, value)

            for control in (
                "Temperature", "CoolPowerPerc", "TargetTemp", "CoolerOn",
                "Gain", "Exposure", "AntiDewHeater",
            ):
                if control in obj:
                    self._publish_camera_control(control, obj.get(control))

    def _remember_filter_name(self, position: int, name: str) -> None:
        name = str(name).strip()
        if not name:
            return
        while len(self._wheel_names) <= position:
            self._wheel_names.append("")
        self._wheel_names[position] = name
        if self._wheel_position == position:
            self.pub("filter_name", name)

    def _learn_filter_from_filename(self, filename: Any) -> None:
        """Learn the active EFW slot name from ASIAIR's own FITS filename.

        Typical ASIAIR names contain ``_<filter>_gainNNN_``. This is only used
        when a wheel position is already known and the token does not look like
        a camera model, so it cannot invent a wheel slot from an arbitrary file.
        """
        if self._wheel_position is None or not filename:
            return
        match = re.search(r"_([^_/]+)_gain[-+]?\d+(?:\.\d+)?(?:_|\.)", str(filename), re.IGNORECASE)
        if not match:
            return
        candidate = match.group(1).strip()
        if re.fullmatch(r"(?:ASI)?\d{3,4}(?:MC|MM)(?:[-_ ]?Pro)?", candidate, re.IGNORECASE):
            return
        self._remember_filter_name(self._wheel_position, candidate)
        LOG.info("[%s] Learned filter slot %s name from FITS filename: %s", self.name, self._wheel_position + 1, candidate)

    def handle_event(self, event: str, payload: Dict[str, Any], port: int) -> None:
        if event not in self._seen_event_types:
            self._seen_event_types.add(event)
            LOG.info("[%s] ASIAIR event discovered on port %s: %s", self.name, port, event)

        if event == "Version" and port == 4700:
            self.bridge.update_metadata(
                self.node_id,
                "root",
                model=payload.get("name") or "ASIAIR",
                sw_version=payload.get("firmware_ver_string") or payload.get("svr_ver_string"),
            )
        elif event == "PiStatus":
            self.pub("cpu_temperature", payload.get("temp"), min_interval=5.0)
            self.pub("over_temperature", payload.get("is_overtemp"))
            self.pub("under_voltage", payload.get("is_undervolt"))
            self.pub("over_current", payload.get("is_over_current"))
        elif event == "Station":
            self.pub("wifi_signal", payload.get("sig_lev"), min_interval=5.0)
        elif event == "Exposure":
            state = payload.get("state")
            self.pub("exposure_state", state)
            self.pub("camera_state", state)
            if payload.get("page") is not None:
                self.pub("app_page", payload.get("page"))
            if state == "start":
                self.pub("capture_working", True)
            elif state in ("complete", "failed", "stopped", "stop", "idle", "paused", "pause", "aborted") and not self._sequence_active:
                self.pub("capture_working", False)
            if payload.get("exp_us") is not None:
                try:
                    self.pub("exposure_seconds", float(payload["exp_us"]) / 1_000_000.0)
                except (TypeError, ValueError):
                    pass
            self.pub("gain", payload.get("gain"))
        elif event == "Sequence":
            state = payload.get("state")
            if state in ("frame_start", "frame_complete", "running", "working", "start"):
                self._sequence_active = True
                self.pub("capture_working", True)
            elif state in ("complete", "completed", "sequence_complete", "stop", "stopped", "idle", "failed", "paused", "pause", "aborted"):
                self._sequence_active = False
                self.pub("capture_working", False)
            self._publish_sequence(payload)

            # Some firmware includes current wheel information inside the
            # sequence block.  Use it opportunistically when present.
            target = payload.get("target") if isinstance(payload.get("target"), dict) else {}
            seq = target.get("seq") if isinstance(target.get("seq"), dict) else {}
            for key in ("current_filter", "filter_name", "filter"):
                if seq.get(key) not in (None, ""):
                    self.pub("filter_name", seq.get(key))
                    break
            for key in ("filter_position", "filter_index", "wheel_position"):
                if seq.get(key) is not None:
                    try:
                        self._wheel_position = int(seq.get(key))
                        self._publish_filter()
                    except (TypeError, ValueError):
                        pass
                    break
        elif event == "SaveImage" and payload.get("state") == "complete":
            filename = payload.get("filename") or payload.get("fullname")
            self.pub("latest_file", filename)
            self._learn_filter_from_filename(filename)

            # Keep the startup FITS count current without rescanning the whole
            # filesystem after every exposure. If a full scan is still in
            # progress, let that scan establish the baseline instead.
            if not self._image_scan_active and self._image_file_count is not None:
                self._image_file_count += 1
                self.pub("image_file_count", self._image_file_count, force=True)

            # Disk usage changes with every saved frame. Refresh the cheap
            # storage counter immediately and fetch the new preview from 4800
            # shortly after ASIAIR reports the save complete.
            channel = self._channel(4700)
            if channel is not None and channel._sock is not None:
                channel.enqueue_command("get_disk_volume", tag="save:image:disk")
            self._request_current_image("SaveImage", delay=0.75)
        elif event == "Temperature":
            self.pub("camera_temperature", payload.get("value"), min_interval=1.0)
        elif event == "CoolerPower":
            value = payload.get("value")
            self.pub("cooler_power", value, min_interval=1.0)
        elif event == "CameraControlChange":
            self._handle_camera_control_change(payload)
        elif event == "CameraStateChange":
            camera_state = payload.get("state") or payload.get("value")
            if camera_state is not None:
                self._camera_state = str(camera_state)
            self.pub("camera_state", camera_state)
            camera_name = payload.get("name") or payload.get("camera_name")
            if camera_name:
                self.pub("camera_name", camera_name)
                self.bridge.update_metadata(self.node_id, "camera", model=camera_name)
        elif event == "DeviceChange":
            # Do not assume a fixed payload shape; publish camera metadata only
            # when the event explicitly identifies a camera.
            kind = str(payload.get("type") or payload.get("device_type") or "").lower()
            name = payload.get("name") or payload.get("device_name")
            if name and "camera" in kind:
                self.pub("camera_name", name)
                self.bridge.update_metadata(self.node_id, "camera", model=name)
        elif event in ("AutoGoto", "ScopeGoto"):
            # Track stages even if they began before the HA Go Home request.
            stage = str(payload.get("state") or payload.get("status") or "").lower()
            if stage in ("working", "running", "start", "started", "slewing", "solving", "exposing"):
                self._goto_active_by_event[event] = True
                self._goto_last_active_at[event] = time.monotonic()
                if self._deferred_park_started_at is not None:
                    # New GoTo activity invalidates an earlier idle verification:
                    # an old gap between plate-solve exposures is not clearance.
                    self._deferred_park_idle_read_count = 0
                    self._deferred_park_last_idle_read = 0.0
                    self.pub("mount_home_state", "waiting_for_goto", force=True)
            elif stage in ("complete", "completed", "idle", "stop", "stopped", "failed", "error", "cancelled", "canceled"):
                self._goto_active_by_event[event] = False
                self._goto_last_active_at.pop(event, None)
        elif event == "ScopeTrack":
            state = payload.get("state")
            self.pub("mount_tracking", state in ("on", "ON", True, 1))
        elif event in ("ScopeHome", "ScopePark"):
            # Only asynchronous confirmation is evidence of completed parking.
            state = payload.get("state") or payload.get("status") or payload.get("value")
            if state is not None:
                normalized = str(state).lower()
                self.pub("mount_home_state", normalized, force=True)
                LOG.info("[%s] Mount park event %s: %s", self.name, event, state)
                if normalized in ("complete", "completed", "success", "succeed", "failed", "error", "stopped"):
                    self._mount_park_requested_at = None
                if normalized in ("complete", "completed", "success", "succeed"):
                    mount_channel = self._channel(4400)
                    if mount_channel is not None and mount_channel._sock is not None:
                        mount_channel.enqueue_command_sequence([
                            ("scope_get_track_state", None, "park:track"),
                            ("scope_get_horiz_coord", None, "park:horiz"),
                            ("scope_get_ra_dec", None, "park:radec"),
                        ])
        elif event == "OpenEAF":
            state = payload.get("state")
            if state == "complete":
                self._focuser_state = "idle"
            elif state:
                self._focuser_state = str(state)
            self.pub("focuser_state", self._focuser_state)
        elif event == "OpenEFW":
            state = payload.get("state")
            if state == "complete":
                self._wheel_state = "idle"
            elif state:
                self._wheel_state = str(state)
        elif event == "WheelMove" and payload.get("state") == "complete":
            position = payload.get("position")
            if position is not None:
                try:
                    self._wheel_position = int(position)
                    self._publish_filter()
                except (TypeError, ValueError):
                    pass
            filter_name = payload.get("name") or payload.get("filter_name")
            if filter_name:
                if self._wheel_position is not None:
                    self._remember_filter_name(self._wheel_position, str(filter_name))
                else:
                    self.pub("filter_name", filter_name)
        elif event == "FocuserMove":
            for key in ("position", "pos", "value", "step"):
                if payload.get(key) is not None:
                    self.pub("focuser_position", payload[key])
                    break
        elif event == "GuideStep":
            self.pub("guide_star_mass", payload.get("StarMass"), min_interval=0.4)
            self.pub("guide_snr", payload.get("SNR"), min_interval=0.4)
            self.pub("guide_ra_error", payload.get("RADistanceRaw"), min_interval=0.4)
            self.pub("guide_dec_error", payload.get("DECDistanceRaw"), min_interval=0.4)
            self.pub("guide_total_error", payload.get("AvgDist"), min_interval=0.4)
            self.pub("guide_state", "guiding")
        elif event in ("StartGuiding", "RestartGuide"):
            self.pub("guide_state", "guiding")
        elif event == "GuidingStopped":
            self.pub("guide_state", "stopped")
        elif event in ("StarLost", "GuideStarLostTooMuch", "LockPositionLost"):
            self.pub("guide_state", "star_lost")

    def _publish_filter(self) -> None:
        if self._wheel_position is None:
            return
        # ASIAIR's protocol uses a zero-based wheel index (0..N-1). Home
        # Assistant should show the physical slot number humans see: 1..N.
        self.pub("filter_position", self._wheel_position + 1)
        if 0 <= self._wheel_position < len(self._wheel_names) and self._wheel_names[self._wheel_position]:
            name = self._wheel_names[self._wheel_position]
        else:
            name = f"Slot {self._wheel_position + 1}"
        self.pub("filter_name", name)
        options = self._filter_options()
        if 0 <= self._wheel_position < len(options):
            self.pub("filter_selection", options[self._wheel_position])

    @staticmethod
    def _scalar(result: Any, *keys: str) -> Any:
        """Extract a scalar from firmware variants that wrap values in dict/list."""
        if isinstance(result, dict):
            for key in keys:
                if key in result and result[key] is not None:
                    return result[key]
            if "value" in result:
                return result.get("value")
            return None
        if isinstance(result, (list, tuple)) and len(result) == 1:
            return result[0]
        return result

    def handle_rpc_error(self, method: str, code: Any, error: Any, port: int, tag: Optional[str] = None) -> bool:
        """Handle expected capability errors without flooding the log."""
        if port == 4700 and tag and tag.startswith("scan:"):
            LOG.info(
                "[%s] FITS count scan unavailable at %r: %s code=%r error=%r",
                self.name, self._image_scan_current_dir, method, code, error,
            )
            self._image_scan_active = False
            return True
        if port == 4700 and method == "get_control_value" and tag and (
            code == 109 or ("unexpected param" in str(error).lower())
        ):
            if tag not in self._unsupported_camera_controls:
                self._unsupported_camera_controls.add(tag)
                LOG.info("[%s] Camera control %s unsupported by this firmware; skipping read until reconnect (code=%r)",
                         self.name, tag, code)
            return True
        if port == 4700 and method in ("pi_output_get2", "get_power_supply"):
            # Unsupported power features are normal on Mini/Pro. Treat the
            # method failure as capability information instead of a warning.
            if method == "pi_output_get2":
                self._has_dc_outputs = False
                self._dc_ports = []
            else:
                self._has_power_monitor = False
            self._update_hardware_profile()
            LOG.info("[%s] Power capability %s unavailable: code=%r error=%r", self.name, method, code, error)
            return True
        if port == 4400 and method == "scope_park":
            self._mount_park_requested_at = None
            self.pub("mount_home_state", "failed", force=True)
            LOG.warning("[%s] Mount park rejected: code=%r error=%r", self.name, code, error)
            return True
        if port == 4700 and method in ("start_exposure", "stop_exposure"):
            self._clear_capture_confirmation()
            self.pub("capture_command_state", "failed", force=True)
            LOG.warning("[%s] ASIAIR %s rejected: code=%r error=%r", self.name, method, code, error)
            return True
        if port == 4400 and method.startswith("scope_"):
            text = str(error or "").lower()
            if code in (207, 315) or "mount is not connected" in text or "fail to operate" in text:
                if not self._mount_missing:
                    LOG.info("[%s] No mount connected; reducing port 4400 polling to a single probe", self.name)
                self._mount_missing = True
                self.pub("mount_device_connected", False, force=True)
                # Clear stale mount values from a previous connection/session.
                for key in (
                    "mount_altitude", "mount_azimuth", "mount_ra", "mount_dec",
                    "mount_pier_side", "mount_track_mode", "mount_tracking", "mount_slewing",
                ):
                    self.bridge.publish_unknown(self.node_id, key)
                return True
        return False

    def handle_response(self, method: str, tag: Optional[str], result: Any, port: int) -> None:
        if port == 4400 and method.startswith("scope_"):
            if self._mount_missing:
                LOG.info("[%s] Mount detected again; restoring full port 4400 polling", self.name)
            self._mount_missing = False
            self.pub("mount_device_connected", True, force=True)

        if port == 4700 and tag == "scan:page_count" and method == "get_img_file_page_number":
            channel = self._channel(4700)
            if channel is not None:
                self._handle_image_scan_page_count(result, channel)
            return
        if port == 4700 and tag == "scan:page" and method == "get_img_file_page_name":
            channel = self._channel(4700)
            if channel is not None:
                self._handle_image_scan_page(result, channel)
            return

        if method == "get_image_save_path" and isinstance(result, dict):
            current = result.get("cur_storage")
            if current is not None:
                self.pub("storage_current", current)
            volumes = result.get("storage_volume") if isinstance(result.get("storage_volume"), list) else []
            selected = None
            for volume in volumes:
                if not isinstance(volume, dict):
                    continue
                if selected is None:
                    selected = volume
                if current is not None and str(volume.get("name")) == str(current):
                    selected = volume
                    break
            if isinstance(selected, dict):
                self.pub("storage_state", selected.get("state"))
                self._publish_storage_numbers(
                    selected.get("totalMB"), selected.get("freeMB"), selected.get("diskSizeMB")
                )
        elif method == "get_disk_volume" and isinstance(result, dict):
            self._publish_storage_numbers(result.get("totalMB"), result.get("freeMB"))
        elif method == "get_svr_version" and isinstance(result, dict):
            sw = result.get("firmware_ver_string") or result.get("svr_ver_string")
            model = result.get("name")
            self.bridge.update_metadata(self.node_id, "root", sw_version=sw)
            if self._has_dc_outputs is None and self._has_power_monitor is None and model:
                self.bridge.update_metadata(self.node_id, "root", model="ASIAIR")
        elif method == "pi_get_info" and isinstance(result, dict):
            self.bridge.update_metadata(
                self.node_id,
                "root",
                serial_number=result.get("guid") or result.get("cpuId"),
                hw_version=result.get("model") or result.get("devtree_sub"),
            )
            self.pub("cpu_temperature", result.get("temp"))
        elif method == "get_app_state" and isinstance(result, dict):
            self.pub("app_page", result.get("page") or result.get("current_page") or result.get("app_page"))
            capture = result.get("capture") if isinstance(result.get("capture"), dict) else {}
            if capture:
                working = capture.get("is_working")
                if working is None:
                    working = capture.get("working")
                if working is not None:
                    if not bool(working):
                        self._sequence_active = False
                    confirmed_working = bool(working)
                    self.pub("capture_working", confirmed_working)
                    self._deferred_park_capture_readback(confirmed_working)
                    if (self._capture_command_accepted_at is not None
                            and self._capture_command_expected is confirmed_working):
                        action = self._capture_command_action
                        self._clear_capture_confirmation()
                        self.pub("capture_command_state", f"{action}_confirmed", force=True)
                        LOG.info("[%s] ASIAIR capture %s CONFIRMED by get_app_state is_working=%s",
                                 self.name, action, confirmed_working)
                target = capture.get("target")
                if isinstance(target, dict):
                    synthetic = {"state": "working" if capture.get("is_working") else "idle", "target": target}
                    self._publish_sequence(synthetic)
        elif method == "pi_station_state" and isinstance(result, dict):
            self.pub("station_mode", bool(result.get("server")))
            self.pub("wifi_network", result.get("ssid"))
            self.pub("wifi_ip", result.get("ip"))
            self.pub("wifi_frequency", result.get("freq"))
            self.pub("wifi_signal", result.get("sig_lev"), min_interval=5.0)
        elif method == "get_app_setting" and isinstance(result, dict):
            camera_name = result.get("main_camera_name")
            guide_name = result.get("guide_camera_name")
            if camera_name:
                self._main_camera_name = str(camera_name)
                self.pub("camera_name", camera_name)
                self.bridge.update_metadata(self.node_id, "camera", model=camera_name)
                if self._main_camera_name in self._camera_ids_by_name:
                    self._main_camera_id = self._camera_ids_by_name[self._main_camera_name]
            if guide_name:
                self._guide_camera_name = str(guide_name)
        elif method == "get_connected_cameras":
            cameras = result if isinstance(result, list) else []
            self._camera_ids_by_name = {}
            for camera in cameras:
                if not isinstance(camera, dict):
                    continue
                name = camera.get("name")
                camera_id = camera.get("id")
                if name is not None and camera_id is not None:
                    try:
                        self._camera_ids_by_name[str(name)] = int(camera_id)
                    except (TypeError, ValueError):
                        pass
            if self._main_camera_name and self._main_camera_name in self._camera_ids_by_name:
                self._main_camera_id = self._camera_ids_by_name[self._main_camera_name]
            elif cameras and isinstance(cameras[0], dict):
                try:
                    self._main_camera_id = int(cameras[0].get("id", 0))
                except (TypeError, ValueError):
                    self._main_camera_id = 0
            if cameras and isinstance(cameras[0], dict):
                camera_name = self._main_camera_name or cameras[0].get("name")
                if camera_name:
                    self.pub("camera_name", camera_name)
                    self.bridge.update_metadata(self.node_id, "camera", model=camera_name)
        elif method == "get_camera_state":
            state_obj = result[0] if isinstance(result, list) and result and isinstance(result[0], dict) else result
            if isinstance(state_obj, dict):
                camera_name = state_obj.get("name")
                state = state_obj.get("state")
                self._camera_state = str(state) if state is not None else None
                if camera_name:
                    self._main_camera_name = str(camera_name)
                    if self._main_camera_name in self._camera_ids_by_name:
                        self._main_camera_id = self._camera_ids_by_name[self._main_camera_name]
                self.pub("camera_name", camera_name or self._main_camera_name)
                self.pub("camera_state", state)
                if camera_name:
                    self.bridge.update_metadata(self.node_id, "camera", model=camera_name)
        elif method == "get_focal_length":
            value = self._scalar(result, "focal_length", "focal_len", "value")
            try:
                self._focal_length = int(round(float(value)))
                LOG.debug("[%s] ASIAIR focal length=%s mm", self.name, self._focal_length)
            except (TypeError, ValueError):
                pass
        elif method == "get_controls":
            # Keep this response visible in DEBUG; individual values are queried
            # separately below.  No HA entity is created from the capability list.
            pass
        elif method == "get_control_value":
            value = result.get("value") if isinstance(result, dict) else self._scalar(result)
            control = tag or (result.get("name") if isinstance(result, dict) else None)
            mapping = {
                "CoolPowerPerc": "cooler_power",
                "TargetTemp": "camera_target_temperature",
                "Gain": "gain",
            }
            if control == "Temperature":
                try:
                    self.pub("camera_temperature", float(value) / 10.0)
                except (TypeError, ValueError):
                    pass
            elif control in mapping:
                self.pub(mapping[control], value)
            elif control == "CoolerOn":
                try:
                    self.pub("cooler_enabled", bool(int(value)))
                except (TypeError, ValueError):
                    self.pub("cooler_enabled", bool(value))
            elif control == "Exposure":
                try:
                    self.pub("exposure_seconds", float(value) / 1_000_000.0)
                except (TypeError, ValueError):
                    pass
            elif control == "AntiDewHeater":
                try:
                    self.pub("dew_heater_enabled", bool(int(value)))
                except (TypeError, ValueError):
                    self.pub("dew_heater_enabled", bool(value))
        elif method == "set_control_value":
            # Ashley Brown's implementation reports 0 for a successful write.
            # Do not fabricate state here; CameraControlChange/Temperature/etc.
            # events remain the source of truth.
            if result in (0, None, True):
                LOG.info("[%s] ASIAIR control command accepted: %s", self.name, tag or "set_control_value")
            else:
                LOG.warning("[%s] Unexpected set_control_value result %r for %s", self.name, result, tag)
        elif method in ("open_camera", "open_focuser", "open_wheel", "set_app_setting", "set_focal_length", "set_focuser_connect_mode"):
            LOG.info("[%s] ASIAIR Enter/init RPC accepted: %s result=%r", self.name, tag or method, result)
        elif method == "set_wheel_position":
            # Do not optimistically change the HA state.  Confirm the physical
            # wheel position with the same read RPC used by the official app.
            if result in (0, None, True):
                LOG.info("[%s] ASIAIR EFW move command accepted: %s", self.name, tag or "set_wheel_position")
                channel = self._channel(4700)
                if channel is not None:
                    channel.enqueue_command("get_wheel_position", tag="cmd:WheelPositionConfirm")
            else:
                LOG.warning("[%s] Unexpected set_wheel_position result %r for %s", self.name, result, tag)
        elif method in ("stop_focuser", "move_focuser", "set_focuser_value", "start_auto_focuse"):
            # Reaching handle_response already means ASIAIR returned code=0 and
            # no error. Different firmware revisions may return either 0 or a
            # useful value, so confirm motor-changing operations from
            # get_focuser_info instead of assuming a specific success payload.
            LOG.info("[%s] ASIAIR EAF command accepted: %s result=%r", self.name, tag or method, result)
            if method != "stop_focuser":
                channel = self._channel(4700)
                if channel is not None:
                    channel.enqueue_command("get_focuser_info", tag=f"cmd:{method}:confirm")
        elif method == "pi_output_get2":
            ports = result if isinstance(result, list) else []
            clean_ports = [dict(item) for item in ports[:4] if isinstance(item, dict)]
            self._dc_ports = clean_ports
            self._has_dc_outputs = bool(clean_ports)
            self._update_hardware_profile()
            self._enable_dc_type_selects()
            for idx, port_state in enumerate(clean_ports):
                # Both switch ON/OFF and type are physical read-back states.
                # Never publish an optimistic value from the command payload.
                self.pub(f"dc_output_{idx + 1}", bool(port_state.get("state")))
                self.pub(
                    f"dc_output_{idx + 1}_type",
                    self._friendly_dc_output_type(port_state.get("type")),
                )
        elif method == "pi_output_set2":
            LOG.info("[%s] ASIAIR DC output command accepted: %s result=%r", self.name, tag or method, result)
            channel = self._channel(4700)
            if channel is not None:
                channel.enqueue_command("pi_output_get2", tag="cmd:DCOutput:confirm")
                if self._has_power_monitor is not False:
                    channel.enqueue_command("get_power_supply", tag="cmd:PowerSupply:confirm")
        elif method == "get_power_supply":
            self._publish_power_supply(result)
        elif method == "get_connected_wheels":
            wheels = result if isinstance(result, list) else []
            if wheels and isinstance(wheels[0], dict):
                wheel_name = wheels[0].get("name") or "EFW"
                try:
                    self._wheel_id = int(wheels[0].get("id", 0))
                except (TypeError, ValueError):
                    self._wheel_id = 0
                self.bridge.update_metadata(self.node_id, "filterwheel", model=wheel_name)
        elif method == "get_wheel_state":
            state_obj = result[0] if isinstance(result, list) and result and isinstance(result[0], dict) else result
            if isinstance(state_obj, dict):
                state = state_obj.get("state")
                self._wheel_state = str(state) if state is not None else None
        elif method == "get_wheel_setting":
            names = result.get("names") if isinstance(result, dict) else None
            if isinstance(names, list):
                self._wheel_names = [str(x) for x in names]
                LOG.info("[%s] Read filter slot names from ASIAIR: %s", self.name, self._wheel_names)
                self.bridge.set_select_options(self.node_id, "filter_control", self._filter_options())
                self._publish_filter()
        elif method == "get_wheel_position":
            value = self._scalar(result, "position", "pos", "index")
            try:
                self._wheel_position = int(value)
                self._publish_filter()
            except (TypeError, ValueError):
                pass
        elif method == "get_connected_focuser":
            focusers = result if isinstance(result, list) else []
            if focusers and isinstance(focusers[0], dict):
                foc_name = focusers[0].get("name") or "EAF"
                try:
                    self._focuser_id = int(focusers[0].get("id", 0))
                except (TypeError, ValueError):
                    self._focuser_id = 0
                self.bridge.update_metadata(self.node_id, "focuser", model=foc_name)
        elif method == "get_focuser_state" and isinstance(result, dict):
            state = result.get("state")
            self._focuser_state = str(state) if state is not None else None
            self.pub("focuser_state", state)
            if result.get("name"):
                self.bridge.update_metadata(self.node_id, "focuser", model=result.get("name"))
        elif method == "get_focuser_setting" and isinstance(result, dict):
            try:
                if result.get("fine_step") is not None:
                    self._focuser_fine_step = max(1, int(result.get("fine_step")))
                if result.get("coarse_step") is not None:
                    self._focuser_coarse_step = max(1, int(result.get("coarse_step")))
                LOG.debug(
                    "[%s] EAF step settings: Slow=%s Fast=%s",
                    self.name, self._focuser_fine_step, self._focuser_coarse_step,
                )
            except (TypeError, ValueError):
                pass
        elif method == "get_focuser_info" and isinstance(result, dict):
            position = result.get("position")
            state = result.get("state")
            self._focuser_state = str(state) if state is not None else self._focuser_state
            try:
                self._focuser_position = int(position) if position is not None else None
            except (TypeError, ValueError):
                self._focuser_position = None
            try:
                max_step = result.get("max_step")
                if max_step is not None:
                    self._focuser_max_step = max(1, int(max_step))
                    self.bridge.set_number_limits(
                        self.node_id, "focuser_current_position_control",
                        min_value=0, max_value=self._focuser_max_step, step=1,
                    )
            except (TypeError, ValueError):
                pass
            if state == "idle" and self._focuser_pending_target is not None:
                # Keep repeated +/- presses responsive while a move is pending,
                # then return to confirmed device position as soon as the EAF is idle.
                self._focuser_pending_target = None
            self.pub("focuser_position", position)
            self.pub("focuser_temperature", result.get("temperature"))
            self.pub("focuser_state", state)
            self.bridge.update_metadata(
                self.node_id,
                "focuser",
                model=result.get("model") or "EAF",
                serial_number=result.get("sn"),
                sw_version=result.get("firmware_version"),
            )
        elif port == 4700 and method in ("start_exposure", "stop_exposure"):
            action = "start" if method == "start_exposure" else "pause"
            LOG.info("[%s] ASIAIR capture %s RPC accepted: result=%r (physical state still unconfirmed)", self.name, action, result)
            self.pub("capture_command_state", f"{action}_accepted_unconfirmed", force=True)
            if self._capture_command_action == action:
                self._capture_command_accepted_at = time.monotonic()
                self._capture_confirmation_next_poll = self._capture_command_accepted_at + 3.0
            # Crucial: RPC acceptance does NOT equal observed capture state.
            # The HA capture_working binary sensor remains device-readback only.
            channel = self._channel(4700)
            if channel is not None and channel._sock is not None:
                channel.enqueue_command("get_app_state", tag=f"cmd:Capture:{action}:readback")
        elif method == "scope_get_horiz_coord" and isinstance(result, (list, tuple)) and len(result) >= 2:
            self.pub("mount_altitude", result[0])
            self.pub("mount_azimuth", result[1])
        elif method == "scope_get_ra_dec" and isinstance(result, (list, tuple)) and len(result) >= 2:
            self.pub("mount_ra", result[0])
            self.pub("mount_dec", result[1])
        elif method == "scope_get_pierside":
            self.pub("mount_pier_side", result)
        elif method == "scope_get_track_mode":
            if isinstance(result, dict):
                values = result.get("list")
                index = result.get("index")
                try:
                    self.pub("mount_track_mode", values[index])
                except Exception:
                    self.pub("mount_track_mode", index)
            else:
                self.pub("mount_track_mode", result)
        elif method == "scope_get_track_state":
            if isinstance(result, str):
                self._mount_tracking = result.lower() in ("on", "true", "1")
            else:
                self._mount_tracking = bool(result)
            self.pub("mount_tracking", self._mount_tracking)
        elif port == 4400 and method == "scope_set_track_state":
            LOG.info("[%s] ASIAIR mount tracking command accepted: %s result=%r", self.name, tag or method, result)
            mount_channel = self._channel(4400)
            if mount_channel is not None and mount_channel._sock is not None:
                mount_channel.enqueue_command("scope_get_track_state", tag="cmd:MountTrackingConfirm")
        elif port == 4400 and method == "scope_park":
            LOG.info("[%s] ASIAIR scope_park accepted: result=%r (asynchronous completion still required)", self.name, result)
            # An asynchronous completion event can arrive before the RPC reply.
            # Never overwrite an already-confirmed complete state with accepted.
            if self._mount_park_requested_at is not None:
                self.pub("mount_home_state", "accepted_unconfirmed", force=True)
        elif method == "scope_is_moving":
            # An absent/null reply is unknown, NEVER proof of a stationary mount.
            if isinstance(result, bool):
                moving = result
            elif result in (0, "none", "None", "off", "OFF", "false", "False", "0"):
                moving = False
            elif isinstance(result, (str, int, float)):
                moving = True
            else:
                moving = None
            if moving is not None:
                self.pub("mount_slewing", moving)
            self._deferred_park_mount_readback(moving)
