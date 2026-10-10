# SPDX-License-Identifier: MIT
"""Names, paths and NetworkManager constants shared by the plugin and the CLI."""

import os

BUS_NAME = "org.freedesktop.NetworkManager.sshuttle"
SERVICE_TYPE = BUS_NAME
PLUGIN_PATH = "/org/freedesktop/NetworkManager/VPN/Plugin"
PLUGIN_IFACE = "org.freedesktop.NetworkManager.VPN.Plugin"

NM_NAME = "org.freedesktop.NetworkManager"
NM_PATH = "/org/freedesktop/NetworkManager"
NM_AC_IFACE = "org.freedesktop.NetworkManager.Connection.Active"
NM_DEVICE_IFACE = "org.freedesktop.NetworkManager.Device"
NM_IP4_IFACE = "org.freedesktop.NetworkManager.IP4Config"

SYSTEMD_NAME = "org.freedesktop.systemd1"
SYSTEMD_PATH = "/org/freedesktop/systemd1"
SYSTEMD_MANAGER_IFACE = "org.freedesktop.systemd1.Manager"
SYSTEMD_UNIT_IFACE = "org.freedesktop.systemd1.Unit"

LOGIN_NAME = "org.freedesktop.login1"
LOGIN_PATH = "/org/freedesktop/login1"
LOGIN_MANAGER_IFACE = "org.freedesktop.login1.Manager"
LOGIN_USER_IFACE = "org.freedesktop.login1.User"
LOGIN_SESSION_IFACE = "org.freedesktop.login1.Session"

PLUGIN_UNIT = "nm-sshuttle.service"
TUNNEL_UNIT = "nm-sshuttle-tunnel.service"

LINK = "nmss0"
LINK_ADDRESS = "192.0.0.8"      # RFC 7600 IPv4 dummy address (design §4.1)
NBNS_SENTINEL = "192.0.0.10"    # the reconnect sentinel (design §4.4)
GUARD_TABLE = "nm-sshuttle-guard"

# Overridable for development and tests; packages use the defaults.
LIBEXEC = os.environ.get("NM_SSHUTTLE_LIBEXEC", "/usr/libexec/nm-sshuttle")
STATE_DIR = os.environ.get("NM_SSHUTTLE_STATE_DIR", "/run/nm-sshuttle")
TUNNEL_SPEC = os.path.join(STATE_DIR, "tunnel.json")
# NM versions on which the nbns reconnect never completed (design §4.4)
INVISIBLE_MARK = os.path.join(STATE_DIR, "invisible-reconnect")

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
