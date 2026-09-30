"""Post-hoc pod claims (audit C.4): owner token ``<pid>:<boot-id>`` instead of
"1", owner-checked release (compare-and-delete), renewal for runs longer than
the TTL, and stale-claim clearing at service start (only claims whose pid is
dead on this host).
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "common"))

import distributed_claim as dc  # noqa: E402
from pod_registry import PodRegistry  # noqa: E402
from test_phase2_posthoc_gpu_lease import FakeRedis  # noqa: E402


def _claim(r, owner=None, prefix="t:"):
    return dc.DistributedClaim(redis_factory=lambda: r, prefix=prefix, owner=owner)


def test_claim_value_is_pid_and_boot_id():
    r = FakeRedis()
    assert _claim(r).try_claim("pod", ttl=100)
    value, expires = r.store["t:pod"]
    assert value == dc.OWNER_TOKEN == "%d:%s" % (os.getpid(), dc.BOOT_ID)
    assert expires == r.now + 100
    assert dc.parse_owner(value) == (os.getpid(), dc.BOOT_ID)
    assert dc.parse_owner("1") is None and dc.parse_owner(b"12:boot") == (12, "boot")


def test_release_is_compare_and_delete():
    r = FakeRedis()
    a, b = _claim(r, "a"), _claim(r, "b")
    assert a.try_claim("pod", ttl=100)
    r.now += 101                    # a's claim lapsed mid-run
    assert b.try_claim("pod", ttl=100)
    a.release("pod")                # the old holder's release
    assert r.store["t:pod"][0] == "b", "b's claim survives a's release"
    assert b.try_claim("pod") is False
    b.release("pod")
    assert "t:pod" not in r.store


def test_renew_extends_only_a_claim_still_held():
    r = FakeRedis()
    a, b = _claim(r, "a"), _claim(r, "b")
    assert a.try_claim("pod", ttl=100)
    r.now += 60
    assert a.renew("pod", 100) is True
    assert r.expires_at("t:pod") == r.now + 100
    r.now += 101
    assert b.try_claim("pod", ttl=100)
    assert a.renew("pod", 100) is False


def test_memory_fallback_renews_while_held():
    c = dc.DistributedClaim(redis_factory=None)
    assert c.try_claim("pod")
    assert c.renew("pod", 10) is True
    c.release("pod")
    assert c.renew("pod", 10) is False


def test_clear_stale_only_touches_dead_pids_of_this_boot_and_legacy_values():
    r = FakeRedis()
    r.set("audio-posthoc:dead", "999999:" + dc.BOOT_ID)
    r.set("audio-posthoc:alive", "4242:" + dc.BOOT_ID)
    r.set("audio-posthoc:otherboot", "999999:some-other-boot")
    r.set("audio-posthoc:legacy", "1")
    r.set("video-posthoc:dead", "999999:" + dc.BOOT_ID)   # another prefix
    c = _claim(r, prefix="audio-posthoc:")
    cleared = c.clear_stale(alive=lambda pid: pid == 4242)
    assert sorted(cleared) == ["audio-posthoc:dead", "audio-posthoc:legacy"]
    assert set(r.store) == {"audio-posthoc:alive", "audio-posthoc:otherboot", "video-posthoc:dead"}


def test_pid_alive_against_real_processes():
    assert dc.pid_alive(os.getpid()) is True
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()   # reaped: the pid is gone
    assert dc.pid_alive(p.pid) is False


def test_clear_stale_survives_redis_errors():
    r = FakeRedis()
    r.fail = True
    assert _claim(r).clear_stale() == []


# ---- PodRegistry --------------------------------------------------------------

def test_registry_renews_every_held_claim_and_reports_lost_ones():
    r = FakeRedis()
    reg = PodRegistry(redis_factory=lambda: r, prefix="t:", ttl=100, renew_interval=0)
    assert reg.try_claim("a") and reg.try_claim("b")
    r.now += 60
    assert reg.renew_all() == []
    assert r.expires_at("t:a") == r.expires_at("t:b") == r.now + 100
    r.delete("t:b")                 # expired and gone
    assert reg.renew_all() == ["b"]


def test_registry_renewer_thread_starts_with_the_first_claim():
    r = FakeRedis()
    reg = PodRegistry(redis_factory=lambda: r, prefix="t:", ttl=100, renew_interval=0.05)
    assert reg._renewer is None
    reg.try_claim("a")
    deadline = time.time() + 2
    while r.renews == 0 and time.time() < deadline:
        time.sleep(0.02)
    assert r.renews >= 1


def test_registry_pop_releases_only_its_own_claim():
    r = FakeRedis()
    reg = PodRegistry(redis_factory=lambda: r, prefix="t:", ttl=100, renew_interval=0)
    other = _claim(r, owner="elsewhere")
    assert reg.try_claim("pod", "proc")
    r.now += 101
    assert other.try_claim("pod", ttl=100)
    assert reg.pop("pod", None) == "proc"
    assert r.store["t:pod"][0] == "elsewhere"


def test_registry_clear_stale_uses_its_prefix():
    r = FakeRedis()
    r.set("video-posthoc:legacy", "1")
    r.set("audio-posthoc:legacy", "1")
    reg = PodRegistry(redis_factory=lambda: r, prefix="video-posthoc:", renew_interval=0)
    assert reg.clear_stale() == ["video-posthoc:legacy"]
    assert "audio-posthoc:legacy" in r.store
