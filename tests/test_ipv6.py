# SPDX-License-Identifier: MIT
"""IPv6 networks and first hops in the supervisor
(docs/later/ipv6.md), with fake events and a fake clock."""

from fakes import AC, NM, bring_up, make, settings
from nm_sshuttle.const import AC_ACTIVATING, CONN_FULL, FAIL_CONNECT, ST_STOPPED

V6 = "10.0.0.0/8, 2001:db8:10::/48"
WIFI = ("wifi-1", ("192.168.1.20/24",), "192.168.1.1")


def connect(fx, sup, hops, **data):
    sup.connect(settings(**data), NM)
    fx.first_hop_cbs.pop(0)(hops, None)


def spec(fx):
    return [e[1] for e in fx.events if e[0] == "spec"][-1]


def test_ipv6_networks_are_guarded_and_tunnelled():
    fx, sup = make()
    connect(fx, sup, ["203.0.113.7", "2001:db8:ffff::7"], subnets=V6,
            exclude="2001:db8:10:5::/64")
    guard = [e for e in fx.events if e[0] == "install_guard"][0]
    assert guard == ("install_guard", ["10.0.0.0/8", "2001:db8:10::/48"],
                     ["203.0.113.7", "2001:db8:ffff::7", "2001:db8:10:5::/64"])
    assert spec(fx)["ipv6"] is True
    assert spec(fx)["first_hop"] == ["203.0.113.7", "2001:db8:ffff::7"]
    fx.finish_start(True)
    config = [e[1] for e in fx.events if e[0] == "config"][0]
    assert config["gateway"] == "203.0.113.7"        # IPv4 first, whatever the order


def test_a_first_hop_with_only_an_ipv6_address_is_the_gateway():
    fx, sup = make()
    connect(fx, sup, ["2001:db8:ffff::7"])
    assert spec(fx)["ipv6"] is False                 # an IPv4 profile tunnels no IPv6
    fx.finish_start(True)
    assert [e[1] for e in fx.events if e[0] == "config"][0]["gateway"] == "2001:db8:ffff::7"


def test_ipv6_off_on_the_host_guards_but_does_not_tunnel(caplog):
    fx, sup = make()
    fx.ipv6 = False
    connect(fx, sup, ["203.0.113.7"], subnets=V6)
    assert spec(fx)["ipv6"] is False
    assert [e for e in fx.events if e[0] == "install_guard"][0][1] == [
        "10.0.0.0/8", "2001:db8:10::/48"]
    assert "guarded but not tunnelled" in caplog.text


def test_an_ipv6_only_profile_fails_when_ipv6_is_off():
    fx, sup = make()
    fx.ipv6 = False
    sup.connect(settings(subnets="2001:db8:10::/48", dns="none"), NM)
    assert ("failure", FAIL_CONNECT) in fx.events and sup.state == ST_STOPPED
    assert "start_tunnel" not in fx.names()


def test_health_wants_a_table_for_each_tunnelled_family():
    fx, sup = make()
    bring_up(fx, sup, subnets=V6)
    fx.clear()
    fx.health = {"guard": True, "sshuttle": {4}}
    fx.advance(10)
    assert sup.phase == "gap"
    fx2, sup2 = make()
    fx2.ipv6 = False
    bring_up(fx2, sup2, subnets=V6)
    fx2.health = {"guard": True, "sshuttle": {4}}
    fx2.advance(10)
    assert sup2.phase == "up"                        # IPv6 is not tunnelled
    fx3, sup3 = make()
    bring_up(fx3, sup3, subnets="2001:db8:10::/48", dns="none")
    fx3.health = {"guard": True, "sshuttle": {6}}
    fx3.advance(10)
    assert sup3.phase == "up"                        # no IPv4 network, no IPv4 table


def test_a_refreshed_first_hop_list_updates_guard_spec_and_config():
    fx, sup = make()
    sup.on_network(True, CONN_FULL, WIFI)
    bring_up(fx, sup, subnets=V6)
    fx.clear()
    sup.on_network(True, CONN_FULL, ("wifi-2",) + WIFI[1:])
    fx.finish_stops()
    fx.advance(0)
    fx.first_hop_cbs.pop(0)(["2001:db8:ffff::9", "198.51.100.9"], None)
    assert fx.events[-3:-1] == [
        ("install_guard", ["10.0.0.0/8", "2001:db8:10::/48"],
         ["2001:db8:ffff::9", "198.51.100.9"]),
        ("spec", dict(spec(fx), first_hop=["2001:db8:ffff::9", "198.51.100.9"]))]
    sup.on_ac_state(AC, AC_ACTIVATING, 6)
    fx.finish_start(True)
    assert [e[1] for e in fx.events if e[0] == "config"][-1]["gateway"] == "198.51.100.9"
