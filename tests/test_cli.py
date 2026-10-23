# SPDX-License-Identifier: MIT
import argparse
from types import SimpleNamespace

import pytest

from nm_sshuttle import cli, profile
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


def terse(value):
    """How `nmcli -g` prints a value: ':' and '\\' escaped."""
    return value.replace("\\", "\\\\").replace(":", "\\:")


class Nmcli:
    """Records `connection add`/`delete`; answers queries from a table, escaped
    as `nmcli -g` escapes them."""
    def __init__(self, conns=None, active=()):
        self.conns, self.calls, self.active = conns or {}, [], set(active)

    def __call__(self, *args):
        if args[:3] == ("-g", "UUID,TYPE", "connection"):
            shown = self.active if "--active" in args else self.conns
            return "".join(f"{u}:vpn\n" for u in self.conns if u in shown)
        if args[0] == "-g" and args[2:4] == ("connection", "show"):
            return terse(self.conns[args[4]].get(args[1], "")) + "\n"
        self.calls.append(args)
        return ""


def conn(name="corp", **over):
    c = {"vpn.service-type": SERVICE_TYPE, "connection.id": name,
         "connection.permissions": "user:dennis", "vpn.persistent": "yes",
         "ipv4.method": "auto", "ipv6.auto-route-ext-gw": "no",
         "vpn.data": "remote = corp, local-user = dennis, subnets = 10.0.0.0/8 172.20.0.0/16"}
    return {**c, **over}


def add_args(**over):
    ns = dict(name="corp", remote="corp", subnets="10.0.0.0/8,172.20.0.0/16", user="dennis",
              exclude=None, dns="split", dns_servers="10.1.0.53", dns_domains="a.example,b.example",
              method=None, gateway=None, probe=None, fail_closed=None)
    return argparse.Namespace(**{**ns, **over})


@pytest.fixture
def dennis(monkeypatch):
    pw = SimpleNamespace(pw_uid=1000, pw_gid=1000, pw_name="dennis")
    monkeypatch.setattr(cli.pwd, "getpwnam", lambda n: pw if n == "dennis" else 1 / 0)
    monkeypatch.setattr(cli.pwd, "getpwuid", lambda uid: pw)


def test_add_sets_the_defaults(dennis):
    nm = Nmcli()
    assert cli.add(add_args(), nm) == 0
    argv = nm.calls[0]
    assert argv[:6] == ("connection", "add", "type", "vpn", "con-name", "corp")
    for pair in [("connection.permissions", "user:dennis"), ("connection.autoconnect", "no"),
                 ("vpn.persistent", "yes"), ("ipv4.method", "auto"),
                 ("ipv4.auto-route-ext-gw", "no"), ("ipv6.auto-route-ext-gw", "no"),
                 ("ipv4.never-default", "yes"), ("ipv6.never-default", "yes")]:
        assert argv[argv.index(pair[0]) + 1] == pair[1]
    data = cli.parse_vpn_data(argv[argv.index("vpn.data") + 1])
    assert data["subnets"] == "10.0.0.0/8 172.20.0.0/16"
    assert data["dns-domains"] == "a.example b.example"
    assert "method" not in data


def test_add_rejects_what_the_plugin_would(dennis):
    nm = Nmcli()
    with pytest.raises(profile.ProfileError):
        cli.add(add_args(remote="-oProxyCommand=x"), nm)
    with pytest.raises(profile.ProfileError):
        cli.add(add_args(subnets="not-a-net"), nm)
    with pytest.raises(RuntimeError):
        cli.add(add_args(name="-x"), nm)
    assert nm.calls == []


def test_add_refuses_a_duplicate(dennis):
    with pytest.raises(RuntimeError, match="exists"):
        cli.add(add_args(), Nmcli({"u1": conn()}))


def test_parse_vpn_data():
    assert cli.parse_vpn_data("remote = corp, subnets = 10.0.0.0/8 10.1.0.0/16, dns = none") == {
        "remote": "corp", "subnets": "10.0.0.0/8 10.1.0.0/16", "dns": "none"}


def test_list_and_remove(dennis):
    nm = Nmcli({"u1": conn(), "u2": conn("other")})
    out = []
    cli.list_profiles(nm, out.append)
    assert out == ["corp\tcorp\t10.0.0.0/8 172.20.0.0/16", "other\tcorp\t10.0.0.0/8 172.20.0.0/16"]
    cli.remove(argparse.Namespace(name="other"), nm)
    assert nm.calls == [("connection", "delete", "uuid", "u2")]
    with pytest.raises(RuntimeError, match="no nm-sshuttle"):
        cli.remove(argparse.Namespace(name="nope"), nm)


def ssh_g_ok(_user, target):
    return "hostname 192.0.2.7\nproxyjump none\n"


def test_check_passes(dennis, capsys):
    ran = []
    def run(argv):
        ran.append(argv)
        return SimpleNamespace(returncode=0)
    assert cli.check(argparse.Namespace(name="corp"), Nmcli({"u1": conn()}), ssh_g_ok, run,
                     port_start=lambda: 1024, cgroup=lambda unit: "/sys/fs/cgroup/x") == 0
    assert ran == [["ssh", "-o", "ConnectTimeout=15", "--", "corp", "true"]]
    out = capsys.readouterr().out
    assert "first hop 192.0.2.7" in out and "WARN" not in out


SPLIT = "remote = corp, local-user = dennis, subnets = 10.0.0.0/8, dns = split, " \
        "dns-servers = 10.1.0.53, dns-domains = ~corp.example"


def test_check_wants_resolved_for_split_dns(dennis, capsys):
    nm = Nmcli({"u1": conn(**{"vpn.data": SPLIT})})
    def run(argv):
        return SimpleNamespace(returncode=0)
    assert cli.check(argparse.Namespace(name="corp"), nm, ssh_g_ok, run,
                     dns_mode=lambda: "default") == 1
    assert "FAIL dns=split needs NetworkManager to use systemd-resolved; its DNS mode is " \
        "default" in capsys.readouterr().out
    assert cli.check(argparse.Namespace(name="corp"), nm, ssh_g_ok, run,
                     dns_mode=lambda: "systemd-resolved", resolv_conf=lambda: None) == 0
    assert cli.check(argparse.Namespace(name="corp"), nm, ssh_g_ok, run,
                     dns_mode=lambda: None, resolv_conf=lambda: None) == 0
    assert "WARN cannot read NetworkManager's DNS mode" in capsys.readouterr().out
    # resolv.conf linked to resolved's list of the real servers
    assert cli.check(argparse.Namespace(name="corp"), nm, ssh_g_ok, run,
                     dns_mode=lambda: "systemd-resolved",
                     resolv_conf=lambda: "/etc/resolv.conf lists 192.168.1.1") == 1
    assert "FAIL dns=split needs every lookup to go to systemd-resolved; /etc/resolv.conf " \
        "lists 192.168.1.1" in capsys.readouterr().out


def test_nm_dns_mode_from_busctl():
    def run(argv, **kw):
        assert argv[:3] == ["busctl", "--system", "get-property"] and argv[-1] == "Mode"
        return SimpleNamespace(returncode=0, stdout='s "systemd-resolved"\n')
    assert cli.nm_dns_mode(run) == "systemd-resolved"
    assert cli.nm_dns_mode(lambda argv, **kw: SimpleNamespace(returncode=1, stdout="")) is None


def test_check_warns_without_systemds_cgroups(dennis, capsys):
    def run(argv):
        return SimpleNamespace(returncode=0)
    assert cli.check(argparse.Namespace(name="corp"), Nmcli({"u1": conn()}), ssh_g_ok, run,
                     port_start=lambda: 1024, cgroup=lambda unit: None) == 0
    assert "WARN no cgroup hierarchy under /sys/fs/cgroup holds systemd's units" in \
        capsys.readouterr().out


def test_check_warns_when_users_may_bind_the_redirect_ports(dennis, capsys):
    def run(argv):
        return SimpleNamespace(returncode=0)
    assert cli.check(argparse.Namespace(name="corp"), Nmcli({"u1": conn()}), ssh_g_ok, run,
                     port_start=lambda: 0) == 0
    assert "WARN net.ipv4.ip_unprivileged_port_start is 0: any user may bind sshuttle's " \
        "redirect ports 225-240" in capsys.readouterr().out


def test_check_reads_escaped_fields(dennis, capsys):
    data = "remote = corp, local-user = dennis, subnets = 10.0.0.0/8, probe = 10.1.0.22:22"
    nm = Nmcli({"u1": conn(**{"vpn.data": data})})
    assert terse("user:dennis") == "user\\:dennis"
    assert cli.read_settings("u1", nm)["vpn"]["data"]["probe"] == "10.1.0.22:22"
    def run(argv):
        return SimpleNamespace(returncode=0)
    assert cli.check(argparse.Namespace(name="corp"), nm, ssh_g_ok, run) == 0
    assert "FAIL" not in capsys.readouterr().out


def test_check_warns_about_a_profile_from_before_ipv6(dennis, capsys):
    def run(argv):
        return SimpleNamespace(returncode=0)
    nm = Nmcli({"u1": conn(**{"ipv6.auto-route-ext-gw": "-1 (default)"})})
    assert cli.check(argparse.Namespace(name="corp"), nm, ssh_g_ok, run) == 0
    assert "WARN ipv6.auto-route-ext-gw is not no" in capsys.readouterr().out


def test_check_flags_gnome_edits(dennis, capsys):
    for over in ({"connection.permissions": ""}, {"ipv4.method": "manual"},
                 {"vpn.persistent": "no"}):
        nm = Nmcli({"u1": conn(**over)})
        assert cli.check(argparse.Namespace(name="corp"), nm, ssh_g_ok, None) == 1
        assert "FAIL profile" in capsys.readouterr().out


def test_check_reports_a_failed_login(dennis, capsys):
    def run(argv):
        return SimpleNamespace(returncode=255)
    assert cli.check(argparse.Namespace(name="corp"), Nmcli({"u1": conn()}), ssh_g_ok, run) == 1
    assert "FAIL test login" in capsys.readouterr().out
