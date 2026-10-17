"""Regression tests: active camera streams must not starve MQTT control RPCs."""
import sys, types, unittest, json, time
from pathlib import Path
stub=types.ModuleType('mqtt_bridge')
stub.MqttBridge=type('MqttBridge',(),{})
sys.modules['mqtt_bridge']=stub
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from asiair_client import AsiairDevice

class Bridge:
    def __init__(self): self.states={}
    def register_command_handler(self,*a,**k): pass
    def set_select_options(self,*a,**k): pass
    def publish_state(self,node_id,key,value,**k): self.states[key]=value
    def publish_unknown(self,node_id,key): self.states[key]=None
    def publish_device_availability(self,*a,**k): pass
    def publish_port4400_availability(self,*a,**k): pass
    def update_metadata(self,*a,**k): pass

class AlwaysEventSocket:
    def __init__(self,dev):
        self.dev=dev;self.count=0;self.sent=[]
    def sendall(self,data): self.sent.append(json.loads(data))
    def recv(self,n):
        self.count+=1
        if self.count==3: self.dev.handle_command('stop_autorun_plan','PRESS')
        if self.count>=9: self.dev.stop_event.set()
        return b'{"Event":"StreamStillActive"}\r\n'

class TestPriorityQueue(unittest.TestCase):
    def setUp(self):
        self.bridge=Bridge()
        self.dev=AsiairDevice('test','ASIAIR', '127.0.0.1',self.bridge,30,8)
        self.ch=self.dev._channel(4700)
        self.ch._rpc_authenticated=True

    def test_pause_sent_despite_continuous_events(self):
        sock=AlwaysEventSocket(self.dev)
        self.ch._sock=sock
        # Isolate the control scheduling behavior, not startup telemetry.
        self.ch._enqueue_poll=lambda **kw: None
        self.ch._connected_loop()
        self.assertTrue(any(x.get('method')=='stop_exposure' and x.get('params')==[True] for x in sock.sent))
        self.assertEqual(self.bridge.states['capture_command_state'],'pause_sent_waiting_reply')

    def test_ack_without_result_field_advances_status(self):
        # No transport roundtrip required. ASIAIR firmware may return only code=0.
        sock=AlwaysEventSocket(self.dev)
        self.ch._sock=sock
        self.ch._pending[23]=('stop_exposure','cmd:Capture:pause:stop_autorun_plan',time.monotonic())
        self.ch._handle_message({'id':23,'code':0})
        self.assertEqual(self.bridge.states['capture_command_state'],'pause_accepted_unconfirmed')
        self.assertEqual(sock.sent[0]['method'],'get_app_state')

if __name__=='__main__': unittest.main()
