"""A small in-memory stand-in for redis-py used by the multi-worker tests.

Covers only what src/server/redis_sync.py, device_commands.py, watchers.py
and posthoc_queue.py call: strings with NX/EX, lists (push/pop/trim/range/
rem/blocking pops), hashes, counters, publish with synchronous subscribers,
and a pipeline that replays its calls on execute(). Values are returned as
str, like the decode_responses=True client the server uses. ``fail=True``
makes every command raise (the Redis-down fallbacks); ``now`` is a manual
clock for TTLs.
"""
import time


class FakeRedis:
    def __init__(self):
        self.store = {}     # key -> value (str | list | dict)
        self.expires = {}   # key -> absolute deadline on self.now
        self.now = 1000.0
        self.fail = False
        self.subscribers = []   # callables invoked synchronously by publish()
        self.published = []     # (channel, message)

    # -- housekeeping ------------------------------------------------------
    def _check(self):
        if self.fail:
            raise ConnectionError("redis down")

    def _alive(self, key):
        deadline = self.expires.get(key)
        if deadline is not None and self.now >= deadline:
            self.store.pop(key, None)
            self.expires.pop(key, None)
        return key in self.store

    def ttl(self, key):
        self._check()
        if not self._alive(key):
            return -2
        deadline = self.expires.get(key)
        return -1 if deadline is None else int(deadline - self.now)

    def expire(self, key, seconds):
        self._check()
        if not self._alive(key):
            return False
        self.expires[key] = self.now + int(seconds)
        return True

    def delete(self, *keys):
        self._check()
        n = 0
        for key in keys:
            if self._alive(key):
                del self.store[key]
                self.expires.pop(key, None)
                n += 1
        return n

    def exists(self, key):
        self._check()
        return 1 if self._alive(key) else 0

    def flushall(self):
        self.store.clear()
        self.expires.clear()

    # -- strings -------------------------------------------------------------
    def set(self, key, value, ex=None, nx=False):
        self._check()
        if nx and self._alive(key):
            return None
        self.store[key] = str(value)
        if ex is not None:
            self.expires[key] = self.now + int(ex)
        else:
            self.expires.pop(key, None)
        return True

    def get(self, key):
        self._check()
        return self.store.get(key) if self._alive(key) else None

    def incr(self, key):
        self._check()
        value = int(self.store[key]) + 1 if self._alive(key) else 1
        self.store[key] = str(value)
        return value

    def decr(self, key):
        self._check()
        value = int(self.store[key]) - 1 if self._alive(key) else -1
        self.store[key] = str(value)
        return value

    # -- lists ---------------------------------------------------------------
    def _list(self, key):
        if not self._alive(key):
            self.store[key] = []
        return self.store[key]

    def lpush(self, key, *values):
        self._check()
        lst = self._list(key)
        for v in values:
            lst.insert(0, str(v))
        return len(lst)

    def rpush(self, key, *values):
        self._check()
        lst = self._list(key)
        lst.extend(str(v) for v in values)
        return len(lst)

    def ltrim(self, key, start, end):
        self._check()
        if self._alive(key):
            lst = self.store[key]
            end = len(lst) - 1 if end == -1 else end
            self.store[key] = lst[start:end + 1]
        return True

    def lrange(self, key, start, end):
        self._check()
        if not self._alive(key):
            return []
        lst = self.store[key]
        end = len(lst) - 1 if end == -1 else end
        return list(lst[start:end + 1])

    def llen(self, key):
        self._check()
        return len(self.store[key]) if self._alive(key) else 0

    def lrem(self, key, count, value):
        self._check()
        if not self._alive(key):
            return 0
        before = len(self.store[key])
        self.store[key] = [v for v in self.store[key] if v != str(value)]
        return before - len(self.store[key])

    def _bpop(self, key, timeout, index):
        deadline = time.time() + (timeout or 3600)
        while True:
            self._check()
            if self._alive(key) and self.store[key]:
                value = self.store[key].pop(index)
                if not self.store[key]:
                    del self.store[key]
                return (key, value)
            if time.time() >= deadline:
                return None
            time.sleep(0.005)

    def blpop(self, key, timeout=0):
        return self._bpop(key, timeout, 0)

    def brpop(self, key, timeout=0):
        return self._bpop(key, timeout, -1)

    # -- hashes --------------------------------------------------------------
    def hset(self, key, field=None, value=None, mapping=None):
        self._check()
        if not self._alive(key):
            self.store[key] = {}
        items = dict(mapping or {})
        if field is not None:
            items[field] = value
        self.store[key].update({k: str(v) for k, v in items.items()})
        return len(items)

    def hgetall(self, key):
        self._check()
        return dict(self.store[key]) if self._alive(key) else {}

    def hdel(self, key, *fields):
        self._check()
        if not self._alive(key):
            return 0
        n = 0
        for f in fields:
            if self.store[key].pop(f, None) is not None:
                n += 1
        return n

    # -- pub/sub -------------------------------------------------------------
    def publish(self, channel, message):
        self._check()
        self.published.append((channel, message))
        for callback in self.subscribers:
            callback(channel, message)
        return len(self.subscribers)

    # -- pipeline ------------------------------------------------------------
    def pipeline(self):
        return _Pipeline(self)


class _Pipeline:
    def __init__(self, r):
        self.r = r
        self.calls = []

    def __getattr__(self, name):
        method = getattr(self.r, name)

        def record(*args, **kwargs):
            self.calls.append((method, args, kwargs))
            return self
        return record

    def execute(self):
        results = [m(*a, **k) for m, a, k in self.calls]
        self.calls = []
        return results
