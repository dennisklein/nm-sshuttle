# SPDX-License-Identifier: MIT
"""The dispatcher (docs/later/multiple-tunnels.md, "Activation path").

NetworkManager spawns nm-sshuttle-activate with the bus name it will wait for,
one per activation. The shim asks this service, which D-Bus activation starts,
for a slot: it picks the lowest free one, writes the name to the slot's
bus-name file and starts nm-sshuttle@N.service, which owns that name and is the
plugin for that one activation.

It keeps no state that matters across its own restarts: the plugin units'
states are the truth. It serves one call at a time and exits when idle.
"""

import logging
import os
import re
import subprocess
import sys
import time
import warnings

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from . import guard  # noqa: E402
from .const import (BUS_NAME, DISPATCHER_NAME, DISPATCHER_PATH, LIBEXEC, SLOTS,  # noqa: E402
                    SYSTEMD_MANAGER_IFACE, SYSTEMD_NAME, SYSTEMD_PATH, SYSTEMD_UNIT_IFACE,
                    V1_GUARD_TABLE, V1_PLUGIN_UNIT, V1_TUNNEL_SPEC, Slot)

warnings.filterwarnings("ignore", category=DeprecationWarning)
log = logging.getLogger("nm-sshuttle")

INTROSPECTION = f"""
<node>
  <interface name="{DISPATCHER_NAME}">
    <method name="Start">
      <arg name="bus_name" type="s" direction="in"/>
      <arg name="slot" type="u" direction="out"/>
    </method>
  </interface>
</node>
"""
# NM's fixed name, or the per-activation one it derives from it (nm-vpn-connection.c)
NAME_RE = re.compile(re.escape(BUS_NAME) + r"(\.Connection_[0-9]+)?")
FREE_STATES = ("inactive", "failed")
RECENT_S = 10.0        # a slot handed out this recently may not show as active yet
IDLE_EXIT_S = 60
ERROR = "org.freedesktop.NetworkManager.sshuttle.Error"


def pick_slot(states, handed, now, recent=RECENT_S):
    """The lowest slot whose plugin unit is inactive or failed and that was not
    handed out in the last `recent` seconds, or None."""
    for n in range(SLOTS):
        if states.get(n) in FREE_STATES and now - handed.get(n, float("-inf")) >= recent:
            return n
    return None


def write_bus_name(slot, name):
    path = Slot(slot).bus_name_file
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(name + "\n")
    os.replace(tmp, path)


class Dispatcher:
    def __init__(self, conn, loop):
        self.conn = conn
        self.loop = loop
        self.handed = {}            # slot -> monotonic time it was handed out
        self.idle_id = 0
        self.node = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION)

    def run(self):
        self.conn.register_object(DISPATCHER_PATH, self.node.interfaces[0],
                                  self.on_method_call, None, None)
        self._arm_idle()
        Gio.bus_own_name_on_connection(self.conn, DISPATCHER_NAME, Gio.BusNameOwnerFlags.NONE,
                                       lambda c, n: log.info("owns %s", n),
                                       lambda c, n: self.loop.quit())

    def _arm_idle(self):
        if self.idle_id:
            GLib.source_remove(self.idle_id)
        self.idle_id = GLib.timeout_add_seconds(IDLE_EXIT_S, self._idle)

    def _idle(self):
        log.info("idle; exiting")
        self.idle_id = 0
        self.loop.quit()
        return False

    def _call(self, dest, path, iface, method, args, rtype):
        return self.conn.call_sync(dest, path, iface, method, args,
                                   GLib.VariantType(rtype) if rtype else None,
                                   Gio.DBusCallFlags.NONE, 5000, None).unpack()

    def unit_state(self, unit):
        """ActiveState of a unit; "inactive" for one systemd has not loaded."""
        try:
            path = self._call(SYSTEMD_NAME, SYSTEMD_PATH, SYSTEMD_MANAGER_IFACE, "GetUnit",
                              GLib.Variant("(s)", (unit,)), "(o)")[0]
        except GLib.Error as e:
            if "NoSuchUnit" in (Gio.DBusError.get_remote_error(e) or ""):
                return "inactive"
            raise
        return self._call(SYSTEMD_NAME, path, "org.freedesktop.DBus.Properties", "Get",
                          GLib.Variant("(ss)", (SYSTEMD_UNIT_IFACE, "ActiveState")),
                          "(v)")[0]

    def caller_uid(self, sender):
        return self._call("org.freedesktop.DBus", "/org/freedesktop/DBus",
                          "org.freedesktop.DBus", "GetConnectionUnixUser",
                          GLib.Variant("(s)", (sender,)), "(u)")[0]

    def start(self, sender, name):
        """Start a plugin instance for name; its slot."""
        if self.caller_uid(sender) != 0:
            raise PermissionError("only root may start the plugin")
        if not NAME_RE.fullmatch(name):
            raise ValueError(f"not an nm-sshuttle bus name: {name!r}")
        states = {n: self.unit_state(Slot(n).plugin_unit) for n in range(SLOTS)}
        slot = pick_slot(states, self.handed, time.monotonic())
        if slot is None:
            raise RuntimeError(f"all {SLOTS} slots are in use")
        write_bus_name(slot, name)
        self._call(SYSTEMD_NAME, SYSTEMD_PATH, SYSTEMD_MANAGER_IFACE, "StartUnit",
                   GLib.Variant("(ss)", (Slot(slot).plugin_unit, "replace")), "(o)")
        self.handed[slot] = time.monotonic()
        log.info("slot %d for %s", slot, name)
        return slot

    def on_method_call(self, conn, sender, path, iface, method, params, inv):
        self._arm_idle()
        if method != "Start":
            inv.return_dbus_error(ERROR + ".UnknownMethod", method)
            return
        name = params.unpack()[0]
        try:
            inv.return_value(GLib.Variant("(u)", (self.start(sender, name),)))
        except (GLib.Error, OSError, PermissionError, ValueError, RuntimeError) as e:
            log.warning("cannot start a plugin for %s: %s", name, e)
            inv.return_dbus_error(ERROR + ".Failed", str(e))


def v1_leftovers():
    """A single-tunnel version left its guard or its spec behind (an upgrade)."""
    return os.path.exists(V1_TUNNEL_SPEC) or guard.guard_present(V1_GUARD_TABLE)


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    from .service import connect_bus
    conn = connect_bus()
    loop = GLib.MainLoop()
    dispatcher = Dispatcher(conn, loop)
    try:
        if v1_leftovers() and dispatcher.unit_state(V1_PLUGIN_UNIT) in FREE_STATES:
            log.info("removing what a single-tunnel version left behind")
            subprocess.run([os.path.join(LIBEXEC, "nm-sshuttle"), "cleanup", "--all"],
                           timeout=30)
    except (GLib.Error, OSError, subprocess.TimeoutExpired) as e:
        log.warning("cleanup of an older version's leftovers failed: %s", e)
    dispatcher.run()
    loop.run()
    return 0
