from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class EntityDef:
    component: str
    key: str
    name: str
    group: str = "root"
    icon: Optional[str] = None
    unit: Optional[str] = None
    device_class: Optional[str] = None
    state_class: Optional[str] = None
    entity_category: Optional[str] = None
    suggested_display_precision: Optional[int] = None
    payload_on: Optional[str] = None
    payload_off: Optional[str] = None
    state_key: Optional[str] = None
    command_key: Optional[str] = None
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    step: Optional[float] = None
    mode: Optional[str] = None
    payload_press: Optional[str] = None


ENTITIES = [
    EntityDef("binary_sensor", "connected", "Connected", icon="mdi:lan-connect", device_class="connectivity"),
    EntityDef("binary_sensor", "imager_verified", "Imager authenticated", icon="mdi:shield-check", device_class="connectivity", entity_category="diagnostic"),
    EntityDef("sensor", "device_name", "Name", icon="mdi:identifier"),
    EntityDef("binary_sensor", "station_mode", "Station mode", icon="mdi:access-point-network"),
    EntityDef("sensor", "wifi_network", "Wi-Fi", icon="mdi:wifi"),
    EntityDef("sensor", "wifi_ip", "Wi-Fi IP", icon="mdi:ip-network", entity_category="diagnostic"),
    EntityDef("sensor", "wifi_frequency", "Wi-Fi frequency", icon="mdi:wifi", unit="MHz", state_class="measurement", entity_category="diagnostic"),
    EntityDef("sensor", "app_page", "App page", icon="mdi:application-outline", entity_category="diagnostic"),

    # Observing workflow mirrors the ASIAIR Autorun section instead of
    # cluttering the root ASIAIR system device.
    EntityDef("sensor", "target", "Target", group="autorun", icon="mdi:creation"),
    EntityDef("sensor", "sequence_state", "Sequence state", group="autorun", icon="mdi:playlist-play"),
    EntityDef("sensor", "frame_type", "Frame type", group="autorun", icon="mdi:image-multiple-outline"),
    EntityDef("sensor", "frame_current", "Current frame", group="autorun", icon="mdi:counter"),
    EntityDef("sensor", "frame_completed", "Completed frames", group="autorun", icon="mdi:check-circle-outline"),
    EntityDef("sensor", "frame_total", "Total frames", group="autorun", icon="mdi:counter"),
    EntityDef("sensor", "sequence_progress", "Sequence progress", group="autorun", icon="mdi:progress-clock", unit="%", state_class="measurement", suggested_display_precision=1),
    EntityDef("binary_sensor", "capture_working", "Capture working", group="autorun", icon="mdi:camera-timer"),
    EntityDef("sensor", "latest_file", "Latest saved file", group="files", icon="mdi:file-image-outline"),
    EntityDef("sensor", "cpu_temperature", "CPU temperature", icon="mdi:thermometer", unit="°C", device_class="temperature", state_class="measurement", entity_category="diagnostic", suggested_display_precision=1),
    EntityDef("sensor", "wifi_signal", "Wi-Fi signal", icon="mdi:wifi", unit="dBm", device_class="signal_strength", state_class="measurement", entity_category="diagnostic"),
    EntityDef("binary_sensor", "over_temperature", "Over temperature", icon="mdi:thermometer-alert", device_class="problem", entity_category="diagnostic"),
    EntityDef("binary_sensor", "under_voltage", "Under voltage", icon="mdi:flash-alert", device_class="problem", entity_category="diagnostic"),
    EntityDef("binary_sensor", "over_current", "Over current", icon="mdi:current-ac", device_class="problem", entity_category="diagnostic"),
    EntityDef("sensor", "hardware_variant", "Hardware variant", icon="mdi:memory", entity_category="diagnostic"),
    EntityDef("sensor", "input_voltage", "Input voltage", icon="mdi:flash", unit="V", device_class="voltage", state_class="measurement", entity_category="diagnostic", suggested_display_precision=2),
    EntityDef("sensor", "input_current", "Total current", icon="mdi:current-dc", unit="A", device_class="current", state_class="measurement", entity_category="diagnostic", suggested_display_precision=2),
    EntityDef("sensor", "input_power", "Total power", icon="mdi:lightning-bolt", unit="W", device_class="power", state_class="measurement", entity_category="diagnostic", suggested_display_precision=1),

    # Power outputs are capability-driven. They are only discovered after
    # pi_output_get2/get_power_supply has proved that this ASIAIR exposes them.
    # That keeps Mini clean, gives Pro the four DC switches, and lets Plus add
    # its electrical telemetry without a manual model selector.
    EntityDef("switch", "dc_output_1_control", "DC Output 1", icon="mdi:power-socket-eu", state_key="dc_output_1", command_key="dc_output_1"),
    EntityDef("switch", "dc_output_2_control", "DC Output 2", icon="mdi:power-socket-eu", state_key="dc_output_2", command_key="dc_output_2"),
    EntityDef("switch", "dc_output_3_control", "DC Output 3", icon="mdi:power-socket-eu", state_key="dc_output_3", command_key="dc_output_3"),
    EntityDef("switch", "dc_output_4_control", "DC Output 4", icon="mdi:power-socket-eu", state_key="dc_output_4", command_key="dc_output_4"),
    # The select state is always read back from pi_output_get2, so it doubles as
    # the live status of the type currently configured in ASIAIR.
    EntityDef("select", "dc_output_1_type_control", "DC Output 1 type", icon="mdi:power-plug-outline", state_key="dc_output_1_type", command_key="dc_output_1_type"),
    EntityDef("select", "dc_output_2_type_control", "DC Output 2 type", icon="mdi:power-plug-outline", state_key="dc_output_2_type", command_key="dc_output_2_type"),
    EntityDef("select", "dc_output_3_type_control", "DC Output 3 type", icon="mdi:power-plug-outline", state_key="dc_output_3_type", command_key="dc_output_3_type"),
    EntityDef("select", "dc_output_4_type_control", "DC Output 4 type", icon="mdi:power-plug-outline", state_key="dc_output_4_type", command_key="dc_output_4_type"),
    EntityDef("sensor", "dc_output_1_voltage", "DC Output 1 voltage", icon="mdi:flash", unit="V", device_class="voltage", state_class="measurement", suggested_display_precision=2),
    EntityDef("sensor", "dc_output_2_voltage", "DC Output 2 voltage", icon="mdi:flash", unit="V", device_class="voltage", state_class="measurement", suggested_display_precision=2),
    EntityDef("sensor", "dc_output_3_voltage", "DC Output 3 voltage", icon="mdi:flash", unit="V", device_class="voltage", state_class="measurement", suggested_display_precision=2),
    EntityDef("sensor", "dc_output_4_voltage", "DC Output 4 voltage", icon="mdi:flash", unit="V", device_class="voltage", state_class="measurement", suggested_display_precision=2),

    EntityDef("sensor", "storage_current", "Current storage", group="files", icon="mdi:harddisk"),
    EntityDef("sensor", "storage_state", "Storage state", group="files", icon="mdi:harddisk"),
    EntityDef("sensor", "storage_disk_size", "Physical disk size", group="files", icon="mdi:harddisk", unit="GB", state_class="measurement", suggested_display_precision=2),
    EntityDef("sensor", "storage_total", "Storage total", group="files", icon="mdi:database", unit="GB", state_class="measurement", suggested_display_precision=2),
    EntityDef("sensor", "storage_used", "Storage used", group="files", icon="mdi:database", unit="GB", state_class="measurement", suggested_display_precision=2),
    EntityDef("sensor", "storage_free", "Storage free", group="files", icon="mdi:database-outline", unit="GB", state_class="measurement", suggested_display_precision=2),
    EntityDef("sensor", "storage_used_percent", "Storage used percent", group="files", icon="mdi:chart-donut", unit="%", state_class="measurement", suggested_display_precision=1),
    EntityDef("sensor", "image_file_count", "FITS files", group="files", icon="mdi:file-image", state_class="measurement"),

    # Mirrors the official ASIAIR app "Enter" action: open the selected main camera
    # and attached EAF/EFW, then refresh live camera/peripheral state.
    EntityDef("button", "enter_button", "Enter", icon="mdi:login", command_key="enter", payload_press="PRESS"),
    EntityDef("button", "restart_button", "Restart", icon="mdi:restart", command_key="restart", payload_press="PRESS"),
    EntityDef("button", "shutdown_button", "Shutdown", icon="mdi:power", command_key="shutdown", payload_press="PRESS"),

    EntityDef("sensor", "camera_name", "Camera", group="camera", icon="mdi:camera"),
    EntityDef("sensor", "camera_state", "State", group="camera", icon="mdi:camera"),
    EntityDef("sensor", "camera_temperature", "Temperature", group="camera", icon="mdi:thermometer", unit="°C", device_class="temperature", state_class="measurement", suggested_display_precision=1),
    EntityDef("sensor", "camera_target_temperature", "Target temperature", group="camera", icon="mdi:snowflake-thermometer", unit="°C", device_class="temperature", state_class="measurement", suggested_display_precision=1),
    EntityDef("sensor", "cooler_power", "Cooler power", group="camera", icon="mdi:snowflake", unit="%", state_class="measurement", suggested_display_precision=1),
    EntityDef("binary_sensor", "cooler_enabled", "Cooler enabled", group="camera", icon="mdi:snowflake"),
    EntityDef("binary_sensor", "dew_heater_enabled", "Dew heater enabled", group="camera", icon="mdi:water-percent"),
    EntityDef("sensor", "gain", "Gain", group="camera", icon="mdi:camera-control", state_class="measurement"),
    EntityDef("sensor", "exposure_seconds", "Exposure", group="camera", icon="mdi:timer-outline", unit="s", device_class="duration", state_class="measurement", suggested_display_precision=3),
    EntityDef("sensor", "exposure_state", "Exposure state", group="camera", icon="mdi:camera-timer"),

    # Current preview is published as a raw PNG payload on a dedicated MQTT
    # image topic.  Refresh is also triggered automatically after SaveImage.
    EntityDef("image", "current_frame_image", "Current frame", group="camera", icon="mdi:image"),
    EntityDef("sensor", "current_frame_resolution", "Current frame resolution", group="camera", icon="mdi:image-size-select-large"),
    EntityDef("sensor", "current_frame_updated", "Current frame updated", group="camera", icon="mdi:clock-outline", device_class="timestamp"),
    EntityDef("button", "refresh_image_button", "Refresh image", group="camera", icon="mdi:refresh",
              command_key="refresh_image", payload_press="PRESS"),

    EntityDef("sensor", "guide_state", "Guiding state", group="guider", icon="mdi:target"),
    EntityDef("sensor", "guide_snr", "Guide SNR", group="guider", icon="mdi:signal", state_class="measurement", suggested_display_precision=2),
    EntityDef("sensor", "guide_star_mass", "Guide star mass", group="guider", icon="mdi:star", state_class="measurement", suggested_display_precision=2),
    EntityDef("sensor", "guide_ra_error", "RA error", group="guider", icon="mdi:axis-x-arrow", state_class="measurement", suggested_display_precision=3),
    EntityDef("sensor", "guide_dec_error", "DEC error", group="guider", icon="mdi:axis-y-arrow", state_class="measurement", suggested_display_precision=3),
    EntityDef("sensor", "guide_total_error", "Total guide error", group="guider", icon="mdi:target", state_class="measurement", suggested_display_precision=3),

    EntityDef("sensor", "mount_altitude", "Altitude", group="mount", icon="mdi:telescope", unit="°", state_class="measurement", suggested_display_precision=3),
    EntityDef("sensor", "mount_azimuth", "Azimuth", group="mount", icon="mdi:telescope", unit="°", state_class="measurement", suggested_display_precision=3),
    EntityDef("sensor", "mount_ra", "Right ascension", group="mount", icon="mdi:telescope", unit="h", state_class="measurement", suggested_display_precision=4),
    EntityDef("sensor", "mount_dec", "Declination", group="mount", icon="mdi:telescope", unit="°", state_class="measurement", suggested_display_precision=4),
    EntityDef("sensor", "mount_pier_side", "Pier side", group="mount", icon="mdi:telescope"),
    EntityDef("sensor", "mount_track_mode", "Track mode", group="mount", icon="mdi:telescope"),
    EntityDef("binary_sensor", "mount_tracking", "Tracking", group="mount", icon="mdi:telescope"),
    EntityDef("switch", "mount_tracking_control", "Tracking", group="mount", icon="mdi:telescope",
              state_key="mount_tracking", command_key="mount_tracking"),
    EntityDef("binary_sensor", "mount_slewing", "Slewing", group="mount", icon="mdi:rotate-orbit"),
    EntityDef("binary_sensor", "mount_channel_connected", "ASIAIR port 4400", group="mount", icon="mdi:lan-connect", device_class="connectivity", entity_category="diagnostic"),
    EntityDef("binary_sensor", "mount_device_connected", "Mount connected", group="mount", icon="mdi:telescope", device_class="connectivity", entity_category="diagnostic"),

    EntityDef("sensor", "focuser_position", "Position", group="focuser", icon="mdi:focus-auto", state_class="measurement"),
    EntityDef("sensor", "focuser_state", "State", group="focuser", icon="mdi:focus-auto"),
    EntityDef("sensor", "focuser_temperature", "Temperature", group="focuser", icon="mdi:thermometer", unit="°C", device_class="temperature", state_class="measurement", suggested_display_precision=1),

    # EAF controls.  The editable current-position number changes the EAF's
    # logical position counter without moving the motor.  Slow/Fast is a
    # bridge-local selection which chooses the fine/coarse step learned from
    # get_focuser_setting.  +/- perform an absolute move_focuser command and
    # AF starts ASIAIR's autofocus routine.
    EntityDef("number", "focuser_current_position_control", "Set current position", group="focuser",
              icon="mdi:counter", state_key="focuser_position", command_key="focuser_set_current",
              min_value=0.0, max_value=600000.0, step=1.0, mode="box"),
    EntityDef("select", "focuser_step_mode_control", "Step mode", group="focuser", icon="mdi:speedometer",
              state_key="focuser_step_mode", command_key="focuser_step_mode"),
    EntityDef("button", "focuser_minus_button", "−", group="focuser", icon="mdi:minus",
              command_key="focuser_minus", payload_press="PRESS"),
    EntityDef("button", "focuser_plus_button", "+", group="focuser", icon="mdi:plus",
              command_key="focuser_plus", payload_press="PRESS"),
    EntityDef("button", "focuser_af_button", "AF", group="focuser", icon="mdi:focus-auto",
              command_key="focuser_af", payload_press="PRESS"),
    EntityDef("sensor", "filter_position", "Slot", group="filterwheel", icon="mdi:image-filter-black-white", state_class="measurement"),
    EntityDef("sensor", "filter_name", "Current filter", group="filterwheel", icon="mdi:image-filter-black-white"),

    # Dynamically populated once get_wheel_setting returns the slot names.
    # The state uses a human-friendly 1-based slot label (for example
    # "1 — SHb"), while the ASIAIR RPC itself remains zero-based.
    EntityDef("select", "filter_control", "Filter", group="filterwheel", icon="mdi:image-filter-black-white",
              state_key="filter_selection", command_key="filter"),

    # Optional control entities. They are only advertised when allow_control=true.
    # Their state is shared with the read-only telemetry entities above.
    EntityDef("switch", "cooler_control", "Cooler", group="camera", icon="mdi:snowflake",
              state_key="cooler_enabled", command_key="cooler"),
    EntityDef("number", "target_temperature_control", "Target temperature", group="camera",
              icon="mdi:snowflake-thermometer", unit="°C", device_class="temperature",
              state_key="camera_target_temperature", command_key="target_temperature",
              min_value=-40.0, max_value=30.0, step=1.0, mode="box"),
    EntityDef("switch", "dew_heater_control", "Dew heater", group="camera", icon="mdi:water-percent",
              state_key="dew_heater_enabled", command_key="dew_heater"),
]
