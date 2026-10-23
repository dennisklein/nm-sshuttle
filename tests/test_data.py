# SPDX-License-Identifier: MIT
"""The installed files carry the settings the design depends on."""

import configparser
import xml.etree.ElementTree as ET
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"


def ini(name):
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.optionxform = str
    cp.read(DATA / name)
    return cp


def test_conf_d_keeps_every_link_unmanaged_without_replacing_the_list():
    cp = ini("90-nm-sshuttle.conf")
    assert dict(cp["keyfile"]) == {"unmanaged-devices+": "interface-name:nmss*"}


def test_bus_policy_denies_only_by_destination():
    # dbus-daemon(1): a deny by interface alone also refuses every message
    # without an interface field, to any service on the bus.
    for deny in ET.parse(DATA / "nm-sshuttle.conf").getroot().iter("deny"):
        assert "send_destination" in deny.attrib or "own_prefix" in deny.attrib, deny.attrib


def test_plugin_unit_cleans_up_its_slot_after_any_exit():
    svc = ini("nm-sshuttle@.service.in")["Service"]
    assert svc["Type"] == "notify"
    assert svc["ExecStart"].endswith("/nm-sshuttle-service --slot %i")
    assert svc["ExecStopPost"].endswith("/nm-sshuttle cleanup --slot %i")
    assert svc["Restart"] == "no"


def test_only_the_dispatcher_is_bus_activated():
    act = ini("org.freedesktop.NetworkManager.sshuttle.Dispatcher.service.in")["D-BUS Service"]
    unit = ini("nm-sshuttle-dispatcher.service.in")["Service"]
    assert act["Name"] == unit["BusName"] == "org.freedesktop.NetworkManager.sshuttle.Dispatcher"
    assert act["SystemdService"] == "nm-sshuttle-dispatcher.service"
    assert unit["Type"] == "dbus"


def test_tunnel_unit_is_not_bound_to_the_plugin_unit():
    cp = ini("nm-sshuttle-tunnel@.service.in")
    for key in ("BindsTo", "PartOf", "After", "Requires"):
        assert key not in cp["Unit"]
    assert cp["Service"]["Type"] == "notify" and cp["Service"]["Restart"] == "no"
    assert cp["Service"]["ExecStart"].endswith("/nm-sshuttle exec-tunnel %i")
    assert cp["Service"]["ExecStopPost"].endswith("/nm-sshuttle sweep")


def test_name_file():
    cp = ini("nm-sshuttle-service.name.in")
    vpn = cp["VPN Connection"]
    assert vpn["service"] == "org.freedesktop.NetworkManager.sshuttle"
    assert vpn["program"].endswith("/nm-sshuttle-activate")
    assert vpn["supports-safe-private-file-access"] == "true"
    assert vpn["supports-multiple-connections"] == "true"
    assert cp["GNOME"]["auth-dialog"].endswith("/nm-sshuttle-auth-dialog")


def test_tunnel_unit_outlasts_the_plugins_longest_attempt():
    from nm_sshuttle.supervisor import Supervisor
    timeout = int(ini("nm-sshuttle-tunnel@.service.in")["Service"]["TimeoutStartSec"])
    assert timeout > Supervisor.UNLOCK_START_TIMEOUT > Supervisor.TUNNEL_START_TIMEOUT


def directives(name):
    """The [Service] keys of a unit, with space-separated values as sets."""
    return {k: set(v.split()) for k, v in ini(name)["Service"].items()}


UNITS = ("nm-sshuttle-tunnel@.service.in", "nm-sshuttle@.service.in",
         "nm-sshuttle-dispatcher.service.in")


def test_every_unit_has_the_common_sandbox():
    for name in UNITS:
        svc = directives(name)
        for key in ("ProtectKernelTunables", "ProtectControlGroups", "LockPersonality",
                    "RestrictRealtime", "RestrictSUIDSGID"):
            assert svc[key] == {"yes"}, (name, key)
        # iptables-legacy runs modprobe itself; nothing may set NoNewPrivileges,
        # which would stop setuid helpers and SELinux transitions (design §4.7)
        for key in ("ProtectKernelModules", "NoNewPrivileges", "ProtectClock",
                    "PrivateDevices", "SystemCallFilter", "MemoryDenyWriteExecute"):
            assert key not in svc, (name, key)


def test_the_tunnel_unit_keeps_etc_read_only_and_ssh_working():
    svc = directives("nm-sshuttle-tunnel@.service.in")
    # sshuttle's root firewall helper must not write /etc/hosts (design §4.8)
    assert svc["ProtectSystem"] == {"full"}
    assert not any(k.startswith(("ReadWritePaths", "ProtectHome", "PrivateTmp",
                                 "RestrictAddressFamilies", "RestrictNamespaces"))
                   for k in svc)
    assert svc["CapabilityBoundingSet"] == {
        "CAP_NET_ADMIN", "CAP_NET_RAW", "CAP_NET_BIND_SERVICE", "CAP_SETUID", "CAP_SETGID",
        "CAP_KILL", "CAP_DAC_READ_SEARCH", "CAP_SYS_PTRACE", "CAP_SYS_MODULE"}


def test_the_units_that_clean_up_can_write_etc_hosts():
    for name in ("nm-sshuttle@.service.in", "nm-sshuttle-dispatcher.service.in"):
        svc = directives(name)
        assert svc["ProtectSystem"] == {"true"}, name
        # they start units through systemd's D-Bus API (CAP_SYS_ADMIN), and
        # ssh-as-user resets the OOM floor (CAP_SYS_RESOURCE; design §4.7)
        assert "CapabilityBoundingSet" not in svc and "ReadOnlyPaths" not in svc, name
    assert "ProtectHome" not in directives("nm-sshuttle@.service.in")    # ssh -G as the user
