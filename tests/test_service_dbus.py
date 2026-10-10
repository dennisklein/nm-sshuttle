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
DEV_PATH = "/org/freedesktop/NetworkManager/Devices/5"
UNIT_PATH = "/org/freedesktop/systemd1/unit/nm_2dsshuttle_2dtunnel_2eservice"

NM_XML = """
<node>
  <interface name="org.freedesktop.NetworkManager">
    <method name="GetDeviceByIpIface">
      <arg name="iface" type="s" direction="in"/><arg name="device" type="o" direction="out"/>
    </method>
    <property name="ActiveConnections" type="ao" access="read"/>
  </interface>
  <interface name="org.freedesktop.NetworkManager.Connection.Active">
    <property name="Uuid" type="s" access="read"/>
    <property name="State" type="u" access="read"/>
    <signal name="StateChanged"><arg type="u"/><arg type="u"/></signal>
  </interface>
  <interface name="org.freedesktop.NetworkManager.Device">
    <property name="State" type="u" access="read"/>
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
        for name in ("org.freedesktop.NetworkManager", "org.freedesktop.systemd1"):
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
            "ActiveConnections": GLib.Variant("ao", [AC_PATH]),
            "Uuid": GLib.Variant("s", "uuid-1"),
            "ActiveState": GLib.Variant("s", self.unit_state),
            "InvocationID": GLib.Variant("ay", bytes(16)),
        }
        if prop == "State":
            return GLib.Variant("u", self.ac_state if path == AC_PATH else 10)
        return values.get(prop)

    def on_call(self, conn, sender, path, iface, method, params, inv):
        self.calls.append(method)
        if method == "GetDeviceByIpIface":
            inv.return_value(GLib.Variant("(o)", (DEV_PATH,)))
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

    def set_ac(self, state, reason=0):
        self.ac_state = state
        self.conn.emit_signal(None, AC_PATH, "org.freedesktop.NetworkManager.Connection.Active",
                              "StateChanged", GLib.Variant("(uu)", (state, reason)))

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
