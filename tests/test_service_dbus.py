# SPDX-License-Identifier: MIT
"""Smoke test of service.py over a private D-Bus daemon.

A fake NetworkManager and a fake systemd own their names on the same bus. The
plugin's host operations (ip, nft, subprocesses) are replaced, so this checks
the D-Bus glue: method dispatch, signal signatures and values, the systemd
job and unit watches, and the AC state subscription.
"""

import os
import shutil
import socket
import struct
import subprocess
import time

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from fakes import fake_getpwnam  # noqa: E402
from nm_sshuttle import guard, profile, service, tunnel  # noqa: E402
from nm_sshuttle.const import (AC_ACTIVATED, AC_ACTIVATING, AC_DEACTIVATED,  # noqa: E402
                               FAIL_CONNECT, ST_STARTED, ST_STARTING, ST_STOPPED)

pytestmark = pytest.mark.skipif(not shutil.which("dbus-daemon"), reason="needs dbus-daemon")

AC_PATH = "/org/freedesktop/NetworkManager/ActiveConnection/1"
PRIMARY_PATH = "/org/freedesktop/NetworkManager/ActiveConnection/0"
AC_IFACE = "org.freedesktop.NetworkManager.Connection.Active"
IP4_PATH = "/org/freedesktop/NetworkManager/IP4Config/3"
SESSION_PATH = "/org/freedesktop/login1/session/_32"
DEV_PATH = "/org/freedesktop/NetworkManager/Devices/5"
UNIT_PATH = "/org/freedesktop/systemd1/unit/nm_2dsshuttle_2dtunnel_2eservice"

NM_XML = """
<node>
  <interface name="org.freedesktop.NetworkManager">
    <method name="GetDeviceByIpIface">
      <arg name="iface" type="s" direction="in"/><arg name="device" type="o" direction="out"/>
    </method>
    <property name="ActiveConnections" type="ao" access="read"/>
    <property name="State" type="u" access="read"/>
    <property name="Connectivity" type="u" access="read"/>
    <property name="PrimaryConnection" type="o" access="read"/>
    <property name="Version" type="s" access="read"/>
  </interface>
  <interface name="org.freedesktop.NetworkManager.Connection.Active">
    <property name="Uuid" type="s" access="read"/>
    <property name="State" type="u" access="read"/>
    <property name="StateFlags" type="u" access="read"/>
    <property name="Vpn" type="b" access="read"/>
    <property name="Type" type="s" access="read"/>
    <property name="Ip4Config" type="o" access="read"/>
    <signal name="StateChanged"><arg type="u"/><arg type="u"/></signal>
  </interface>
  <interface name="org.freedesktop.NetworkManager.Device">
    <property name="State" type="u" access="read"/>
  </interface>
  <interface name="org.freedesktop.NetworkManager.IP4Config">
    <property name="AddressData" type="aa{sv}" access="read"/>
    <property name="Gateway" type="s" access="read"/>
  </interface>
</node>"""

LOGIN_XML = """
<node>
  <interface name="org.freedesktop.login1.Manager">
    <method name="Inhibit">
      <arg type="s" direction="in"/><arg type="s" direction="in"/>
      <arg type="s" direction="in"/><arg type="s" direction="in"/>
      <arg type="h" direction="out"/>
    </method>
    <method name="GetUser"><arg type="u" direction="in"/><arg type="o" direction="out"/></method>
    <signal name="PrepareForSleep"><arg type="b"/></signal>
  </interface>
  <interface name="org.freedesktop.login1.User">
    <property name="Sessions" type="a(so)" access="read"/>
  </interface>
  <interface name="org.freedesktop.login1.Session">
    <property name="LockedHint" type="b" access="read"/>
  </interface>
</node>"""

SYSTEMD_XML = """
<node>
  <interface name="org.freedesktop.systemd1.Manager">
    <method name="Subscribe"/>
    <method name="ResetFailedUnit"><arg type="s" direction="in"/></method>
    <method name="StartUnit">
      <arg type="s" direction="in"/><arg type="s" direction="in"/>
      <arg type="o" direction="out"/>
    </method>
    <method name="StopUnit">
      <arg type="s" direction="in"/><arg type="s" direction="in"/>
      <arg type="o" direction="out"/>
    </method>
    <signal name="JobRemoved">
      <arg type="u"/><arg type="o"/><arg type="s"/><arg type="s"/>
    </signal>
  </interface>
  <interface name="org.freedesktop.systemd1.Unit">
    <property name="ActiveState" type="s" access="read"/>
    <property name="InvocationID" type="ay" access="read"/>
  </interface>
</node>"""


def u32_ip(value):
    return socket.inet_ntoa(struct.pack("=I", value))


def iterate_until(cond, timeout=5.0):
    ctx = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        ctx.iteration(False) or time.sleep(0.002)


@pytest.fixture
def bus(tmp_path):
    sock = tmp_path / "bus"
    conf = tmp_path / "bus.conf"
    conf.write_text(f"""<busconfig>
  <type>session</type>
  <listen>unix:path={sock}</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
""")
    proc = subprocess.Popen(["dbus-daemon", f"--config-file={conf}", "--nofork"])
    for _ in range(200):
        if sock.exists():
            break
        time.sleep(0.01)
    yield f"unix:path={sock}"
    proc.terminate()
    proc.wait()


def connect(address):
    return Gio.DBusConnection.new_for_address_sync(
        address, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
        | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)


class FakeWorld:
    """NetworkManager and systemd, as far as the plugin sees them."""

    def __init__(self, address):
        self.conn = connect(address)
        self.ac_state = AC_ACTIVATING
        self.unit_state = "inactive"
        self.nm_state = 70
        self.primary_state = AC_ACTIVATED
        self.address = "192.168.1.20"
        self.locked = False
        self.inhibit_fds = []
        self.signals = []
        self.jobs = 0
        self.calls = []
        nm = Gio.DBusNodeInfo.new_for_xml(NM_XML)
        sd = Gio.DBusNodeInfo.new_for_xml(SYSTEMD_XML)
        self.conn.register_object("/org/freedesktop/NetworkManager", nm.interfaces[0],
                                  self.on_call, self.on_prop, None)
        self.conn.register_object(AC_PATH, nm.interfaces[1], self.on_call, self.on_prop, None)
        self.conn.register_object(DEV_PATH, nm.interfaces[2], self.on_call, self.on_prop, None)
        self.conn.register_object("/org/freedesktop/systemd1", sd.interfaces[0],
                                  self.on_call, self.on_prop, None)
        self.conn.register_object(UNIT_PATH, sd.interfaces[1], self.on_call, self.on_prop, None)
        self.conn.register_object(PRIMARY_PATH, nm.interfaces[1], self.on_call, self.on_prop, None)
        self.conn.register_object(IP4_PATH, nm.interfaces[3], self.on_call, self.on_prop, None)
        lg = Gio.DBusNodeInfo.new_for_xml(LOGIN_XML)
        self.conn.register_object("/org/freedesktop/login1", lg.interfaces[0], self.on_call,
                                  self.on_prop, None)
        self.conn.register_object("/org/freedesktop/login1/user/_1000", lg.interfaces[1],
                                  self.on_call, self.on_prop, None)
        self.conn.register_object(SESSION_PATH, lg.interfaces[2], self.on_call, self.on_prop,
                                  None)
        for name in ("org.freedesktop.NetworkManager", "org.freedesktop.systemd1",
                     "org.freedesktop.login1"):
            self.conn.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                                "org.freedesktop.DBus", "RequestName",
                                GLib.Variant("(su)", (name, 4)), None,
                                Gio.DBusCallFlags.NONE, 1000, None)
        self.conn.signal_subscribe(None, "org.freedesktop.NetworkManager.VPN.Plugin", None,
                                   "/org/freedesktop/NetworkManager/VPN/Plugin", None,
                                   Gio.DBusSignalFlags.NONE, self.on_plugin_signal)

    # what NetworkManager and systemd answer
    def on_prop(self, conn, sender, path, iface, prop):
        values = {
            "ActiveConnections": GLib.Variant("ao", [PRIMARY_PATH, AC_PATH]),
            "Uuid": GLib.Variant("s", "uuid-1" if path == AC_PATH else "wifi-1"),
            "Vpn": GLib.Variant("b", path == AC_PATH),
            "Type": GLib.Variant("s", "vpn" if path == AC_PATH else "802-11-wireless"),
            "StateFlags": GLib.Variant("u", 0),
            "ActiveState": GLib.Variant("s", self.unit_state),
            "InvocationID": GLib.Variant("ay", bytes(16)),
            "Connectivity": GLib.Variant("u", 4),
            "PrimaryConnection": GLib.Variant("o", PRIMARY_PATH),
            "Version": GLib.Variant("s", "1.56.1"),
            "Ip4Config": GLib.Variant("o", IP4_PATH if path == PRIMARY_PATH else "/"),
            "AddressData": GLib.Variant("aa{sv}", [{"address": GLib.Variant("s", self.address),
                                                    "prefix": GLib.Variant("u", 24)}]),
            "Gateway": GLib.Variant("s", "192.168.1.1"),
            "Sessions": GLib.Variant("a(so)", [("2", SESSION_PATH)]),
            "LockedHint": GLib.Variant("b", self.locked),
        }
        if prop == "State":
            if path == "/org/freedesktop/NetworkManager":
                return GLib.Variant("u", self.nm_state)
            if path == PRIMARY_PATH:
                return GLib.Variant("u", self.primary_state)
            return GLib.Variant("u", self.ac_state if path == AC_PATH else 10)
        return values.get(prop)

    def on_call(self, conn, sender, path, iface, method, params, inv):
        self.calls.append(method)
        if method == "GetDeviceByIpIface":
            inv.return_value(GLib.Variant("(o)", (DEV_PATH,)))
        elif method == "GetUser":
            inv.return_value(GLib.Variant("(o)", ("/org/freedesktop/login1/user/_1000",)))
        elif method == "Inhibit":
            r, w = os.pipe()
            self.inhibit_fds.append(w)
            inv.return_value_with_unix_fd_list(GLib.Variant("(h)", (0,)),
                                               Gio.UnixFDList.new_from_array([r]))
        elif method in ("StartUnit", "StopUnit"):
            self.jobs += 1
            job = f"/org/freedesktop/systemd1/job/{self.jobs}"
            inv.return_value(GLib.Variant("(o)", (job,)))
            self.unit_state = "active" if method == "StartUnit" else "inactive"
            GLib.idle_add(self.job_done, job, self.jobs)
        else:
            inv.return_value(None)

    def job_done(self, job, jid):
        self.set_unit(self.unit_state)
        self.conn.emit_signal(None, "/org/freedesktop/systemd1",
                              "org.freedesktop.systemd1.Manager", "JobRemoved",
                              GLib.Variant("(uoss)", (jid, job, "nm-sshuttle-tunnel.service",
                                                      "done")))
        return False

    def set_unit(self, state):
        self.unit_state = state
        self.conn.emit_signal(None, UNIT_PATH, "org.freedesktop.DBus.Properties",
                              "PropertiesChanged",
                              GLib.Variant("(sa{sv}as)", ("org.freedesktop.systemd1.Unit",
                                           {"ActiveState": GLib.Variant("s", state)}, [])))

    def props_changed(self, path, iface, props):
        self.conn.emit_signal(None, path, "org.freedesktop.DBus.Properties", "PropertiesChanged",
                              GLib.Variant("(sa{sv}as)", (iface, props, [])))

    def set_ac(self, state, reason=0):
        self.ac_state = state
        self.conn.emit_signal(None, AC_PATH, AC_IFACE, "StateChanged",
                              GLib.Variant("(uu)", (state, reason)))
        self.props_changed(AC_PATH, AC_IFACE, {"State": GLib.Variant("u", state)})

    # what NetworkManager sees from the plugin
    def on_plugin_signal(self, conn, sender, path, iface, name, params):
        self.signals.append((name, params.unpack()[0]))
        # Like NM: STARTING after "activated" (a reconnect) makes the connection
        # "activating" at once, before the plugin's attempt can finish.
        if name == "StateChanged" and params.unpack()[0] == ST_STARTING \
                and self.ac_state == AC_ACTIVATED:
            self.set_ac(AC_ACTIVATING, 6)

    def names(self):
        return [n if n != "StateChanged" else f"State{v}" for n, v in self.signals]

    def call(self, dest, path, iface, method, args):
        """Call asynchronously: the plugin runs on this thread's main loop."""
        out = []
        self.conn.call(dest, path, iface, method, args, None, Gio.DBusCallFlags.NONE, 2000,
                       None, lambda c, res: out.append(c.call_finish(res)))
        iterate_until(lambda: out)
        return out[0]

    def call_plugin(self, method, args=None, iface="org.freedesktop.NetworkManager.VPN.Plugin"):
        return self.call("org.freedesktop.NetworkManager.sshuttle",
                         "/org/freedesktop/NetworkManager/VPN/Plugin", iface, method, args)


def settings_variant():
    return GLib.Variant("(a{sa{sv}})", ({
        "connection": {"id": GLib.Variant("s", "corp"), "uuid": GLib.Variant("s", "uuid-1"),
                       "permissions": GLib.Variant("as", ["user:alice:"])},
        "vpn": {"persistent": GLib.Variant("b", True),
                "data": GLib.Variant("a{ss}", {
                    "remote": "corp", "local-user": "alice", "subnets": "10.0.0.0/8",
                    "dns": "split", "dns-servers": "10.1.0.53",
                    "dns-domains": "~corp.example", "gateway": "203.0.113.7"})},
        "ipv4": {"method": GLib.Variant("s", "auto")},
    },))


@pytest.fixture
def world(bus, monkeypatch, tmp_path):
    host = {"link": None, "ops": []}

    def create_link(name="nmss0"):
        host["link"] = 17
        host["ops"].append("create_link")
        return 17

    def remove_link(name="nmss0"):
        host["link"] = None
        host["ops"].append("remove_link")
        return True

    monkeypatch.setattr(guard, "create_link", create_link)
    monkeypatch.setattr(guard, "remove_link", remove_link)
    monkeypatch.setattr(guard, "link_ifindex", lambda name="nmss0": host["link"])
    monkeypatch.setattr(guard, "install_guard", lambda n, e: host["ops"].append("guard"))
    monkeypatch.setattr(guard, "remove_guard", lambda: host["ops"].append("unguard"))
    monkeypatch.setattr(tunnel, "sweep", lambda: None)
    monkeypatch.setattr(tunnel, "nft_health", lambda: {"guard": True, "sshuttle": True})
    monkeypatch.setattr(service.pwd, "getpwnam", fake_getpwnam)
    monkeypatch.setattr(service.Service, "run_cleanup", lambda self, done: done())
    monkeypatch.setattr(service.Service, "write_tunnel_spec", lambda self, spec: None)

    fake = FakeWorld(bus)
    loop = GLib.MainLoop()
    svc = service.Service(connect(bus), loop)
    svc.sup.parse_profile = lambda s: profile.parse(s, getpwnam=fake_getpwnam)
    svc.quit = lambda: setattr(svc, "quitted", True)
    svc.quitted = False
    svc.owned = False
    svc.on_name_acquired = lambda conn, name: setattr(svc, "owned", True)
    svc.run()
    iterate_until(lambda: svc.owned and svc.sup.phase == "idle" and svc.sup.nm_current_owner)
    return fake, svc, host


def test_connect_reconnect_and_link_loss(world):
    fake, svc, host = world
    fake.call_plugin("Connect", settings_variant())
    iterate_until(lambda: f"State{ST_STARTED}" in fake.names())
    assert fake.names() == [f"State{ST_STARTING}", "Config", "Ip4Config", f"State{ST_STARTED}"]
    cfg = fake.signals[1][1]
    assert u32_ip(cfg["gateway"]) == "203.0.113.7" and cfg["tundev"] == "nmss0"
    assert cfg["can-persist"] is True and cfg["has-ip4"] is True
    ip4 = fake.signals[2][1]
    assert u32_ip(ip4["address"]) == "192.0.0.8" and ip4["prefix"] == 32
    assert [u32_ip(a) for a in ip4["dns"]] == ["10.1.0.53"]
    assert ip4["domains"] == ["~corp.example"] and ip4["never-default"] is True
    assert host["ops"] == ["create_link", "guard"]

    fake.set_ac(AC_ACTIVATED)
    iterate_until(lambda: svc.sup.link_managed is False)

    # the tunnel unit fails: STARTING, then the reconnect burst after 1 s
    fake.signals.clear()
    fake.set_unit("failed")
    iterate_until(lambda: fake.names() == [f"State{ST_STARTING}"])
    iterate_until(lambda: f"State{ST_STARTED}" in fake.names(), timeout=5)
    assert fake.names() == [f"State{ST_STARTING}", "Config", "Ip4Config", "Ip4Config",
                            f"State{ST_STARTED}"]
    assert [u32_ip(a) for a in fake.signals[2][1]["nbns"]] == ["192.0.0.10"]
    assert "nbns" not in fake.signals[3][1]
    fake.set_ac(AC_ACTIVATED)
    iterate_until(lambda: svc.sup.phase == "up")

    # nmss0 vanishes: Config, Failure, STOPPED, and the link check finds it alone
    fake.signals.clear()
    fake.set_unit("failed")
    iterate_until(lambda: fake.names() == [f"State{ST_STARTING}"])
    iterate_until(lambda: svc.sup.ac_state == AC_ACTIVATING)
    fake.signals.clear()
    host["link"] = None
    iterate_until(lambda: f"State{ST_STOPPED}" in fake.names())
    assert fake.names() == ["Config", "Failure", f"State{ST_STOPPED}"]
    assert fake.signals[1][1] == FAIL_CONNECT
    fake.call_plugin("Disconnect")
    fake.set_ac(AC_DEACTIVATED)
    iterate_until(lambda: svc.sup.phase == "idle")
    assert fake.names().count(f"State{ST_STOPPED}") == 1
    assert host["ops"][-1] == "unguard"


def test_sigterm_in_a_gap_waits_for_disconnect(world):
    fake, svc, host = world
    fake.call_plugin("Connect", settings_variant())
    iterate_until(lambda: svc.sup.phase == "up")
    iterate_until(lambda: f"State{ST_STARTED}" in fake.names())   # all of it arrived
    fake.set_ac(AC_ACTIVATED)
    iterate_until(lambda: svc.sup.ac_state == AC_ACTIVATED)
    fake.signals.clear()
    fake.set_unit("failed")
    iterate_until(lambda: fake.names() == [f"State{ST_STARTING}"])
    iterate_until(lambda: svc.sup.ac_state == AC_ACTIVATING)
    fake.signals.clear()
    os.kill(os.getpid(), 15)
    iterate_until(lambda: f"State{ST_STOPPED}" in fake.names())
    assert fake.names() == ["Config", "Failure", f"State{ST_STOPPED}"]
    time.sleep(0.05)
    iterate_until(lambda: "StopUnit" in fake.calls)
    assert not svc.quitted
    fake.call_plugin("Disconnect")
    fake.set_ac(AC_DEACTIVATED)
    iterate_until(lambda: svc.quitted)
    assert "remove_link" in host["ops"]


def test_need_secrets_and_state_property(world):
    fake, svc, host = world
    r = fake.call_plugin("NeedSecrets", GLib.Variant("(a{sa{sv}})", ({},)))
    assert r.unpack() == ("",)
    r = fake.call_plugin("Get", GLib.Variant("(ss)", ("org.freedesktop.NetworkManager.VPN.Plugin",
                                                      "State")),
                         iface="org.freedesktop.DBus.Properties")
    assert r.unpack() == (1,)


def test_network_and_logind_watchers(world):
    fake, svc, host = world
    sup = svc.sup
    iterate_until(lambda: sup.net_key is not None)
    assert sup.uplink is True and sup.connectivity == 4 and sup.nm_version == "1.56.1"
    assert sup.net_key == ("wifi-1", ("192.168.1.20/24",), "192.168.1.1")

    fake.call_plugin("Connect", settings_variant())
    iterate_until(lambda: sup.phase == "up" and svc.inhibit_fd is not None)
    iterate_until(lambda: f"State{ST_STARTED}" in fake.names())   # all of it arrived
    fake.set_ac(AC_ACTIVATED)
    iterate_until(lambda: sup.ac_state == AC_ACTIVATED)

    # a new address on the primary connection: a drop, STARTING, and an at-once
    # attempt. NM's global State reads CONNECTING meanwhile; it does not matter.
    fake.signals.clear()
    fake.nm_state = 40
    fake.address = "10.20.0.7"
    fake.props_changed(IP4_PATH, "org.freedesktop.NetworkManager.IP4Config",
                       {"AddressData": GLib.Variant("aa{sv}", [])})
    iterate_until(lambda: sup.net_key[1] == ("10.20.0.7/24",))
    iterate_until(lambda: f"State{ST_STARTED}" in fake.names(), timeout=2)
    assert fake.names()[0] == f"State{ST_STARTING}" and sup.uplink is True
    fake.nm_state = 70
    fake.set_ac(AC_ACTIVATED)
    iterate_until(lambda: sup.phase == "up")

    # the uplink goes away: offline, and the tunnel drops
    fake.signals.clear()
    fake.primary_state = AC_DEACTIVATED
    fake.props_changed(PRIMARY_PATH, AC_IFACE, {"State": GLib.Variant("u", AC_DEACTIVATED)})
    iterate_until(lambda: sup.uplink is False and sup.phase == "gap")
    iterate_until(lambda: fake.names() == [f"State{ST_STARTING}"])
    fake.primary_state = AC_ACTIVATED
    fake.props_changed(PRIMARY_PATH, AC_IFACE, {"State": GLib.Variant("u", AC_ACTIVATED)})
    iterate_until(lambda: sup.uplink is True)
    iterate_until(lambda: f"State{ST_STARTED}" in fake.names(), timeout=5)
    fake.set_ac(AC_ACTIVATED)
    iterate_until(lambda: sup.phase == "up")

    # suspend: the inhibitor goes once the tunnel is stopped; resume takes it again
    fake.conn.emit_signal(None, "/org/freedesktop/login1", "org.freedesktop.login1.Manager",
                          "PrepareForSleep", GLib.Variant("(b)", (True,)))
    iterate_until(lambda: sup.asleep and svc.inhibit_fd is None)
    fake.conn.emit_signal(None, "/org/freedesktop/login1", "org.freedesktop.login1.Manager",
                          "PrepareForSleep", GLib.Variant("(b)", (False,)))
    iterate_until(lambda: not sup.asleep and svc.inhibit_fd is not None)

    # the lock state, read through the user's sessions
    answers = []
    svc.session_locked("alice", answers.append)
    iterate_until(lambda: answers)
    fake.locked = True
    svc.session_locked("alice", answers.append)
    iterate_until(lambda: len(answers) == 2)
    assert answers == [False, True]
    seen = []
    sup.on_lock_changed = lambda: seen.append(1)
    svc.logind.on_lock_changed = sup.on_lock_changed
    fake.props_changed(SESSION_PATH, "org.freedesktop.login1.Session",
                       {"LockedHint": GLib.Variant("b", True)})
    iterate_until(lambda: seen)


def test_probe_needs_data_from_the_far_end():
    svc = service.Service(None, None)
    banner, silent = socket.socket(), socket.socket()
    for s in (banner, silent):
        s.bind(("127.0.0.1", 0))
        s.listen(1)
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    closed_port = closed.getsockname()[1]
    closed.close()

    def serve():
        try:
            c, _ = banner.accept()
            c.sendall(b"SSH-2.0-test\r\n")
            c.close()
        except BlockingIOError:
            return True
        return False
    banner.setblocking(False)
    GLib.timeout_add(5, serve)

    results = {}
    svc.probe("127.0.0.1", banner.getsockname()[1], 2, lambda ok, d: results.update(banner=ok))
    svc.probe("127.0.0.1", silent.getsockname()[1], 0.3,
              lambda ok, d: results.update(silent=(ok, d)))
    svc.probe("127.0.0.1", closed_port, 2, lambda ok, d: results.update(closed=ok))
    iterate_until(lambda: len(results) == 3)
    assert results["banner"] is True
    assert results["silent"] == (False, "no data within 0.3 s")
    assert results["closed"] is False
    banner.close()
    silent.close()
