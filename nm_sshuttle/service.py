# SPDX-License-Identifier: MIT
"""The VPN service plugin process: org.freedesktop.NetworkManager.VPN.Plugin
spoken directly through Gio (design §4.1), plus the real Effects for the
Supervisor: NetworkManager and systemd over D-Bus, ip and nft for the link
and the guard, and GLib timers.

Nothing here blocks the main loop for long: systemd jobs, the first hop and
the cleanup run asynchronously. ip and nft calls are single short runs.
"""

import json
import logging
import os
import signal
import socket
import struct
import sys
import time
import warnings

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from . import guard, tunnel  # noqa: E402
from .const import (BUS_NAME, LIBEXEC, NM_AC_IFACE, NM_DEVICE_IFACE, NM_NAME,  # noqa: E402
                    NM_PATH, PLUGIN_IFACE, PLUGIN_PATH, STATE_DIR, SYSTEMD_MANAGER_IFACE,
                    SYSTEMD_NAME, SYSTEMD_PATH, SYSTEMD_UNIT_IFACE, TUNNEL_SPEC, TUNNEL_UNIT)
from .supervisor import Supervisor  # noqa: E402

# GLib.unix_signal_add and DBusConnection.register_object are deprecated in
# recent PyGObject but still work everywhere; keep the journal readable.
warnings.filterwarnings("ignore", category=DeprecationWarning)

log = logging.getLogger("nm-sshuttle")

PROPS_IFACE = "org.freedesktop.DBus.Properties"
VPN_ERROR_LAUNCH_FAILED = "org.freedesktop.NetworkManager.VPN.Error.LaunchFailed"
FIRST_HOP_TIMEOUT_S = 25
LINK_CHECK_MS = 500

INTROSPECTION = """
<node>
  <interface name="org.freedesktop.NetworkManager.VPN.Plugin">
    <method name="Connect">
      <arg name="connection" type="a{sa{sv}}" direction="in"/>
    </method>
    <method name="ConnectInteractive">
      <arg name="connection" type="a{sa{sv}}" direction="in"/>
      <arg name="details" type="a{sv}" direction="in"/>
    </method>
    <method name="NeedSecrets">
      <arg name="settings" type="a{sa{sv}}" direction="in"/>
      <arg name="setting_name" type="s" direction="out"/>
    </method>
    <method name="Disconnect"/>
    <method name="SetConfig">
      <arg name="config" type="a{sv}" direction="in"/>
    </method>
    <method name="SetIp4Config">
      <arg name="config" type="a{sv}" direction="in"/>
    </method>
    <method name="SetIp6Config">
      <arg name="config" type="a{sv}" direction="in"/>
    </method>
    <method name="SetFailure">
      <arg name="reason" type="s" direction="in"/>
    </method>
    <method name="NewSecrets">
      <arg name="connection" type="a{sa{sv}}" direction="in"/>
    </method>
    <property name="State" type="u" access="read"/>
    <signal name="StateChanged"><arg name="state" type="u"/></signal>
    <signal name="SecretsRequired">
      <arg name="message" type="s"/>
      <arg name="secrets" type="as"/>
    </signal>
    <signal name="Config"><arg name="config" type="a{sv}"/></signal>
    <signal name="Ip4Config"><arg name="ip4config" type="a{sv}"/></signal>
    <signal name="Ip6Config"><arg name="ip6config" type="a{sv}"/></signal>
    <signal name="LoginBanner"><arg name="banner" type="s"/></signal>
    <signal name="Failure"><arg name="reason" type="u"/></signal>
  </interface>
</node>
"""


def ip4_u32(addr):
    """NM wants IPv4 addresses as uint32 in network byte order."""
    return struct.unpack("=I", socket.inet_aton(addr))[0]


def config_variant(cfg):
    types = {"gateway": "u", "tundev": "s", "has-ip4": "b", "has-ip6": "b", "can-persist": "b"}
    out = {}
    for key, value in cfg.items():
        out[key] = GLib.Variant(types[key], ip4_u32(value) if key == "gateway" else value)
    return out


def ip4_variant(cfg):
    out = {}
    for key, value in cfg.items():
        if key == "address":
            out[key] = GLib.Variant("u", ip4_u32(value))
        elif key == "prefix":
            out[key] = GLib.Variant("u", value)
        elif key == "never-default":
            out[key] = GLib.Variant("b", value)
        elif key in ("dns", "nbns"):
            out[key] = GLib.Variant("au", [ip4_u32(a) for a in value])
        elif key == "domains":
            out[key] = GLib.Variant("as", value)
        else:
            raise KeyError(key)
    return out


def unit_object_path(unit):
    """systemd's bus label escaping (bus_label_escape)."""
    out = []
    for i, ch in enumerate(unit):
        if ch.isascii() and (ch.isalpha() or (ch.isdigit() and i > 0)):
            out.append(ch)
        else:
            out.append(f"_{ord(ch):02x}")
    return f"{SYSTEMD_PATH}/unit/{''.join(out)}"


class Service:
    """Owns the bus name, dispatches VPN.Plugin calls and implements Effects."""

    def __init__(self, conn, loop):
        self.conn = conn
        self.loop = loop
        self.sup = Supervisor(self)
        self.jobs = {}            # systemd job path -> callback(result)
        self.finished_jobs = {}   # JobRemoved that arrived before StartUnit's reply
        self.unit_path = unit_object_path(TUNNEL_UNIT)
        self.node = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION)

    # ------------------------------------------------------------- set-up
    def run(self, bus_name=BUS_NAME):
        self.conn.register_object(PLUGIN_PATH, self.node.interfaces[0],
                                  self.on_method_call, self.on_get_property, None)
        self.conn.signal_subscribe(NM_NAME, NM_AC_IFACE, "StateChanged", None, None,
                                   Gio.DBusSignalFlags.NONE, self.on_ac_state)
        self.conn.signal_subscribe(SYSTEMD_NAME, SYSTEMD_MANAGER_IFACE, "JobRemoved",
                                   SYSTEMD_PATH, None, Gio.DBusSignalFlags.NONE,
                                   self.on_job_removed)
        self.conn.signal_subscribe(SYSTEMD_NAME, PROPS_IFACE, "PropertiesChanged",
                                   self.unit_path, None, Gio.DBusSignalFlags.NONE,
                                   self.on_unit_changed)
        # systemd sends unit and job signals only while someone is subscribed.
        self._call(SYSTEMD_NAME, SYSTEMD_PATH, SYSTEMD_MANAGER_IFACE, "Subscribe", None, None,
                   lambda r, e: e and log.warning("systemd Subscribe failed: %s", e))
        Gio.bus_watch_name_on_connection(self.conn, NM_NAME, Gio.BusNameWatcherFlags.NONE,
                                         self.on_nm_appeared, self.on_nm_vanished)
        for sig in (signal.SIGTERM, signal.SIGINT):
            GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, self.on_term)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR1, self.on_usr1)
        GLib.timeout_add(LINK_CHECK_MS, self.on_link_check)
        self.sup.start()
        self.name_id = Gio.bus_own_name_on_connection(
            self.conn, bus_name, Gio.BusNameOwnerFlags.NONE,
            self.on_name_acquired, self.on_name_lost)

    def on_name_acquired(self, conn, name):
        log.info("owns %s", name)

    def on_name_lost(self, conn, name):
        self.sup.on_name_lost()

    def on_nm_appeared(self, conn, name, owner):
        log.info("NetworkManager is %s", owner)
        self.sup.on_nm_owner(owner)

    def on_nm_vanished(self, conn, name):
        self.sup.on_nm_owner(None)

    def on_term(self):
        self.sup.on_term()
        return True

    def on_usr1(self):
        self.sup.simulate_drop()
        return True

    def on_link_check(self):
        self.sup.check_link()
        return True

    # ---------------------------------------------------------- VPN.Plugin
    def on_get_property(self, conn, sender, path, iface, prop):
        if prop == "State":
            return GLib.Variant("u", self.sup.state)
        return None

    def on_method_call(self, conn, sender, path, iface, method, params, inv):
        try:
            log.info("D-Bus call %s from %s", method, sender)
            self.sup.touch()
            if method in ("Connect", "ConnectInteractive"):
                settings = params.unpack()[0]
                inv.return_value(None)
                GLib.idle_add(self.sup.connect, settings, sender)
            elif method == "NeedSecrets":
                inv.return_value(GLib.Variant("(s)", ("",)))   # the agent holds the keys
            elif method == "Disconnect":
                inv.return_value(None)
                GLib.idle_add(self.sup.disconnect)
            else:
                inv.return_value(None)
        except Exception as e:  # noqa: BLE001
            log.error("D-Bus handler error: %r", e)
            try:
                inv.return_dbus_error(VPN_ERROR_LAUNCH_FAILED, str(e))
            except Exception:  # noqa: BLE001 - already replied
                pass

    def _emit(self, name, sig, args):
        self.conn.emit_signal(None, PLUGIN_PATH, PLUGIN_IFACE, name, GLib.Variant(sig, args))

    def emit_state(self, state):
        self._emit("StateChanged", "(u)", (state,))

    def emit_config(self, cfg):
        log.info("Config: %s", cfg)
        self._emit("Config", "(a{sv})", (config_variant(cfg),))

    def emit_ip4_config(self, cfg):
        log.info("Ip4Config: %s", cfg)
        self._emit("Ip4Config", "(a{sv})", (ip4_variant(cfg),))

    def emit_failure(self, reason):
        self._emit("Failure", "(u)", (reason,))

    # ------------------------------------------------------- D-Bus helpers
    def _call(self, dest, path, iface, method, args, rtype, done, timeout_ms=5000):
        """Async call; done(result or None, error message or None)."""
        def finished(conn, res):
            try:
                r = conn.call_finish(res)
            except GLib.Error as e:
                done(None, e.message)
                return
            done(r.unpack() if r is not None else (), None)
        self.conn.call(dest, path, iface, method, args,
                       GLib.VariantType(rtype) if rtype else None,
                       Gio.DBusCallFlags.NONE, timeout_ms, None, finished)

    def _get_property(self, dest, path, iface, prop, done):
        self._call(dest, path, PROPS_IFACE, "Get", GLib.Variant("(ss)", (iface, prop)), "(v)",
                   lambda r, e: done(r[0] if r else None, e))

    # ------------------------------------------------------ NetworkManager
    def on_ac_state(self, conn, sender, path, iface, signal_name, params):
        state, reason = params.unpack()
        self.sup.on_ac_state(path, state, reason)

    def find_active_connection(self, uuid, done):
        def got_list(paths, err):
            if err or not paths:
                if err:
                    log.warning("cannot list active connections: %s", err)
                done(None, None)
                return
            check(list(paths))

        def check(paths):
            if not paths:
                done(None, None)
                return
            path = paths.pop(0)
            self._call(NM_NAME, path, PROPS_IFACE, "GetAll", GLib.Variant("(s)", (NM_AC_IFACE,)),
                       "(a{sv})", lambda r, e: got_props(path, paths, r, e))

        def got_props(path, rest, r, err):
            props = r[0] if r else {}
            # Skip a previous activation that is still deactivating.
            if props.get("Uuid") == uuid and props.get("State", 0) < 3:
                done(path, props.get("State"))
            else:
                check(rest)

        self._get_property(NM_NAME, NM_PATH, NM_NAME, "ActiveConnections", got_list)

    def find_device(self, iface, done):
        self._call(NM_NAME, NM_PATH, NM_NAME, "GetDeviceByIpIface", GLib.Variant("(s)", (iface,)),
                   "(o)", lambda r, e: done(r[0] if r else None), timeout_ms=1000)

    def device_state(self, path, done):
        self._get_property(NM_NAME, path, NM_DEVICE_IFACE, "State", lambda v, e: done(v))

    # ------------------------------------------------------------- systemd
    def on_job_removed(self, conn, sender, path, iface, signal_name, params):
        _id, job, unit, result = params.unpack()
        cb = self.jobs.pop(job, None)
        if cb:
            cb(result)
        elif unit == TUNNEL_UNIT:
            self.finished_jobs[job] = result

    def on_unit_changed(self, conn, sender, path, iface, signal_name, params):
        changed_iface, changed, _ = params.unpack()
        if changed_iface == SYSTEMD_UNIT_IFACE and "ActiveState" in changed:
            state = changed["ActiveState"]
            log.info("%s is %s", TUNNEL_UNIT, state)
            if state in ("inactive", "failed"):
                self.sup.on_tunnel_inactive()

    def _job(self, method, done):
        """Run StartUnit or StopUnit; done(result string or None, error)."""
        def queued(r, err):
            if err:
                done(None, err)
                return
            job = r[0]
            if job in self.finished_jobs:
                done(self.finished_jobs.pop(job), None)
            else:
                self.jobs[job] = lambda result: done(result, None)
        self._call(SYSTEMD_NAME, SYSTEMD_PATH, SYSTEMD_MANAGER_IFACE, method,
                   GLib.Variant("(ss)", (TUNNEL_UNIT, "replace")), "(o)", queued)

    def start_tunnel(self, done):
        def started(result, err):
            if err:
                done(False, f"systemd: {err}")      # an error is a failure, never "inactive"
            elif result == "done":
                done(True, "")
            else:
                self._failure_detail(lambda text: done(False, f"start job {result}\n{text}"))

        def reset(_r, _err):
            self._job("StartUnit", started)
        self._call(SYSTEMD_NAME, SYSTEMD_PATH, SYSTEMD_MANAGER_IFACE, "ResetFailedUnit",
                   GLib.Variant("(s)", (TUNNEL_UNIT,)), None, reset)

    def stop_tunnel(self, done):
        def stopped(result, err):
            if err and "not loaded" not in err:
                log.warning("stopping %s: %s", TUNNEL_UNIT, err)
            done()
        self._job("StopUnit", stopped)

    def _failure_detail(self, done):
        """The last lines of the tunnel unit's journal, for the failure markers."""
        def got_id(value, err):
            if not value:
                done(err or "")
                return
            inv = bytes(value).hex()
            self._spawn(["journalctl", f"_SYSTEMD_INVOCATION_ID={inv}", "-o", "cat",
                         "--no-pager", "-n", "40"], 10, lambda ok, out, err2: done(out[-2000:]))
        self._get_property(SYSTEMD_NAME, self.unit_path, SYSTEMD_UNIT_IFACE, "InvocationID",
                           got_id)

    # --------------------------------------------------------- subprocesses
    def _spawn(self, argv, timeout_s, done):
        """Run argv without blocking; done(ok, stdout, stderr)."""
        try:
            proc = Gio.Subprocess.new(argv, Gio.SubprocessFlags.STDOUT_PIPE
                                      | Gio.SubprocessFlags.STDERR_PIPE)
        except GLib.Error as e:
            done(False, "", e.message)
            return
        timer = {}

        def expire():
            timer.clear()
            log.warning("%s did not finish within %d s; killing it", argv[0], timeout_s)
            proc.force_exit()
            return False
        timer["id"] = GLib.timeout_add_seconds(timeout_s, expire)

        def finished(p, res):
            if timer:
                GLib.source_remove(timer.pop("id"))
            try:
                _, out, err = p.communicate_utf8_finish(res)
            except GLib.Error as e:
                done(False, "", e.message)
                return
            done(p.get_successful(), out or "", err or "")
        proc.communicate_utf8_async(None, None, finished)

    def resolve_first_hop(self, user, remote, done):
        def finished(ok, out, err):
            if ok and out.strip():
                done(out.strip(), None)
            else:
                done(None, err.strip() or "no address")
        self._spawn([os.path.join(LIBEXEC, "nm-sshuttle"), "first-hop", user, remote],
                    FIRST_HOP_TIMEOUT_S, finished)

    def run_cleanup(self, done):
        def finished(ok, out, err):
            for line in (out + err).splitlines():
                log.info("cleanup: %s", line)
            if not ok:
                log.warning("startup cleanup failed")
            done()
        self._spawn([os.path.join(LIBEXEC, "nm-sshuttle"), "cleanup"], 60, finished)

    # ------------------------------------------------------- link and guard
    def link_ifindex(self):
        return guard.link_ifindex()

    def create_link(self):
        return guard.create_link()

    def remove_link(self):
        guard.remove_link()

    def install_guard(self, networks, exclude):
        guard.install_guard(networks, exclude)

    def remove_guard(self):
        guard.remove_guard()

    def sweep(self):
        tunnel.sweep()

    def write_tunnel_spec(self, spec):
        os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
        tmp = TUNNEL_SPEC + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(spec, f)
        os.replace(tmp, TUNNEL_SPEC)

    # ----------------------------------------------------------------- loop
    def now(self):
        return time.monotonic()

    def call_later(self, seconds, fn):
        def fire():
            fn()
            return False
        return GLib.timeout_add(max(0, int(seconds * 1000)), fire)

    def cancel(self, handle):
        GLib.source_remove(handle)

    def flush(self):
        try:
            self.conn.flush_sync(None)
        except GLib.Error as e:
            log.warning("D-Bus flush failed: %s", e.message)

    def quit(self):
        # Release the name before the loop ends, so that NM's next call starts a
        # new instance instead of going to a process that is leaving.
        if getattr(self, "name_id", 0):
            Gio.bus_unown_name(self.name_id)
            self.name_id = 0
            self.flush()
        self.loop.quit()


def connect_bus():
    """The system bus, or NM_SSHUTTLE_BUS_ADDRESS (tests)."""
    address = os.environ.get("NM_SSHUTTLE_BUS_ADDRESS")
    if not address:
        return Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
    return Gio.DBusConnection.new_for_address_sync(
        address, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
        | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    bus_name = BUS_NAME
    if "--bus-name" in argv:
        bus_name = argv[argv.index("--bus-name") + 1]
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    log.info("starting (pid %d)", os.getpid())
    loop = GLib.MainLoop()
    service = Service(connect_bus(), loop)
    service.run(bus_name)
    loop.run()
    log.info("exited")
    return 0
