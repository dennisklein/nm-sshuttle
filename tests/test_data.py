# SPDX-License-Identifier: MIT
"""The installed files carry the settings the design depends on."""

import configparser
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"


def ini(name):
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.optionxform = str
    cp.read(DATA / name)
    return cp


def test_conf_d_keeps_nmss0_unmanaged_without_replacing_the_list():
    cp = ini("90-nm-sshuttle.conf")
    assert dict(cp["keyfile"]) == {"unmanaged-devices+": "interface-name:nmss0"}


def test_plugin_unit_cleans_up_after_any_exit():
    svc = ini("nm-sshuttle.service.in")["Service"]
    assert svc["Type"] == "dbus"
    assert svc["ExecStopPost"].endswith("/nm-sshuttle cleanup")
    assert svc["Restart"] == "no"


def test_tunnel_unit_is_not_bound_to_the_plugin_unit():
    cp = ini("nm-sshuttle-tunnel.service.in")
    for key in ("BindsTo", "PartOf", "After", "Requires"):
        assert key not in cp["Unit"]
    assert cp["Service"]["Type"] == "notify" and cp["Service"]["Restart"] == "no"
    assert cp["Service"]["ExecStopPost"].endswith("/nm-sshuttle sweep")


def test_name_file():
    cp = ini("nm-sshuttle-service.name.in")
    vpn = cp["VPN Connection"]
    assert vpn["service"] == "org.freedesktop.NetworkManager.sshuttle"
    assert vpn["program"].endswith("/nm-sshuttle-activate")
    assert vpn["supports-safe-private-file-access"] == "true"
    assert cp["GNOME"]["auth-dialog"].endswith("/nm-sshuttle-auth-dialog")


def test_tunnel_unit_outlasts_the_plugins_longest_attempt():
    from nm_sshuttle.supervisor import Supervisor
    timeout = int(ini("nm-sshuttle-tunnel.service.in")["Service"]["TimeoutStartSec"])
    assert timeout > Supervisor.UNLOCK_START_TIMEOUT > Supervisor.TUNNEL_START_TIMEOUT
