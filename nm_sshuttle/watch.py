# SPDX-License-Identifier: MIT
"""Watchers for what the supervisor reacts to besides its own activation
(design §4.4): NetworkManager's uplinks, connectivity and primary connection,
and logind's sleep signal and lock hints. The VPN's own active connection and
the tunnel unit are watched in service.py.

Each watcher turns D-Bus signals into plain calls: it reads what changed
asynchronously and reports whole values, so the supervisor never sees D-Bus
types.
"""

import logging

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from .const import (AC_ACTIVATED, LOGIN_MANAGER_IFACE, LOGIN_NAME, LOGIN_PATH,  # noqa: E402
                    LOGIN_SESSION_IFACE, LOGIN_USER_IFACE, NM_AC_IFACE, NM_IP4_IFACE, NM_NAME,
                    NM_PATH)

log = logging.getLogger("nm-sshuttle")

PROPS_IFACE = "org.freedesktop.DBus.Properties"
NM_KEYS = {"ActiveConnections", "Connectivity", "PrimaryConnection", "Version"}
AC_KEYS = {"State", "StateFlags"}
AC_FLAG_EXTERNAL = 0x80         # NM_ACTIVATION_STATE_FLAG_EXTERNAL


def is_uplink(props):
    """An activated connection NetworkManager manages that is not a VPN: the
    plugin's own connection, loopback and external ones (docker0, virbr0) do not
    count."""
    return (props.get("State") == AC_ACTIVATED and not props.get("Vpn")
            and props.get("Type") != "loopback"
            and not props.get("StateFlags", 0) & AC_FLAG_EXTERNAL)


class NetworkWatcher:
    """Reports whether an uplink is activated, NM's Connectivity, and a key for
    the network: the primary connection's UUID with its IPv4 addresses and
    gateway. The key changes on a roam to another network or connection, and
    not on a DHCP renewal or an IPv6 change (the tunnel is IPv4).

    NM's global State is no use for "offline": an activating VPN, the plugin's
    own during a reconnect included, turns it into CONNECTING."""

    def __init__(self, conn, call, on_network, on_version):
        self.conn = conn
        self.call = call            # Service._call
        self.on_network = on_network
        self.on_version = on_version
        self.primary = None
        self.ip4 = None
        self.version = None
        self.generation = 0

    def start(self):
        sub = self.conn.signal_subscribe
        sub(NM_NAME, PROPS_IFACE, "PropertiesChanged", NM_PATH, NM_NAME,
            Gio.DBusSignalFlags.NONE, self._on_nm_props)
        sub(NM_NAME, PROPS_IFACE, "PropertiesChanged", None, NM_AC_IFACE,
            Gio.DBusSignalFlags.NONE, self._on_ac_props)
        sub(NM_NAME, PROPS_IFACE, "PropertiesChanged", None, NM_IP4_IFACE,
            Gio.DBusSignalFlags.NONE, self._on_ip4_props)

    # NetworkManager appeared (again): read everything afresh
    def refresh(self):
        self.generation += 1
        gen = self.generation
        self._get_all(NM_PATH, NM_NAME, lambda props: self._got_nm(gen, props))

    def forget(self):
        self.generation += 1
        self.primary = self.ip4 = self.version = None

    def _on_nm_props(self, conn, sender, path, iface, name, params):
        _iface, changed, invalidated = params.unpack()
        if NM_KEYS & (set(changed) | set(invalidated)):
            self.refresh()

    def _on_ac_props(self, conn, sender, path, iface, name, params):
        _iface, changed, invalidated = params.unpack()
        keys = set(changed) | set(invalidated)
        if AC_KEYS & keys or (path == self.primary and "Ip4Config" in keys):
            self.refresh()

    def _on_ip4_props(self, conn, sender, path, iface, name, params):
        _iface, changed, invalidated = params.unpack()
        if path == self.ip4 and {"AddressData", "Gateway"} & (set(changed) | set(invalidated)):
            self.refresh()

    def _get_all(self, path, iface, done):
        self.call(NM_NAME, path, PROPS_IFACE, "GetAll", GLib.Variant("(s)", (iface,)),
                  "(a{sv})", lambda r, e: done(r[0] if r else None))

    def _got_nm(self, gen, props):
        if gen != self.generation or props is None:
            return
        version = props.get("Version")
        if version and version != self.version:
            self.version = version
            self.on_version(version)
        paths = list(props.get("ActiveConnections", []))
        acs = {}
        nm = (props.get("Connectivity"), props.get("PrimaryConnection"))
        if not paths:
            self._got_acs(gen, nm, acs)
            return

        def got(path, ac):
            if gen != self.generation:
                return
            acs[path] = ac or {}            # gone in between: a newer signal follows
            if len(acs) == len(paths):
                self._got_acs(gen, nm, acs)
        for path in paths:
            self._get_all(path, NM_AC_IFACE, lambda ac, path=path: got(path, ac))

    def _got_acs(self, gen, nm, acs):
        conn, primary = nm
        uplink = any(is_uplink(ac) for ac in acs.values())
        ac = acs.get(primary) if primary and primary != "/" else None
        if not ac:
            self.primary = self.ip4 = None
            self.on_network(uplink, conn, None)
            return
        self.primary = primary
        uuid = ac.get("Uuid", "")
        ip4 = ac.get("Ip4Config")
        if not ip4 or ip4 == "/":
            self.ip4 = None
            self.on_network(uplink, conn, (uuid, (), ""))
            return
        self.ip4 = ip4
        self._get_all(ip4, NM_IP4_IFACE, lambda p: self._got_ip4(gen, uplink, conn, uuid, p))

    def _got_ip4(self, gen, uplink, conn, uuid, props):
        if gen != self.generation:
            return
        props = props or {}
        addrs = tuple(sorted(f"{a.get('address')}/{a.get('prefix')}"
                             for a in props.get("AddressData", [])))
        self.on_network(uplink, conn, (uuid, addrs, props.get("Gateway", "")))


class LogindWatcher:
    """PrepareForSleep, and any session's LockedHint changing."""

    def __init__(self, conn, on_sleep, on_lock_changed):
        self.conn = conn
        self.on_sleep = on_sleep
        self.on_lock_changed = on_lock_changed

    def start(self):
        self.conn.signal_subscribe(LOGIN_NAME, LOGIN_MANAGER_IFACE, "PrepareForSleep", LOGIN_PATH,
                                   None, Gio.DBusSignalFlags.NONE, self._on_sleep)
        self.conn.signal_subscribe(LOGIN_NAME, PROPS_IFACE, "PropertiesChanged", None,
                                   LOGIN_SESSION_IFACE, Gio.DBusSignalFlags.NONE, self._on_props)

    def _on_sleep(self, conn, sender, path, iface, name, params):
        self.on_sleep(bool(params.unpack()[0]))

    def _on_props(self, conn, sender, path, iface, name, params):
        _iface, changed, invalidated = params.unpack()
        if "LockedHint" in changed or "LockedHint" in invalidated:
            self.on_lock_changed()


def session_locked(call, uid, done):
    """done(True) when any of the user's sessions has LockedHint set, False when
    none has (or the user has no session), None when logind cannot tell.

    The agent's prompt shows in the user's graphical session; a second, unlocked
    session (ssh) does not make the prompt visible, so any locked one counts."""
    def got_user(r, err):
        if err:
            done(False if "NoSuchUser" in err or "No user" in err else None)
            return
        call(LOGIN_NAME, r[0], PROPS_IFACE, "Get",
             GLib.Variant("(ss)", (LOGIN_USER_IFACE, "Sessions")), "(v)", got_sessions)

    def got_sessions(r, err):
        if err:
            done(None)
            return
        paths = [path for _id, path in r[0]]
        if not paths:
            done(False)
            return
        answers = []

        def got_hint(r2, err2):
            answers.append(None if err2 else bool(r2[0]))
            if len(answers) < len(paths):
                return
            if any(a is True for a in answers):
                done(True)
            else:
                done(None if all(a is None for a in answers) else False)
        for path in paths:
            call(LOGIN_NAME, path, PROPS_IFACE, "Get",
                 GLib.Variant("(ss)", (LOGIN_SESSION_IFACE, "LockedHint")), "(v)", got_hint)

    call(LOGIN_NAME, LOGIN_PATH, LOGIN_MANAGER_IFACE, "GetUser", GLib.Variant("(u)", (uid,)),
         "(o)", got_user)
