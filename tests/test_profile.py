# SPDX-License-Identifier: MIT
"""Profile validation (design §4.2): the profile is untrusted input to root."""

import pytest

from fakes import fake_getpwnam, settings
from nm_sshuttle.profile import ProfileError, parse


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
