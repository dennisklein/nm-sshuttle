# SPDX-License-Identifier: MIT
"""A fake Effects with a fake clock, for driving the Supervisor in tests."""

import heapq
import itertools
import types

from nm_sshuttle.const import AC_ACTIVATED, AC_ACTIVATING, ST_STARTED
from nm_sshuttle.profile import ProfileError
from nm_sshuttle.supervisor import Supervisor

AC = "/org/freedesktop/NetworkManager/ActiveConnection/7"
DEVICE = "/org/freedesktop/NetworkManager/Devices/9"
NM = ":1.5"


def settings(**data):
    d = {"remote": "corp", "local-user": "alice", "subnets": "10.0.0.0/8",
         "dns": "split", "dns-servers": "10.1.0.53", "dns-domains": "~corp.example"}
    d.update(data)
    return {"connection": {"id": "corp", "uuid": "u-1", "permissions": ["user:alice:"]},
            "vpn": {"data": d, "persistent": True}, "ipv4": {"method": "auto"}}


def fake_getpwnam(name):
    if name != "alice":
        raise KeyError(name)
    return types.SimpleNamespace(pw_uid=1000, pw_gid=1000, pw_dir="/home/alice")


class FakeEffects:
    def __init__(self):
        self.t = 0.0
        self.timers = []
        self.seq = itertools.count()
        self.cancelled = set()
        self.events = []
        self.link = None
        self.next_ifindex = 40
        self.cleanup_cb = None
        self.start_cbs = []
        self.stop_cbs = []
        self.device_cbs = []
        self.device_state_cbs = []
        self.ac_cbs = []
        self.first_hop_cbs = []
        self.device_answer = DEVICE   # None: no device yet; a callable: computed
        self.auto_device = True
        self.quit_called = False
        self.raise_on = {}

    # recording helpers
    def _rec(self, *event):
        self.events.append(event)
        exc = self.raise_on.get(event[0])
        if exc:
            raise exc

    def names(self):
        return [e[0] for e in self.events]

    def signals(self):
        return [e for e in self.events if e[0] in ("state", "config", "ip4", "failure")]

    def clear(self):
        self.events.clear()

    # NetworkManager
    def emit_state(self, state):
        self._rec("state", state)

    def emit_config(self, cfg):
        self._rec("config", dict(cfg))

    def emit_ip4_config(self, cfg):
        self._rec("ip4", dict(cfg))

    def emit_failure(self, reason):
        self._rec("failure", reason)

    def find_active_connection(self, uuid, done):
        self._rec("find_ac", uuid)
        self.ac_cbs.append(done)

    def find_device(self, iface, done):
        self._rec("find_device", iface)
        if self.auto_device:
            ans = self.device_answer() if callable(self.device_answer) else self.device_answer
            done(ans)
        else:
            self.device_cbs.append(done)

    def device_state(self, path, done):
        self._rec("device_state", path)
        self.device_state_cbs.append(done)

    # systemd
    def start_tunnel(self, done):
        self._rec("start_tunnel")
        self.start_cbs.append(done)

    def stop_tunnel(self, done):
        self._rec("stop_tunnel")
        self.stop_cbs.append(done)

    def run_cleanup(self, done):
        self._rec("cleanup")
        self.cleanup_cb = done

    # host
    def resolve_first_hop(self, user, remote, done):
        self._rec("first_hop", user, remote)
        self.first_hop_cbs.append(done)

    def write_tunnel_spec(self, spec):
        self._rec("spec", spec)

    def create_link(self):
        self._rec("create_link")
        self.link = self.next_ifindex
        self.next_ifindex += 1
        return self.link

    def link_ifindex(self):
        return self.link

    def remove_link(self):
        self._rec("remove_link")
        self.link = None

    def install_guard(self, networks, exclude):
        self._rec("install_guard", list(networks), list(exclude))

    def remove_guard(self):
        self._rec("remove_guard")

    def sweep(self):
        self._rec("sweep")

    # loop
    def now(self):
        return self.t

    def call_later(self, seconds, fn):
        handle = next(self.seq)
        heapq.heappush(self.timers, (self.t + seconds, handle, fn))
        return handle

    def cancel(self, handle):
        self.cancelled.add(handle)

    def advance(self, seconds):
        end = self.t + seconds
        while self.timers and self.timers[0][0] <= end + 1e-9:
            when, handle, fn = heapq.heappop(self.timers)
            if handle in self.cancelled:
                continue
            self.t = max(self.t, when)
            fn()
            if self.quit_called:
                break
        self.t = max(self.t, end)

    def flush(self):
        self._rec("flush")

    def quit(self):
        self._rec("quit")
        self.quit_called = True

    # completing async work
    def finish_start(self, ok=True, detail=""):
        self.start_cbs.pop(0)(ok, detail)

    def finish_stops(self):
        cbs, self.stop_cbs = self.stop_cbs, []
        for cb in cbs:
            cb()


def parse(s):
    from nm_sshuttle import profile
    return profile.parse(s, getpwnam=fake_getpwnam)


def make():
    fx = FakeEffects()
    sup = Supervisor(fx, parse_profile=parse)
    sup.nm_current_owner = NM
    return fx, sup


def bring_up(fx, sup, activated=True, **data):
    """Connect through to STARTED (and NM's "activated")."""
    sup.connect(settings(**data), NM)
    if fx.first_hop_cbs:
        fx.first_hop_cbs.pop(0)("203.0.113.7", None)
    fx.finish_start(True)
    assert sup.phase == "up", sup.phase
    assert sup.state == ST_STARTED
    fx.ac_cbs.pop(0)(AC, AC_ACTIVATING)
    if activated:
        sup.on_ac_state(AC, AC_ACTIVATED, 0)
    return fx, sup


def drop_and_retry(fx, sup):
    """Tunnel drop, then the first reconnect attempt succeeds: burst sent."""
    sup.on_tunnel_inactive()
    sup.on_ac_state(AC, AC_ACTIVATING, 6)    # NM's answer to STARTING
    fx.advance(1.0)
    fx.finish_start(True)


__all__ = ["AC", "DEVICE", "NM", "FakeEffects", "ProfileError", "bring_up", "drop_and_retry",
           "make", "settings", "fake_getpwnam"]
