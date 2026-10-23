# SPDX-License-Identifier: MIT
"""Profile validation (design §4.2): the profile is untrusted input to root."""

import pytest

from fakes import fake_getpwnam, settings
from nm_sshuttle.profile import (MAX_HOPS, ProfileError, collapse, listed, parse,
                                 split_remote)


def p(s):
    return parse(s, getpwnam=fake_getpwnam)


def test_valid_profile():
    prof = p(settings(subnets="10.0.0.0/8, 172.20.0.0/16", exclude="10.0.5.0/24"))
    assert prof.remote == "corp" and prof.user == "alice"
    assert prof.subnets == ["10.0.0.0/8", "172.20.0.0/16"]
    assert prof.exclude == ["10.0.5.0/24"]
    assert prof.dns_servers == ["10.1.0.53"] and prof.dns_domains == ["~corp.example"]
    assert prof.method == "nft" and prof.fail_closed is True
    # the DNS server's /32 lies inside 10.0.0.0/8 and is merged into it
    assert prof.guarded_networks() == ["10.0.0.0/8", "172.20.0.0/16"]
    assert p(settings(**{"dns-servers": "192.168.5.5"})).guarded_networks() == [
        "10.0.0.0/8", "192.168.5.5/32"]
    spec = prof.tunnel_spec(["203.0.113.7"])
    assert spec["first_hop"] == ["203.0.113.7"] and spec["dns_servers"] == ["10.1.0.53"]


def test_ipv6_networks():
    prof = p(settings(subnets="10.0.0.0/8, 2001:db8:10::/48, 2001:db8:11::/48",
                      exclude="2001:db8:10:5::/64", gateway="2001:db8:ffff::7",
                      probe="[2001:DB8:10::22]:22"))
    assert prof.subnets == ["10.0.0.0/8", "2001:db8:10::/48", "2001:db8:11::/48"]
    assert prof.exclude == ["2001:db8:10:5::/64"]
    assert prof.gateway == "2001:db8:ffff::7"
    assert prof.probe == "[2001:db8:10::22]:22"
    assert prof.probe_target() == ("2001:db8:10::22", 22)
    assert prof.has_ipv6()
    # each family collapsed on its own, IPv4 first
    assert prof.guarded_networks() == ["10.0.0.0/8", "2001:db8:10::/47"]
    only6 = p(settings(subnets="fd00::/8", dns="none"))
    assert only6.guarded_networks() == ["fd00::/8"] and only6.has_ipv6()
    assert not p(settings()).has_ipv6()
    assert p(settings(probe="10.1.0.10:22")).probe_target() == ("10.1.0.10", 22)


def test_an_ipv6_dns_server_is_refused_with_the_reason():
    with pytest.raises(ProfileError, match="only IPv4 DNS servers"):
        p(settings(**{"dns-servers": "10.1.0.53 2001:db8::53"}))


def test_dns_modes():
    assert p(settings(dns="all")).dns_domains == ["~."]
    none = p(settings(dns="none"))
    assert none.dns_servers == [] and none.dns_domains == []
    assert none.guarded_networks() == ["10.0.0.0/8"]


def test_root_is_not_a_regular_user():
    def getpwnam(name):
        import types
        return types.SimpleNamespace(pw_uid=0, pw_gid=0, pw_dir="/root")
    s = settings(**{"local-user": "root"})
    s["connection"]["permissions"] = ["user:root:"]
    with pytest.raises(ProfileError, match="regular user"):
        parse(s, getpwnam=getpwnam)


def test_permissions_must_name_exactly_the_local_user():
    s = settings()
    s["connection"]["permissions"] = []
    with pytest.raises(ProfileError, match="permissions"):
        p(s)
    s["connection"]["permissions"] = ["user:alice:", "user:bob:"]
    with pytest.raises(ProfileError, match="permissions"):
        p(s)


def test_persistent_and_ipv4_method_are_required():
    s = settings()
    del s["vpn"]["persistent"]
    with pytest.raises(ProfileError, match="persistent"):
        p(s)
    s = settings()
    s["ipv4"]["method"] = "manual"
    with pytest.raises(ProfileError, match="ipv4.method"):
        p(s)


def test_unknown_keys_are_reported_not_used():
    assert p(settings(**{"ssh-options": "-oFoo"})).ignored_keys == ["ssh-options"]


INCLUDE = 'fe80::%x};include"passwd";/64'


@pytest.mark.parametrize("key,value", [
    ("subnets", INCLUDE), ("subnets", "10.0.0.0/8 fe80::%eth0/64"), ("exclude", INCLUDE),
    ("gateway", "fe80::1%x }\ninclude \"passwd\""), ("gateway", "fe80::1%eth0"),
    ("probe", "[fe80::1%a b]:22"), ("dns-servers", "fe80::53%eth0")])
def test_a_scope_id_is_refused(key, value):
    # ipaddress keeps any text after "%" in str(), and that text would reach
    # the root nft script and sshuttle's argv.
    with pytest.raises(ProfileError, match=f"^{key}: .* has a scope id"):
        p(settings(**{key: value}))


def test_stored_networks_are_address_text_only():
    prof = p(settings(subnets="10.1.2.3/8, 2001:DB8:10::1/48", exclude="2001:db8:10:5::7",
                      gateway="2001:DB8::7"))
    assert prof.subnets == ["10.0.0.0/8", "2001:db8:10::/48"]
    assert prof.exclude == ["2001:db8:10:5::7/128"]
    assert prof.gateway == "2001:db8::7"


def test_collapse_drops_a_scope_that_got_through():
    # Even past validation, a network's text is rebuilt from its number.
    assert collapse([INCLUDE, "fe80::1%eth0", "10.0.0.0/8"]) == [
        "10.0.0.0/8", "fe80::/64"]
    assert collapse(["fe80::%a b/10"]) == ["fe80::/10"]


@pytest.mark.parametrize("key,value", [
    ("remote", "corp:" + "1" * 4400), ("probe", "10.1.0.10:" + "1" * 4400),
    ("probe", "10.1.0.10:\u00b2"), ("probe", "10.1.0.10:\u0661")],
    ids=["remote-long", "probe-long", "probe-superscript", "probe-arabic-indic"])
def test_a_port_beyond_int_or_ascii_is_a_profile_error(key, value):
    # int() refuses over 4300 digits with a ValueError of its own, and
    # str.isdigit() takes "²", which int() refuses, and "١", which it reads.
    with pytest.raises(ProfileError, match=f"^{key}: "):
        p(settings(**{key: value}))


def test_a_port_may_have_leading_zeros():
    assert p(settings(remote="corp:000022")).remote == "corp:000022"
    assert p(settings(probe="10.1.0.10:000022")).probe == "10.1.0.10:22"


@pytest.mark.parametrize("remote", ["alice:hunter2@corp", ":@", "a:b@c@corp"])
def test_a_password_in_remote_is_refused_and_not_logged(remote):
    with pytest.raises(ProfileError, match="^remote: the user part must not contain ':'") as e:
        p(settings(remote=remote))
    assert "hunter2" not in str(e.value) and remote not in str(e.value)


def test_split_remote_splits_as_sshuttle_does():
    assert split_remote("corp") == ("", "corp", None)
    assert split_remote("alice@corp:022") == ("alice", "corp", 22)
    assert split_remote("alice@[2001:db8::7]:2222") == ("alice", "2001:db8::7", 2222)
    assert split_remote("2001:db8::7") == ("", "2001:db8::7", None)
    with pytest.raises(ProfileError):
        split_remote("corp:2222:1")


def test_first_hop_addresses_inside_the_guarded_networks_are_refused():
    # A lookup on the local network may be spoofed (design §4.2).
    prof = p(settings(subnets="10.0.0.0/8, 2001:db8:10::/48", exclude="10.0.5.0/24",
                      **{"dns-servers": "192.168.5.5"}))
    hops = ["203.0.113.7", "10.20.30.40", "192.168.5.5", "10.0.5.9", "2001:db8:10::7",
            "2001:db8:ffff::7", "not-an-address"]
    assert prof.first_hop_addresses(hops) == (
        ["203.0.113.7", "10.0.5.9", "2001:db8:ffff::7"],
        ["10.20.30.40", "192.168.5.5", "2001:db8:10::7"], [])
    # given as an address, the first hop was never looked up: as 'gateway'
    assert prof.first_hop_addresses(["10.20.30.40"], given=True) == (["10.20.30.40"], [], [])
    # without DNS through the tunnel, its servers are not guarded
    prof = p(settings(dns="none", **{"dns-servers": "192.168.5.5"}))
    assert prof.first_hop_addresses(["192.168.5.5"]) == (["192.168.5.5"], [], [])


def test_first_hop_addresses_are_capped():
    hops = [f"198.51.100.{i}" for i in range(1, 12)]
    usable, refused, dropped = p(settings()).first_hop_addresses(hops)
    assert usable == hops[:MAX_HOPS] and dropped == hops[MAX_HOPS:] and refused == []
    assert MAX_HOPS == 8
    assert listed(hops) == ", ".join(hops[:MAX_HOPS]) + " and 3 more"
    assert listed(hops[:2]) == "198.51.100.1, 198.51.100.2"
