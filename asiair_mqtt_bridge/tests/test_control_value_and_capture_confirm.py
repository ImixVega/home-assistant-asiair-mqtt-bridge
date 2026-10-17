"""Regression tests for the corrected ASIAIR control-read wire format.

These tests are offline only, using a fake TCP socket and MQTT publisher.
"""
import json
import sys
import time
import types
import unittest
from pathlib import Path

stub = types.ModuleType("mqtt_bridge")
stub.MqttBridge = type("MqttBridge", (), {})
sys.modules["mqtt_bridge"] = stub
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from asiair_client import AsiairDevice


class Bridge:
    def __init__(self):
        self.states = {}
    def register_command_handler(self, *a, **k): pass
    def set_select_options(self, *a, **k): pass
    def publish_state(self, n, k, v, **kw): self.states[k] = v
    def publish_unknown(self, n, k): self.states[k] = None
    def publish_device_availability(self, *a, **k): pass
    def publish_port4400_availability(self, *a, **k): pass
    def update_metadata(self, *a, **k): pass


class Socket:
    def __init__(self): self.sent = []
    def sendall(self, b): self.sent.append(json.loads(b))


class Tests(unittest.TestCase):
    def setUp(self):
        self.bridge = Bridge()
        self.dev = AsiairDevice('test', 'Test', '127.0.0.1', self.bridge, 30, 8)
        self.ch = self.dev._channel(4700)
        self.ch._rpc_authenticated = True
        self.ch._sock = Socket()

    def test_all_control_reads_have_the_required_two_parameters(self):
        self.ch._enqueue_poll(initial=True)
        self.ch._enqueue_poll(initial=False)
        self.ch._enqueue_fast_4700_poll()
        self.dev._queue_enter_initialization(self.ch)
        commands = [t for t in self.ch._queue if t[0] == 'get_control_value']
        self.assertGreater(len(commands), 15)
        for method, params, tag in commands:
            self.assertEqual(params, [tag, True], (method, params, tag))
        self.ch._queue.clear()
        self.ch._enqueue('get_control_value', ['CoolerOn', True], tag='CoolerOn')
        self.ch._send_next_queued()
        self.assertEqual(self.ch._sock.sent[0]['params'], ['CoolerOn', True])

    def test_invalid_control_can_be_disabled_without_stopping_remaining_polls(self):
        self.assertTrue(self.dev.handle_rpc_error('get_control_value', 109, 'unexpected param', 4700, tag='AntiDewHeater'))
        self.assertIn('AntiDewHeater', self.dev._unsupported_camera_controls)
        self.ch._enqueue('get_control_value', ['AntiDewHeater', True], tag='AntiDewHeater')
        self.ch._enqueue('get_control_value', ['Gain', True], tag='Gain')
        self.ch._send_next_queued()
        self.assertEqual(self.ch._sock.sent[0]['params'], ['Gain', True])
        self.dev.on_channel_status(4700, True)
        self.assertFalse(self.dev._unsupported_camera_controls)

    def test_pause_requires_fresh_real_get_app_state_readback(self):
        self.dev.pub('capture_working', True)
        self.dev.handle_command('stop_autorun_plan', 'PRESS')
        self.dev.handle_event('Sequence', {'state': 'stopped'}, 4700)
        self.assertEqual(self.bridge.states['capture_command_state'], 'pause_requested')
        self.dev.handle_response('stop_exposure', 'cmd:Capture:pause:stop_autorun_plan', 0, 4700)
        self.assertEqual(self.bridge.states['capture_command_state'], 'pause_accepted_unconfirmed')
        self.assertIsNotNone(self.dev._capture_confirmation_next_poll)
        # A Sequence push cannot confirm that ASIAIR capture is safely paused.
        self.dev.handle_event('Sequence', {'state': 'stopped'}, 4700)
        self.assertEqual(self.bridge.states['capture_command_state'], 'pause_accepted_unconfirmed')
        # The first readback may still show the camera working.
        self.dev.handle_response('get_app_state', 'cmd:Capture:pause:readback', {'capture': {'is_working': True}}, 4700)
        self.assertEqual(self.bridge.states['capture_command_state'], 'pause_accepted_unconfirmed')
        # A NEW device readback completes confirmation.
        self.dev.handle_response('get_app_state', 'cmd:Capture:confirmation_poll', {'capture': {'is_working': False}}, 4700)
        self.assertEqual(self.bridge.states['capture_command_state'], 'pause_confirmed')
        self.assertFalse(self.bridge.states['capture_working'])
        self.assertIsNone(self.dev._capture_command_action)

    def test_resume_requires_readback_too(self):
        self.dev.pub('capture_working', False)
        self.dev.handle_command('autorun_start', 'PRESS')
        self.dev.handle_response('start_exposure', 'cmd:Capture:start:autorun_start', 0, 4700)
        self.assertEqual(self.bridge.states['capture_command_state'], 'start_accepted_unconfirmed')
        self.dev.handle_response('get_app_state', None, {'capture': {'is_working': True}}, 4700)
        self.assertEqual(self.bridge.states['capture_command_state'], 'start_confirmed')
        self.assertTrue(self.bridge.states['capture_working'])

    def test_lost_reply_clears_pending_confirmation(self):
        self.dev.handle_command('stop_autorun_plan', 'PRESS')
        self.dev.handle_rpc_error('stop_exposure', 42, 'error', 4700)
        self.assertEqual(self.bridge.states['capture_command_state'], 'failed')
        self.assertIsNone(self.dev._capture_command_expected)


if __name__ == '__main__': unittest.main()
