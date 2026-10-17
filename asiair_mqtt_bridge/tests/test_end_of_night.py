"""Offline regression tests; no physical ASIAIR/mount connection is made."""
import sys
import types
import unittest
from pathlib import Path

# The transport source annotates the MQTT class. Replace the MQTT dependency
# with a type-only fake, avoiding a requirement for a broker/Paho in CI.
stub = types.ModuleType('mqtt_bridge')
stub.MqttBridge = type('MqttBridge', (), {})
sys.modules['mqtt_bridge'] = stub
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))

from asiair_client import AsiairDevice
from entities import ENTITIES

class FakeBridge:
    def __init__(self):
        self.states = {}
    def register_command_handler(self, *args, **kwargs): pass
    def set_select_options(self, *args, **kwargs): pass
    def publish_state(self, node_id, key, value, **kwargs): self.states[key] = value
    def publish_unknown(self, node_id, key): self.states[key] = None
    def publish_device_availability(self, *args): pass
    def publish_port4400_availability(self, *args): pass
    def update_metadata(self, *args, **kwargs): pass

class TestEndOfNight(unittest.TestCase):
    def setUp(self):
        self.bridge = FakeBridge()
        self.device = AsiairDevice('test', 'Test ASIAIR', '127.0.0.1', self.bridge, 30, 8)
        self.imager = self.device._channel(4700)
        self.mount = self.device._channel(4400)
        self.imager._sock = object()
        self.imager._rpc_authenticated = True
        self.mount._sock = object()

    def pop(self, channel):
        self.assertTrue(channel._queue)
        return channel._queue.popleft()

    def test_all_four_capture_buttons_send_exact_official_rpc(self):
        for command in ['autorun_start', 'plan_start']:
            with self.subTest(command=command):
                self.imager._queue.clear()
                self.device.handle_command(command, 'PRESS')
                self.assertEqual(self.pop(self.imager)[:2], ('start_exposure', ['light', False]))
                self.assertEqual(self.bridge.states['capture_command_state'], 'start_requested')
        for command in ['stop_autorun_plan', 'plan_pause']:
            with self.subTest(command=command):
                self.imager._queue.clear()
                self.device.handle_command(command, 'PRESS')
                self.assertEqual(self.pop(self.imager)[:2], ('stop_exposure', [True]))
                self.assertEqual(self.bridge.states['capture_command_state'], 'pause_requested')

    def test_pause_ack_does_not_set_capture_working_false(self):
        self.device.pub('capture_working', True)
        self.device.handle_response('stop_exposure', 'cmd:Capture:pause:stop_autorun_plan', True, 4700)
        self.assertEqual(self.bridge.states['capture_command_state'], 'pause_accepted_unconfirmed')
        self.assertIs(self.bridge.states['capture_working'], True)
        self.assertEqual(self.pop(self.imager)[:2], ('get_app_state', None))
        self.device.handle_response('get_app_state', 'cmd:Capture:pause:readback', {'capture': {'is_working': False}}, 4700)
        self.assertIs(self.bridge.states['capture_working'], False)

    def test_go_home_waits_for_two_fresh_idle_readbacks_and_mount_still(self):
        from unittest.mock import patch
        import asiair_client
        with patch.object(asiair_client.time, "monotonic", return_value=100.0):
            self.device.pub('capture_working', False)
            self.device.handle_command('mount_go_home', 'PRESS')
            self.assertEqual(self.bridge.states['mount_home_state'], 'checking_safe_state')
            self.assertEqual(self.pop(self.imager)[:2], ('get_app_state', None))
            self.assertEqual(self.pop(self.mount)[:2], ('scope_is_moving', None))
            self.device.handle_response('get_app_state', 'park:verify_capture', {'capture': {'is_working': False}}, 4700)
            self.device.handle_response('scope_is_moving', 'park:verify_mount', False, 4400)
            self.device._service_deferred_park()
            self.assertFalse(self.mount._queue)
        with patch.object(asiair_client.time, "monotonic", return_value=103.2):
            self.device.handle_response('get_app_state', 'park:verify_capture', {'capture': {'is_working': False}}, 4700)
            self.device.handle_response('scope_is_moving', 'park:verify_mount', False, 4400)
            self.device._service_deferred_park()
            self.assertEqual(self.bridge.states['mount_home_state'], 'requested')
            # Periodic probes were queued prior to dispatch but the control
            # command must be first, ahead of lower-priority readbacks.
            self.assertEqual(self.pop(self.mount)[:2], ('scope_park', None))
            self.device.handle_response('scope_park', 'cmd:GoHome', 0, 4400)
            self.assertEqual(self.bridge.states['mount_home_state'], 'accepted_unconfirmed')
            self.device.handle_event('ScopeHome', {'state': 'complete'}, 4400)
            self.assertEqual(self.bridge.states['mount_home_state'], 'complete')
            self.assertIsNone(self.device._mount_park_requested_at)

    def test_go_home_while_capture_running_is_deferred_not_parked(self):
        self.device.pub('capture_working', True)
        self.device.handle_command('mount_go_home', 'PRESS')
        self.assertEqual(self.bridge.states['mount_home_state'], 'waiting_for_capture_stop')
        self.assertFalse(any(cmd[0] == 'scope_park' for cmd in self.mount._queue))
        self.assertFalse(any(cmd[0] == 'stop_exposure' for cmd in self.imager._queue))

    def test_duplicate_park_suppressed(self):
        self.device.pub('capture_working', False)
        self.device.handle_command('mount_go_home', 'PRESS')
        self.device.handle_command('mount_go_home', 'PRESS')
        self.assertEqual(len(self.mount._queue), 1)
        self.assertEqual(self.mount._queue[0][0], 'scope_is_moving')

    def test_rpc_errors_surface_without_false_success(self):
        self.device.pub('capture_working', True)
        self.assertTrue(self.device.handle_rpc_error('stop_exposure', 123, 'fail', 4700))
        self.assertEqual(self.bridge.states['capture_command_state'], 'failed')
        self.assertIs(self.bridge.states['capture_working'], True)
        self.assertTrue(self.device.handle_rpc_error('scope_park', 123, 'fail', 4400))
        self.assertEqual(self.bridge.states['mount_home_state'], 'failed')

    def test_reset_stale_state_on_reconnect(self):
        self.device.pub('mount_home_state', 'complete')
        self.device.on_channel_status(4400, True)
        self.assertIsNone(self.bridge.states['mount_home_state'])
        self.device.pub('capture_working', True)
        self.device.on_channel_status(4700, True)
        self.assertIsNone(self.bridge.states['capture_working'])

    def test_plan_entities_and_old_id_remain(self):
        keys={e.key for e in ENTITIES}
        self.assertTrue({'plan_start_button', 'plan_pause_button', 'autorun_start_button','autorun_stop_button','capture_command_state'} <= keys)

if __name__ == '__main__': unittest.main()
