# SPDX-License-Identifier: MIT
"""Smoke test of the dispatcher over a private D-Bus daemon: a fake systemd
runs on its own thread (the dispatcher's calls are synchronous), and a client
asks for slots the way nm-sshuttle-activate does."""

import os
import shutil
import threading

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from test_service_dbus import bus, connect, iterate_until  # noqa: E402,F401
from nm_sshuttle import const, dispatcher  # noqa: E402
from nm_sshuttle.const import DISPATCHER_NAME, DISPATCHER_PATH, SLOTS, Slot  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("dbus-daemon"), reason="needs dbus-daemon")

SYSTEMD_XML = """
<node>
  <interface name="org.freedesktop.systemd1.Manager">
    <method name="GetUnit">
      <arg type="s" direction="in"/><arg type="o" direction="out"/>
    </method>
    <method name="StartUnit">
      <arg type="s" direction="in"/><arg type="s" direction="in"/>
      <arg type="o" direction="out"/>
    </method>
  </interface>
  <interface name="org.freedesktop.systemd1.Unit">
    <property name="ActiveState" type="s" access="read"/>
  </interface>
</node>"""


def unit_path(n):
    return f"/org/freedesktop/systemd1/unit/nm_2dsshuttle_40{n}_2eservice"


class FakeSystemd(threading.Thread):
    """Units it has loaded have a state; the others are NoSuchUnit."""

    def __init__(self, address):
        super().__init__(daemon=True)
        self.address = address
        self.states = {}               # slot -> ActiveState
        self.started = []
        self.ready = threading.Event()

    def run(self):
        ctx = GLib.MainContext.new()
        ctx.push_thread_default()
        self.loop = GLib.MainLoop(ctx)
        conn = connect(self.address)
        node = Gio.DBusNodeInfo.new_for_xml(SYSTEMD_XML)
        conn.register_object("/org/freedesktop/systemd1", node.interfaces[0], self.on_call,
                             None, None)
        for n in range(SLOTS):
            conn.register_object(unit_path(n), node.interfaces[1], None,
                                 lambda c, s, path, i, p: GLib.Variant(
                                     "s", self.states.get(int(path[-11]), "inactive")), None)
        conn.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                       "RequestName", GLib.Variant("(su)", ("org.freedesktop.systemd1", 4)),
                       None, Gio.DBusCallFlags.NONE, 1000, None)
        self.ready.set()
        self.loop.run()

    def on_call(self, conn, sender, path, iface, method, params, inv):
        unit = params.unpack()[0]
        n = int(unit.split("@")[1].split(".")[0])
        if method == "GetUnit":
            if n not in self.states:
                inv.return_dbus_error("org.freedesktop.systemd1.NoSuchUnit",
                                      f"Unit {unit} not loaded.")
            else:
                inv.return_value(GLib.Variant("(o)", (unit_path(n),)))
        else:
            self.started.append(unit)
            self.states[n] = "activating"
            inv.return_value(GLib.Variant("(o)", ("/org/freedesktop/systemd1/job/1",)))


@pytest.fixture
def world(bus, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(const, "STATE_DIR", str(tmp_path))
    sd = FakeSystemd(bus)
    sd.start()
    sd.ready.wait(5)
    loop = GLib.MainLoop()
    d = dispatcher.Dispatcher(connect(bus), loop)
    d.uid = 0
    monkeypatch.setattr(d, "caller_uid", lambda sender: d.uid)
    owned = []
    d.conn.signal_subscribe("org.freedesktop.DBus", "org.freedesktop.DBus", "NameAcquired",
                            "/org/freedesktop/DBus", None, Gio.DBusSignalFlags.NONE,
                            lambda *a: owned.append(a[5].unpack()[0]))
    d.run()
    iterate_until(lambda: DISPATCHER_NAME in owned)
    client = connect(bus)
    yield sd, d, client
    sd.loop.quit()


def start(client, name):
    out = []

    def done(conn, res):
        try:
            out.append(conn.call_finish(res).unpack()[0])
        except GLib.Error as e:
            name = Gio.DBusError.get_remote_error(e)
            out.append(name + ": " + e.message.split(name + ": ", 1)[-1])
    client.call(DISPATCHER_NAME, DISPATCHER_PATH, DISPATCHER_NAME, "Start",
                GLib.Variant("(s)", (name,)), None, Gio.DBusCallFlags.NONE, 5000, None, done)
    iterate_until(lambda: out)
    return out[0]


def test_two_activations_get_two_slots(world, tmp_path):
    sd, d, client = world
    first = "org.freedesktop.NetworkManager.sshuttle.Connection_1"
    second = "org.freedesktop.NetworkManager.sshuttle.Connection_2"
    assert start(client, first) == 0
    assert start(client, second) == 1
    assert sd.started == ["nm-sshuttle@0.service", "nm-sshuttle@1.service"]
    assert (tmp_path / "0" / "bus-name").read_text() == first + "\n"
    assert (tmp_path / "1" / "bus-name").read_text() == second + "\n"


def test_a_free_slot_is_reused_once_its_unit_is_inactive(world):
    sd, d, client = world
    sd.states = {0: "active", 1: "failed"}
    assert start(client, "org.freedesktop.NetworkManager.sshuttle") == 1
    d.handed.clear()
    sd.states[1] = "inactive"
    assert start(client, "org.freedesktop.NetworkManager.sshuttle") == 1


def test_errors_come_back_as_dbus_errors(world):
    sd, d, client = world
    assert start(client, "org.example.Other").startswith(
        dispatcher.ERROR + ".Failed: not an nm-sshuttle bus name")
    d.uid = 1000
    assert start(client, "org.freedesktop.NetworkManager.sshuttle").endswith(
        "only root may start the plugin")
    d.uid = 0
    sd.states = {n: "active" for n in range(SLOTS)}
    assert start(client, "org.freedesktop.NetworkManager.sshuttle").endswith(
        f"all {SLOTS} slots are in use")
    assert sd.started == []


def test_caller_uid_asks_the_bus(world):
    sd, d, client = world
    assert dispatcher.Dispatcher.caller_uid(d, client.get_unique_name()) == os.getuid()


def test_unit_state_of_an_unloaded_unit_is_inactive(world):
    sd, d, client = world
    assert d.unit_state(Slot(3).plugin_unit) == "inactive"
    sd.states[3] = "active"
    assert d.unit_state(Slot(3).plugin_unit) == "active"
