# SPDX-License-Identifier: MIT
"""method=tproxy (docs/later/tproxy.md): sshuttle's mark, the guard's mark
rules, the policy routing, the sweep of mangle chains and the supervisor."""

import argparse
from types import SimpleNamespace

from fakes import NM, bring_up, fake_getpwnam, make, settings
from test_cli import Nmcli, conn, ssh_g_ok
from nm_sshuttle import cli, guard, tunnel
from nm_sshuttle.const import FAIL_CONNECT, ST_STOPPED, TPROXY_MARK
from nm_sshuttle.profile import parse

SPEC = {"remote": "corp", "user": "alice", "subnets": ["10.0.0.0/8"], "exclude": [],
        "dns": "none", "dns_servers": [], "method": "tproxy", "first_hop": ["203.0.113.7"],
        "tmark": TPROXY_MARK}


# ------------------------------------------------------------- profile, argv
def test_only_a_tproxy_spec_carries_the_mark():
    nft = parse(settings(), fake_getpwnam).tunnel_spec(["203.0.113.7"])
    assert "tmark" not in nft
    spec = parse(settings(method="tproxy"), fake_getpwnam).tunnel_spec(["203.0.113.7"])
    assert spec["method"] == "tproxy" and spec["tmark"] == 0x4e530000


def test_sshuttle_argv_passes_the_mark():
    argv = tunnel.sshuttle_argv(SPEC, "sshuttle")
    assert argv[argv.index("--method") + 1] == "tproxy"
    assert argv[argv.index("--tmark") + 1] == "0x4e530000"
    assert "--tmark" not in tunnel.sshuttle_argv(dict(SPEC, method="nft", tmark=None), "s")


# ------------------------------------------------------------------- guard
def test_the_guard_accepts_the_mark_ahead_of_the_dnat_rule():
    rules = guard.guard_ruleset("g", ["10.0.0.0/8", "2001:db8::/48"], [], TPROXY_MARK).splitlines()
    mark4 = rules.index("        ip daddr @subnets4 meta mark 0x4e530000 accept")
    mark6 = rules.index("        ip6 daddr @subnets6 meta mark 0x4e530000 accept")
    assert max(mark4, mark6) < rules.index("        ip daddr @subnets4 ct status dnat accept")
    assert "meta mark" not in guard.guard_ruleset("g", ["10.0.0.0/8"], [])


def fake_ip(monkeypatch, rules="", routes=""):
    """Record ip's argv; 'rule del' fails once nothing is left to delete."""
    calls = []
    state = {"rules": 1}

    def run(argv, input=None, timeout=10):
        calls.append(argv)
        if argv[2:4] == ["rule", "del"]:
            state["rules"] -= 1
            return state["rules"] >= 0, ""
        if argv[2:4] == ["rule", "list"]:
            return True, rules
        if argv[2:4] == ["route", "show"]:
            return True, routes
        return True, ""
    monkeypatch.setattr(guard, "run", run)
    return calls


def test_policy_routing_goes_by_exact_values(monkeypatch):
    calls = fake_ip(monkeypatch)
    guard.install_policy_routing([4, 6], 0)
    rule = ["priority", "100", "fwmark", "0x4e530000/0xffffffff", "lookup", "20051"]
    route = ["local", "default", "dev", "lo", "table", "20051"]
    assert ["ip", "-4", "rule", "add", *rule] in calls
    assert ["ip", "-6", "route", "replace", *route] in calls
    calls.clear()
    assert guard.remove_policy_routing(0)
    assert ["ip", "-4", "rule", "del", *rule] in calls
    assert not any("flush" in argv for argv in calls)


def test_policy_routing_is_read_back_per_family(monkeypatch):
    fake_ip(monkeypatch, rules="0:\tfrom all lookup local\n"
                               "100:\tfrom all fwmark 0x4e530000 lookup 20051\n",
            routes="local default dev lo scope host\n")
    assert guard.policy_routing_present([4, 6], 0)
    fake_ip(monkeypatch, rules="100:\tfrom all fwmark 0x1 lookup 100\n",
            routes="local default dev lo scope host\n")
    assert not guard.policy_routing_present([4], 0)


# ------------------------------------------------------------------- sweep
MANGLE = """-P PREROUTING ACCEPT
-P OUTPUT ACCEPT
-N sshuttle-d-12300
-N sshuttle-m-12300
-N sshuttle-t-12300
-N sshuttle-m-12301
-N other-chain
-A PREROUTING -j sshuttle-t-12300
-A OUTPUT -j sshuttle-m-12301
-A OUTPUT -j sshuttle-m-12300
-A sshuttle-m-12300 -d 10.0.0.0/8 -p udp -m udp -j MARK --set-xmark 0x4e530000/0xffffffff
"""


def test_mangle_chains_are_parsed():
    assert tunnel.mangle_chains(MANGLE) == {"sshuttle-d-12300": 12300,
                                            "sshuttle-m-12300": 12300,
                                            "sshuttle-t-12300": 12300,
                                            "sshuttle-m-12301": 12301}


def test_the_sweep_removes_the_chains_of_a_port_without_listener():
    cmds = tunnel.stale_chain_commands(MANGLE, ports={12301})
    assert cmds == [["-D", "PREROUTING", "-j", "sshuttle-t-12300"],
                    ["-D", "OUTPUT", "-j", "sshuttle-m-12300"],
                    ["-F", "sshuttle-d-12300"], ["-F", "sshuttle-m-12300"],
                    ["-F", "sshuttle-t-12300"],
                    ["-X", "sshuttle-d-12300"], ["-X", "sshuttle-m-12300"],
                    ["-X", "sshuttle-t-12300"]]
    assert tunnel.stale_chain_commands(MANGLE, ports={12300, 12301}) == []


def test_health_reads_the_mark_chain_for_tproxy(monkeypatch):
    monkeypatch.setattr(tunnel.subprocess, "run", lambda argv, **kw: SimpleNamespace(
        returncode=0, stdout=MANGLE if argv[0].endswith("tables") else
        "table inet g\ntable inet sshuttle-ipv4-12300\n"))
    monkeypatch.setattr(tunnel, "listening_ports", lambda: {12300})
    assert tunnel.nft_health("tproxy", "g") == {"guard": True, "sshuttle": {4, 6}}
    monkeypatch.setattr(tunnel, "listening_ports", lambda: {12399})
    assert tunnel.nft_health("tproxy", "g")["sshuttle"] == set()


# -------------------------------------------------------------- supervisor
def test_connect_installs_the_marked_guard_then_the_routing():
    fx, sup = make()
    bring_up(fx, sup, method="tproxy")
    names = fx.names()
    assert names.index("install_guard") < names.index("install_routing") < \
        names.index("start_tunnel")
    assert fx.guard_mark == TPROXY_MARK and fx.routing == {4}


def test_the_routing_is_installed_without_the_guard_too():
    fx, sup = make()
    bring_up(fx, sup, method="tproxy", **{"fail-closed": "no"})
    assert "install_guard" not in fx.names() and fx.routing == {4}


def test_the_routing_goes_with_the_guard():
    fx, sup = make()
    bring_up(fx, sup, method="tproxy")
    sup.disconnect()
    fx.advance(0.1)
    assert fx.names()[-2:] == ["remove_guard", "remove_routing"] and fx.routing is None
    fx2, sup2 = make()
    bring_up(fx2, sup2)
    sup2.disconnect()
    fx2.advance(0.1)
    assert "remove_routing" not in fx2.names()


def test_connect_fails_without_iptables():
    fx, sup = make()
    fx.iptables = False
    sup.connect(settings(method="tproxy"), NM)
    assert ("failure", FAIL_CONNECT) in fx.events and sup.state == ST_STOPPED
    assert "install_guard" not in fx.names() and "start_tunnel" not in fx.names()
    fx2, sup2 = make()
    fx2.iptables = False
    bring_up(fx2, sup2)                              # nft needs no iptables
    assert sup2.phase == "up"


def test_health_drops_without_the_mark_chain_and_restores_the_routing():
    fx, sup = make()
    bring_up(fx, sup, method="tproxy")
    fx.routing = None
    fx.advance(10)
    assert fx.health_methods[-1] == "tproxy"
    assert fx.names()[-1] == "install_routing" and fx.routing == {4} and sup.phase == "up"
    fx.health = {"guard": True, "sshuttle": set()}
    fx.advance(10)
    assert sup.phase == "gap"


# --------------------------------------------------------------------- cli
def test_check_wants_iptables_for_tproxy(capsys, monkeypatch):
    monkeypatch.setattr(cli.pwd, "getpwnam", lambda n: SimpleNamespace(pw_uid=1000))
    monkeypatch.setattr(cli.pwd, "getpwuid", lambda u: SimpleNamespace(pw_name="dennis"))
    data = "remote = corp, local-user = dennis, subnets = 10.0.0.0/8, method = tproxy"
    nm = Nmcli({"u1": conn(**{"vpn.data": data})})
    ok = SimpleNamespace(returncode=0)
    assert cli.check(argparse.Namespace(name="corp"), nm, ssh_g_ok, lambda a: ok,
                     lambda: False) == 1
    assert "FAIL method=tproxy needs iptables" in capsys.readouterr().out
    assert cli.check(argparse.Namespace(name="corp"), nm, ssh_g_ok, lambda a: ok,
                     lambda: True) == 0
