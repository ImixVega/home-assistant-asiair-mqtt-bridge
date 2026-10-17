"""Safety regression checks for deferred Go Home (offline)."""
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
    def __init__(self): self.states={}
    def register_command_handler(self,*a,**k): pass
    def set_select_options(self,*a,**k): pass
    def publish_state(self,n,key,value,**k): self.states[key]=value
    def publish_unknown(self,n,key): self.states[key]=None
    def publish_device_availability(self,*a,**k): pass
    def publish_port4400_availability(self,*a,**k): pass
    def update_metadata(self,*a,**k): pass

class DeferredParkTests(unittest.TestCase):
    def setUp(self):
        self.bridge=FakeBridge()
        self.dev=asiair_client.AsiairDevice('mini','Mini','127.0.0.1',self.bridge,30,8)
        self.mount=self.dev._channel(4400)
        self.imager=self.dev._channel(4700)
        self.mount._sock=object()
        self.imager._sock=object()
        self.imager._rpc_authenticated=True

    def request_at(self,when=100):
        with patch.object(asiair_client.time,'monotonic', return_value=when):
            self.dev.handle_command('mount_go_home','PRESS')

    def respond_at(self,when, *, working=False, moving=False):
        with patch.object(asiair_client.time,'monotonic',return_value=when):
            self.dev.handle_response('get_app_state','park:verify_capture',{'capture':{'is_working':working}},4700)
            self.dev.handle_response('scope_is_moving','park:verify_mount',moving,4400)
            self.dev._service_deferred_park()

    def test_busy_state_and_mid_frame_idle_do_not_dispatch(self):
        self.dev.pub('capture_working', True)
        self.request_at()
        self.respond_at(101,working=True)
        self.respond_at(103,working=False)
        self.assertFalse(any(x[0]=='scope_park' for x in self.mount._queue))
        self.respond_at(104,working=True)
        self.respond_at(105,working=False)
        self.assertFalse(any(x[0]=='scope_park' for x in self.mount._queue))
        self.respond_at(109,working=False)
        self.assertEqual(self.mount._queue[0][0],'scope_park')

    def test_cancel_before_dispatch_stops_future_park(self):
        self.request_at()
        self.dev.handle_command('mount_cancel_go_home','PRESS')
        self.assertEqual(self.bridge.states['mount_home_state'],'cancelled_before_dispatch')
        self.respond_at(103,working=False)
        self.respond_at(107,working=False)
        self.assertFalse(any(x[0]=='scope_park' for x in self.mount._queue))

    def test_timeout_does_not_park(self):
        self.request_at()
        with patch.object(asiair_client.time,'monotonic',return_value=401):
            self.dev._service_deferred_park()
        self.assertEqual(self.bridge.states['mount_home_state'],'waiting_timeout_no_park')
        self.assertFalse(any(x[0]=='scope_park' for x in self.mount._queue))

    def test_no_dispatch_if_mount_is_moving_or_readback_missing(self):
        self.request_at()
        self.respond_at(101,moving=True)
        self.respond_at(105,moving=True)
        self.assertFalse(any(x[0]=='scope_park' for x in self.mount._queue))
        with patch.object(asiair_client.time,'monotonic',return_value=109):
            self.dev.handle_response('scope_is_moving','park:verify_mount',None,4400)
            self.dev._service_deferred_park()
        self.assertFalse(any(x[0]=='scope_park' for x in self.mount._queue))
        self.respond_at(112,moving=False)
        self.assertEqual(self.mount._queue[0][0],'scope_park')

    def test_autogoto_started_before_request_is_still_a_blocker(self):
        self.dev.handle_event('AutoGoto',{'state':'working'},4700)
        self.request_at()
        self.assertEqual(self.bridge.states['mount_home_state'],'waiting_for_goto')
        self.respond_at(101)
        self.respond_at(105)
        self.assertFalse(any(x[0]=='scope_park' for x in self.mount._queue))
        with patch.object(asiair_client.time,'monotonic',return_value=106):
            self.dev.handle_event('AutoGoto',{'state':'complete'},4700)
            self.dev._service_deferred_park()
        self.assertEqual(self.mount._queue[0][0],'scope_park')

    def test_autogoto_active_requires_completion(self):
        self.request_at()
        with patch.object(asiair_client.time,'monotonic',return_value=101):
            self.dev.handle_event('AutoGoto',{'state':'working'},4700)
        self.respond_at(103)
        self.respond_at(107)
        self.assertFalse(any(x[0]=='scope_park' for x in self.mount._queue))
        with patch.object(asiair_client.time,'monotonic',return_value=108):
            self.dev.handle_event('AutoGoto',{'state':'complete'},4700)
            self.dev._service_deferred_park()
        self.assertEqual(self.mount._queue[0][0],'scope_park')

    def test_imager_disconnect_cancels_wait(self):
        self.request_at()
        self.dev.on_channel_status(4700,False)
        self.assertIsNone(self.dev._deferred_park_started_at)
        self.assertFalse(any(x[0]=='scope_park' for x in self.mount._queue))

    def test_cannot_cancel_dispatched_park(self):
        self.request_at()
        self.respond_at(101)
        self.respond_at(105)
        self.dev.handle_command('mount_cancel_go_home','PRESS')
        self.assertEqual(self.bridge.states['mount_home_state'],'requested')
        self.assertEqual(self.mount._queue[0][0],'scope_park')

if __name__=='__main__': unittest.main()
