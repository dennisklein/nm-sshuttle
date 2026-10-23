# SPDX-License-Identifier: MIT
"""Names, paths and NetworkManager constants shared by the plugin and the CLI."""

import os
from dataclasses import dataclass

BUS_NAME = "org.freedesktop.NetworkManager.sshuttle"
SERVICE_TYPE = BUS_NAME
PLUGIN_PATH = "/org/freedesktop/NetworkManager/VPN/Plugin"
PLUGIN_IFACE = "org.freedesktop.NetworkManager.VPN.Plugin"

NM_NAME = "org.freedesktop.NetworkManager"
NM_PATH = "/org/freedesktop/NetworkManager"
NM_AC_IFACE = "org.freedesktop.NetworkManager.Connection.Active"
NM_DEVICE_IFACE = "org.freedesktop.NetworkManager.Device"
NM_IP4_IFACE = "org.freedesktop.NetworkManager.IP4Config"
NM_IP6_IFACE = "org.freedesktop.NetworkManager.IP6Config"
NM_DNS_PATH = "/org/freedesktop/NetworkManager/DnsManager"
NM_DNS_IFACE = "org.freedesktop.NetworkManager.DnsManager"

SYSTEMD_NAME = "org.freedesktop.systemd1"
SYSTEMD_PATH = "/org/freedesktop/systemd1"
SYSTEMD_MANAGER_IFACE = "org.freedesktop.systemd1.Manager"
SYSTEMD_UNIT_IFACE = "org.freedesktop.systemd1.Unit"

LOGIN_NAME = "org.freedesktop.login1"
LOGIN_PATH = "/org/freedesktop/login1"
LOGIN_MANAGER_IFACE = "org.freedesktop.login1.Manager"
LOGIN_USER_IFACE = "org.freedesktop.login1.User"
LOGIN_SESSION_IFACE = "org.freedesktop.login1.Session"

# The dispatcher gives each activation a slot (docs/later/multiple-tunnels.md).
DISPATCHER_NAME = BUS_NAME + ".Dispatcher"
DISPATCHER_PATH = "/org/freedesktop/NetworkManager/sshuttle"
DISPATCHER_UNIT = "nm-sshuttle-dispatcher.service"
SLOTS = 8

LINK_ADDRESS = "192.0.0.8"      # RFC 7600 IPv4 dummy address (design §4.1)
NBNS_SENTINEL = "192.0.0.10"    # the reconnect sentinel (design §4.4)
# method=tproxy (docs/later/tproxy.md): the mark sshuttle sets ("NS" in the high
# half, nobody else's), the routing table that sends marked packets to lo, and
# the rule's priority, ahead of Tailscale's and wg-quick's rules.
TPROXY_MARK = 0x4e530000
TPROXY_TABLE = 0x4e53
TPROXY_PRIORITY = 100
# sshuttle's redirect ports (design §4.8). Below net.ipv4.ip_unprivileged_port_start
# only root may bind them, and IANA has not assigned 225 to 241. Each slot tries
# two of its own first, then the others: a port in TIME_WAIT cannot be bound for
# 60 s (lab T8), and quick restarts can leave both of a slot's own in it.
LISTEN_PORTS = range(225, 241)
LISTEN_PORTS_PER_SLOT = 2

# Overridable for development and tests; packages use the defaults.
LIBEXEC = os.environ.get("NM_SSHUTTLE_LIBEXEC", "/usr/libexec/nm-sshuttle")
STATE_DIR = os.environ.get("NM_SSHUTTLE_STATE_DIR", "/run/nm-sshuttle")
# World-readable, unlike STATE_DIR: ssh reads it as the user (design §4.3).
SSH_CONFIG = os.environ.get("NM_SSHUTTLE_SSH_CONFIG", "/run/nm-sshuttle-ssh/ssh_config")
# NM versions on which the nbns reconnect never completed (design §4.4); global,
# instances append to it
INVISIBLE_MARK = os.path.join(STATE_DIR, "invisible-reconnect")
CLAIMS_LOCK = os.path.join(STATE_DIR, "claims.lock")

# What a single-tunnel version created; cleanup --all removes it after an upgrade.
V1_PLUGIN_UNIT = "nm-sshuttle.service"
V1_TUNNEL_UNIT = "nm-sshuttle-tunnel.service"
V1_GUARD_TABLE = "nm-sshuttle-guard"
V1_TUNNEL_SPEC = os.path.join(STATE_DIR, "tunnel.json")


@dataclass(frozen=True)
class Slot:
    """Everything one activation creates is named after its slot, 0 to 7."""
    n: int

    @property
    def plugin_unit(self):
        return f"nm-sshuttle@{self.n}.service"

    @property
    def tunnel_unit(self):
        return f"nm-sshuttle-tunnel@{self.n}.service"

    @property
    def link(self):
        return f"nmss{self.n}"

    @property
    def guard_table(self):
        return f"nm-sshuttle-guard-{self.n}"

    @property
    def dir(self):
        return os.path.join(STATE_DIR, str(self.n))

    @property
    def spec(self):
        return os.path.join(self.dir, "tunnel.json")

    @property
    def ssh_config(self):
        return os.path.join(os.path.dirname(SSH_CONFIG), f"ssh_config-{self.n}")

    @property
    def bus_name_file(self):
        return os.path.join(self.dir, "bus-name")

    @property
    def claim(self):
        return os.path.join(self.dir, "claim.json")

    @property
    def tproxy_mark(self):
        return TPROXY_MARK + self.n

    @property
    def listen_ports(self):
        """The redirect ports in the order the slot tries them: its own two,
        then the others from the top down, whose slots are used least."""
        first = LISTEN_PORTS[0] + LISTEN_PORTS_PER_SLOT * self.n
        own = list(range(first, first + LISTEN_PORTS_PER_SLOT))
        return own + [p for p in reversed(LISTEN_PORTS) if p not in own]


def slot_number(text):
    """A slot from the command line or a unit's instance name."""
    n = int(text)
    if not 0 <= n < SLOTS:
        raise ValueError(f"slot {n} is not 0 to {SLOTS - 1}")
    return n

# NMVpnServiceState and NMVpnPluginFailure (nm-vpn-dbus-interface.h)
ST_INIT, ST_STARTING, ST_STARTED, ST_STOPPING, ST_STOPPED = 1, 3, 4, 5, 6
STATE_NAMES = {ST_INIT: "INIT", ST_STARTING: "STARTING", ST_STARTED: "STARTED",
               ST_STOPPING: "STOPPING", ST_STOPPED: "STOPPED"}
FAIL_LOGIN, FAIL_CONNECT, FAIL_BAD_IP = 0, 1, 2

# NMActiveConnectionState
AC_UNKNOWN, AC_ACTIVATING, AC_ACTIVATED, AC_DEACTIVATING, AC_DEACTIVATED = 0, 1, 2, 3, 4
AC_NAMES = {AC_UNKNOWN: "unknown", AC_ACTIVATING: "activating", AC_ACTIVATED: "activated",
            AC_DEACTIVATING: "deactivating", AC_DEACTIVATED: "deactivated"}

# NMDeviceState
DEVICE_UNMANAGED = 10

# NMConnectivityState
CONN_UNKNOWN, CONN_NONE, CONN_PORTAL, CONN_LIMITED, CONN_FULL = 0, 1, 2, 3, 4
