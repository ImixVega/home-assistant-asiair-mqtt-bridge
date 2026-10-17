"""Regression tests reproducing user-reported v0.1.25 tracking disconnect and stale GoTo."""
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

stub = types.ModuleType('mqtt_bridge')
stub.MqttBridge = type('MqttBridge', (), {})
sys.modules['mqtt_bridge'] = stub
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import asiair_client

class FakeBridge:
    def __init__(self): self.states = {}
    def register_command_handler(self, *args, **kwargs): pass
    def set_select_options(self, *args, **kwargs): pass
    def publish_state(self, node, key, value, **kwargs): self.states[key] = value
    def publish_unknown(self, node, key): self.states[key] = None
    def publish_device_availability(self, *args): pass
    def publish_port4400_availability(self, *args): pass
    def update_metadata(self, *args, **kwargs): pass

class TrackingSocket:
    def __init__(self): self.writes=[]
    def sendall(self, payload): self.writes.append(payload)

class TestV0126Regressions(unittest.TestCase):
    def setUp(self):
        self.bridge=FakeBridge()
        self.dev=asiair_client.AsiairDevice('mini','Mini','127.0.0.1',self.bridge,30,8)
        self.mount=self.dev._channel(4400)
        self.imager=self.dev._channel(4700)
        self.mount._sock=TrackingSocket()
        self.imager._sock=TrackingSocket()
        self.imager._rpc_authenticated=True

    def command_go_home_at(self, when):
        with patch.object(asiair_client.time, 'monotonic', return_value=when):
            self.dev.handle_command('mount_go_home','PRESS')

    def read_idle_at(self, when, *, working=False, moving=False):
        with patch.object(asiair_client.time, 'monotonic', return_value=when):
            self.dev.handle_response('get_app_state', 'park:verify_capture', {'capture':{'is_working':working}}, 4700)
            self.dev.handle_response('scope_is_moving', 'park:verify_mount', moving, 4400)
            self.dev._service_deferred_park()

    def test_tracking_ack_does_not_raise_nameerror(self):
        # The actual user log: scope_set_track_state result=0 -> UnboundLocalError('channel').
        self.dev.handle_response('scope_set_track_state', 'cmd:MountTracking:False', 0, 4400)
        self.assertEqual(self.mount._queue[0][0], 'scope_get_track_state')
        self.assertEqual(self.mount._queue[0][2], 'cmd:MountTrackingConfirm')

    def test_tracking_wire_reply_does_not_disconnect(self):
        self.mount._send('scope_set_track_state', [False], tag='cmd:MountTracking:False')
        request_id = next(iter(self.mount._pending))
        self.mount._handle_message({'id':request_id, 'result':0, 'code':0})
        # The receive handler dispatches the verification RPC immediately.
        import json
        request = json.loads(self.mount._sock.writes[-1].decode().strip())
        self.assertEqual(request['method'], 'scope_get_track_state')
        self.assertEqual(len(self.mount._pending), 1)

    def test_stale_autogoto_no_longer_blocks_forever(self):
        # Exactly the reported regression: AutoGoto working event without completion.
        with patch.object(asiair_client.time,'monotonic',return_value=90.0):
            self.dev.handle_event('AutoGoto',{'state':'working'},4700)
            self.dev.handle_event('ScopeGoto',{'state':'working'},4400)
        self.command_go_home_at(100.0)
        self.read_idle_at(101.0)
        self.read_idle_at(104.0)
        self.assertFalse(any(method=='scope_park' for method,_,_ in self.mount._queue))
        # >30s quiet period from latest GoTo event; fresh idle readbacks continue.
        self.read_idle_at(119.0)
        self.read_idle_at(121.0)
        self.assertEqual(self.mount._queue[0][0], 'scope_park')
        self.assertEqual(self.bridge.states['mount_home_state'], 'requested')
        self.assertFalse(any(self.dev._goto_active_by_event.values()))

    def test_fresh_goto_updates_rearm_latch(self):
        with patch.object(asiair_client.time,'monotonic',return_value=90.0):
            self.dev.handle_event('AutoGoto',{'state':'working'},4700)
        self.command_go_home_at(100.0)
        self.read_idle_at(101.0)
        self.read_idle_at(104.0)
        with patch.object(asiair_client.time,'monotonic',return_value=118.0):
            self.dev.handle_event('AutoGoto',{'state':'working'},4700)
        self.read_idle_at(122.0)
        self.read_idle_at(125.0)
        self.assertFalse(any(method=='scope_park' for method,_,_ in self.mount._queue))
        self.read_idle_at(151.0)
        self.assertEqual(self.mount._queue[0][0], 'scope_park')

    def test_stale_goto_never_bypasses_camera_or_mount_busy(self):
        with patch.object(asiair_client.time,'monotonic',return_value=90.0):
            self.dev.handle_event('AutoGoto',{'state':'working'},4700)
        self.command_go_home_at(100)
        self.read_idle_at(121,working=True)
        self.read_idle_at(124,working=True)
        self.assertFalse(any(method=='scope_park' for method,_,_ in self.mount._queue))
        self.read_idle_at(127,moving=True)
        self.read_idle_at(131,moving=True)
        self.assertFalse(any(method=='scope_park' for method,_,_ in self.mount._queue))
        self.read_idle_at(135,moving=False)
        self.assertEqual(self.mount._queue[0][0], 'scope_park')

    def test_goto_state_cleared_at_imager_reconnect(self):
        with patch.object(asiair_client.time,'monotonic',return_value=90.0):
            self.dev.handle_event('AutoGoto',{'state':'working'},4700)
        self.dev.on_channel_status(4700, True)
        self.assertFalse(self.dev._goto_active_by_event)
        self.assertFalse(self.dev._goto_last_active_at)

if __name__=='__main__': unittest.main()
