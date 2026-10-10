# SPDX-License-Identifier: MIT
from nm_sshuttle import cli
from nm_sshuttle.const import SERVICE_TYPE


def fake_nmcli(table):
    def run(*args):
        return table[args]
    return run


def test_active_sshuttle_vpns():
    nmcli = fake_nmcli({
        ("-g", "UUID,TYPE", "connection", "show", "--active"):
            "a:802-11-wireless\nb:vpn\nc:vpn\n",
        ("-g", "vpn.service-type", "connection", "show", "b"):
            "org.freedesktop.NetworkManager.openvpn\n",
        ("-g", "vpn.service-type", "connection", "show", "c"): SERVICE_TYPE + "\n",
    })
    assert cli.active_sshuttle_vpns(nmcli) == ["c"]
