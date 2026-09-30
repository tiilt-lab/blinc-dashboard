"""Device commands across processes (infra audit A, multi-worker step).

The pods' websocket server (device_websockets.py, Twisted, DC_DEVICE_WS_PORT)
runs in exactly one process, the coordinator (coordinator.py). gunicorn API
workers hold no device connections, so a route that must talk to a pod
publishes the command to Redis and the coordinator forwards it:

  channel ``device_cmd``
      JSON ``{"device_id": <int>, "cmd": {...}}`` — fire and forget, or
      ``{"device_id": <int>, "cmd": {...}, "reply_key": "device_reply:<uuid>"}``
  list ``device_reply:<uuid>``  (RPUSH by the coordinator, BLPOP by the worker)
      JSON ``{"success": true, "data": {...}}`` or
      ``{"success": false, "error": "not connected" | "timeout"}``, 60 s TTL
      so a reply nobody read cannot linger.

``ConnectionManager.instance`` keeps the surface routes and handlers always
used (``send_command`` / ``send_command_and_wait``); DC_ROLE picks the
backend: the Twisted manager where the device server runs (coordinator, or
the single-process dev runner, role ``all``), the Redis proxy in API workers.
"""
import json
import logging
import os
import threading
import uuid

CHANNEL = 'device_cmd'
REPLY_PREFIX = 'device_reply:'
REPLY_TTL = 60
# device_websockets.Job waits 15 s on the pod; the worker waits that plus the
# time the reply needs to travel.
WAIT_TIMEOUT = 15.0
_REPLY_GRACE = 2

NOT_CONNECTED = {'success': False, 'error': 'not connected'}
TIMED_OUT = {'success': False, 'error': 'timeout'}
NO_COORDINATOR = {'success': False, 'error': 'no coordinator'}


def role():
    return os.environ.get('DC_ROLE', 'api').strip().lower() or 'api'


def owns_devices(role_name=None):
    """True when this process runs the Twisted device server itself."""
    return (role_name or role()) in ('coordinator', 'all')


def _redis():
    from redis_helper import r
    return r


def _text(value):
    return value.decode('utf-8') if isinstance(value, bytes) else value


class RedisCommandProxy:
    """API-worker side of the channel; same surface as the Twisted manager."""

    def __init__(self, client=None, timeout=WAIT_TIMEOUT):
        self._client = client
        self.timeout = timeout

    def _r(self):
        return self._client if self._client is not None else _redis()

    def send_command(self, device_id, command):
        # True when a coordinator received it (Redis reports the subscriber
        # count), not when the pod is connected: only the coordinator knows
        # that, and callers already treat False as "log it and carry on".
        payload = json.dumps({'device_id': device_id, 'cmd': command})
        return self._r().publish(CHANNEL, payload) > 0

    def send_command_and_wait(self, device_id, command):
        reply_key = REPLY_PREFIX + uuid.uuid4().hex
        payload = json.dumps({'device_id': device_id, 'cmd': command, 'reply_key': reply_key})
        r = self._r()
        if r.publish(CHANNEL, payload) == 0:
            logging.warning('device command for %s: no coordinator subscribed to %s', device_id, CHANNEL)
            return False, dict(NO_COORDINATOR)
        item = r.blpop(reply_key, timeout=int(self.timeout + _REPLY_GRACE))
        if item is None:
            return False, dict(TIMED_OUT)
        reply = json.loads(_text(item[1]))
        if reply.get('success'):
            return True, reply.get('data')
        return False, reply


class CommandSubscriber:
    """Coordinator side: forwards ``device_cmd`` publishes to the live
    sockets through the Twisted ConnectionManager and answers reply keys."""

    def __init__(self, manager, client=None):
        self.manager = manager
        self._client = client
        self._stop = threading.Event()
        self.thread = None

    def _r(self):
        return self._client if self._client is not None else _redis()

    def start(self):
        self.thread = threading.Thread(target=self.run, name='device-cmd-subscriber', daemon=True)
        self.thread.start()
        return self.thread

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            try:
                pubsub = self._r().pubsub(ignore_subscribe_messages=True)
                pubsub.subscribe(CHANNEL)
                logging.info('device commands: subscribed to %s', CHANNEL)
                for message in pubsub.listen():
                    if self._stop.is_set():
                        break
                    if message.get('type') == 'message':
                        self.handle(message['data'])
            except Exception as e:
                logging.warning('device commands: subscription lost (%s); retrying in 2 s', e)
                self._stop.wait(2)

    def handle(self, raw):
        try:
            msg = json.loads(_text(raw))
            device_id = msg['device_id']
            cmd = msg['cmd']
        except Exception:
            logging.warning('device commands: malformed message %r', raw)
            return
        reply_key = msg.get('reply_key')
        if not reply_key:
            try:
                self.manager.send_command(device_id, cmd)
            except Exception:
                logging.exception('device commands: send to %s failed', device_id)
            return
        # A waited command blocks up to 15 s on the pod; answer on its own
        # thread so one slow pod never holds the channel.
        threading.Thread(target=self._answer, args=(device_id, cmd, reply_key),
                         name='device-cmd-reply', daemon=True).start()

    def _answer(self, device_id, cmd, reply_key):
        try:
            if not self.manager.is_connected(device_id):
                reply = dict(NOT_CONNECTED)
            else:
                success, data = self.manager.send_command_and_wait(device_id, cmd)
                reply = {'success': True, 'data': data} if success else dict(TIMED_OUT)
        except Exception as e:
            logging.exception('device commands: waited send to %s failed', device_id)
            reply = {'success': False, 'error': str(e)}
        self.push_reply(reply_key, reply)

    def push_reply(self, reply_key, reply):
        try:
            pipe = self._r().pipeline()
            pipe.rpush(reply_key, json.dumps(reply))
            pipe.expire(reply_key, REPLY_TTL)
            pipe.execute()
        except Exception:
            logging.exception('device commands: could not push reply to %s', reply_key)


class _Dispatcher:
    # Resolved per call, not at import: the Twisted manager exists only after
    # device_websockets.run_server(), and the role is fixed by create_app().
    def __init__(self):
        self._proxy = None

    def _backend(self):
        if owns_devices():
            import device_websockets
            manager = device_websockets.ConnectionManager.instance
            if manager is None:
                raise RuntimeError('device websocket server is not running in this process')
            return manager
        if self._proxy is None:
            self._proxy = RedisCommandProxy()
        return self._proxy

    def send_command(self, device_id, command):
        return self._backend().send_command(device_id, command)

    def send_command_and_wait(self, device_id, command):
        return self._backend().send_command_and_wait(device_id, command)


class ConnectionManager:
    """What routes and handlers import: ``ConnectionManager.instance.send_command(...)``."""
    instance = _Dispatcher()
