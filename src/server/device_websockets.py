from twisted.internet import reactor
from autobahn.twisted.websocket import WebSocketServerFactory
from autobahn.twisted.websocket import WebSocketServerProtocol
from app import socketio
import os
import sys
import json
import time
import threading
import database
import logging
import uuid

# src/common holds the shared reactor/thread-boundary helper. The server
# process doesn't otherwise put it on the path (unlike the audio/video
# services, which do it via their connection_manager shim).
_COMMON = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'common')
if _COMMON not in sys.path:
    sys.path.insert(0, _COMMON)
import reactor_safety

class ConnectionManager:
    # Lives in the coordinator process only (DC_ROLE=coordinator/all). Routes
    # and handlers import device_commands.ConnectionManager, which reaches
    # this instance directly there and over Redis from the API workers.
    instance = None

    def __init__(self):
        ConnectionManager.instance = self
        # connections is mutated on the websocket reactor thread and read
        # from Flask request threads — every access goes through the lock.
        self._lock = threading.Lock()
        self.connections = []
        self.jobs = {}

    def add_connection(self, device_id, socket):
        with self._lock:
            match = next((conn for conn in self.connections if conn['id'] == device_id), None)
        if match:
            self.remove_connection(match['id'], match['socket'])
        with self._lock:
            self.connections.append({'id':device_id, 'socket': socket})
            count = len(self.connections)
        from app import app
        with app.app_context():
            database.set_device_connected(device_id, True)
            database.close_session()
        logging.info('%d device(s) connected.', count)

    def remove_connection(self, device_id, socket):
        with self._lock:
            match = next((conn for conn in self.connections if conn['id'] == device_id and conn['socket'] == socket), None)
            if match is not None:
                self.connections = [conn for conn in self.connections if conn['id'] != device_id]
        if match is not None:
            from app import app
            with app.app_context():
                database.set_device_connected(device_id, False)
                database.close_session()
            logging.info('Device {0} has disconnected.'.format(device_id))

    def send_command_and_wait(self, device_id, command):
        with self._lock:
            match = next((conn for conn in self.connections if conn['id'] == device_id), None)
        success = False
        response = None
        if match:
            job = Job()
            command['job_id'] = job.job_id
            self.jobs[job.job_id] = job
            match['socket'].send_json(command)
            # Event wait instead of the old 0.1s busy-poll on the request
            # thread; wakes immediately when the response lands.
            job.wait()
            if job.is_complete():
                success = True
                response = job.response_data
            self.jobs.pop(job.job_id, None)
        return success, response

    def send_command(self, device_id, command):
        with self._lock:
            match = next((conn for conn in self.connections if conn['id'] == device_id), None)
        if match:
            match['socket'].send_json(command)
            return True
        return False

    def is_connected(self, device_id):
        # Lets the coordinator answer "not connected" at once instead of
        # waiting out a Job timeout for a pod that is not even here.
        with self._lock:
            return any(conn['id'] == device_id for conn in self.connections)

    def update_job(self, job_id, response):
        job = self.jobs.get(job_id, None)
        if job:
            job.add_response(response)

class Job:
    def __init__(self, timeout=15.0):
        self.job_id = str(uuid.uuid4())
        self.request_time = time.time()
        self.timeout = timeout
        self.response_time = None
        self.response_data = None
        self._done = threading.Event()

    def add_response(self, data):
        self.response_time = time.time()
        self.response_data = data
        self._done.set()

    def wait(self):
        # True the moment the response arrives; False on timeout.
        return self._done.wait(self.timeout)

    def is_complete(self):
        return self.response_time is not None

    def is_timed_out(self):
        return time.time() > self.request_time + self.timeout


class ServerProtocol(WebSocketServerProtocol):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.last_message = time.time()
        self.device_id = None

    def onOpen(self):
        logging.info('Device connected to device websocket...')

    def onMessage(self, payload, is_binary):
        self.last_message = time.time()
        if not is_binary:
            try:
                data = json.loads(payload.decode('utf-8'))
            except Exception as e:
                logging.info('Payload is not properly formatted JSON: ' + str(e))
                return
            try:
                # db.session needs an app context on this (reactor) thread —
                # without one every handler raises and the device looks dead.
                from app import app
                with app.app_context():
                    self.process_json(data)
            except Exception:
                logging.exception('Device websocket handler failed for cmd=%r', data.get('cmd'))

    def onClose(self, *args, **kwargs):
        if self.device_id:
            ConnectionManager.instance.remove_connection(self.device_id, self)

    def process_json(self, data):
        cmd = data.get('cmd', None)

        # Verify message and authenticate.
        if not cmd:
            logging.info('Message missing "cmd".')
            return
        elif cmd == 'auth':
            mac_address = data.get('key', None)
            device = database.get_devices(mac_addr=mac_address, archived=False)
            if device:
                self.device_id = device.id
                ConnectionManager.instance.add_connection(self.device_id, self)
                self.send_json({'cmd': 'auth', 'status': True})
                logging.info('Device {0} has been authenticated and is now connected.'.format(self.device_id))
                session_device = database.get_device_active_session_device(self.device_id)
                if session_device:
                    logging.info('Requesting device {0} join session {1}.'.format(self.device_id, session_device.session_id))
                    self.send_json({'cmd': 'start', 'key': session_device.processing_key})
            database.close_session()

        # Reject any connection that tries to message before authenticating.
        if not self.device_id:
            self.send_json({'cmd': 'auth', 'status': False})
            logging.info('An unauthenticated device has skipped or failed authentication...rejecting connection.')
            self.close_connection()
            return

        # Capture job responses.
        job_id = data.get('job_id', None)
        if job_id:
            ConnectionManager.instance.update_job(job_id, data)

        # Handle commands.
        if cmd == 'help':
            device_key = data.get('key', None)
            state = data.get('state', None)
            session_device = database.get_session_devices(processing_key=device_key)
            if session_device and session_device.button_pressed != state:
                session_device.button_pressed = state
                database.save_changes()
                room_name = str(session_device.session_id)
                socketio.emit('device_update', json.dumps(session_device.json()), room=room_name, namespace="/session")
            database.close_session()

    def close_connection(self):
        self.transport.loseConnection()

    def send_json(self, message):
        # send_command/_and_wait call this from Flask request threads (help
        # button, admin log fetch) — via the reactor-safety boundary.
        reactor_safety.send_json(self, message)


def run_server():
    ConnectionManager()
    factory = WebSocketServerFactory()
    factory.protocol = ServerProtocol
    reactor.listenTCP(int(os.environ.get('DC_DEVICE_WS_PORT', 9001)), factory, interface='127.0.0.1')
    thread = threading.Thread(target=reactor.run, kwargs={'installSignalHandlers': False})
    thread.daemon = True
    thread.start()

if __name__ == '__main__':
    run_server()
    data = ''
    while(data != 'q'):
        input()
        ConnectionManager.instance.send_command_and_wait(1, {'cmd': 'mac_address'})
