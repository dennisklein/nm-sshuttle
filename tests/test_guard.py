# SPDX-License-Identifier: MIT
import shutil
import subprocess

import pytest

from nm_sshuttle import guard

T = "nm-sshuttle-guard-0"


def test_guard_ruleset_matches_the_design():
    rules = guard.guard_ruleset(T, ["10.0.0.0/8", "192.168.1.0/24"], ["203.0.113.7"])
    assert "table inet nm-sshuttle-guard-0 {" in rules
    assert "elements = { 10.0.0.0/8, 192.168.1.0/24 }" in rules
    assert "elements = { 203.0.113.7/32 }" in rules
    assert "set subnets6 { type ipv6_addr; flags interval; }" in rules
    assert "set exclude6 { type ipv6_addr; flags interval; }" in rules
    lines = [ln.strip() for ln in rules.splitlines()]
    i = lines.index("ip daddr @exclude4 accept")
    assert lines[i:i + 8] == [
        "ip daddr @exclude4 accept",
        "ip6 daddr @exclude6 accept",
        "ip daddr @subnets4 ct status dnat accept",
        "ip6 daddr @subnets6 ct status dnat accept",
        "ip daddr @subnets4 meta l4proto tcp reject with tcp reset",
        "ip6 daddr @subnets6 meta l4proto tcp reject with tcp reset",
        "ip daddr @subnets4 reject with icmpx admin-prohibited",
        "ip6 daddr @subnets6 reject with icmpx admin-prohibited",
    ]


def test_guard_ruleset_splits_the_families():
    rules = guard.guard_ruleset(T, ["10.0.0.0/8", "2001:db8:10::/48", "2001:db8:11::/48"],
                                ["203.0.113.7", "2001:db8:ffff::7", "2001:db8:10::/64"])
    assert "set subnets4 { type ipv4_addr; flags interval; elements = { 10.0.0.0/8 } }" in rules
    assert "set subnets6 { type ipv6_addr; flags interval; elements = { 2001:db8:10::/47 } }" \
        in rules
    assert "set exclude4 { type ipv4_addr; flags interval; elements = { 203.0.113.7/32 } }" \
        in rules
    assert ("set exclude6 { type ipv6_addr; flags interval; elements = { 2001:db8:10::/64, "
            "2001:db8:ffff::7/128 } }") in rules
    only6 = guard.guard_ruleset(T, ["2001:db8::/32"], ["2001:db8::1"])
    assert "set subnets4 { type ipv4_addr; flags interval; }" in only6


def nft_usable():
    if not shutil.which("nft"):
        return False
    return subprocess.run(["nft", "list", "tables"], capture_output=True).returncode == 0


@pytest.mark.skipif(not nft_usable(), reason="needs a working nft (root)")
def test_guard_ruleset_parses_in_nft():
    for networks, exclude in ((["10.0.0.0/8"], ["203.0.113.7", "10.0.5.0/24"]),
                              (["10.0.0.0/8"], []),
                              (["10.0.0.0/8", "2001:db8:10::/48"], ["2001:db8:ffff::7"]),
                              (["2001:db8::/32"], [])):
        rules = guard.guard_ruleset(T, networks, exclude)
        p = subprocess.run(["nft", "-c", "-f", "-"], input=rules, text=True,
                           capture_output=True)
        assert p.returncode == 0, p.stderr


def test_overlapping_networks_are_merged_for_nft():
    # A DNS server's /32 inside a subnet made nft refuse the set (first VM run).
    rules = guard.guard_ruleset(T, ["10.99.0.0/24", "10.99.0.53/32"], ["1.2.3.4", "1.2.3.4/32"])
    assert "elements = { 10.99.0.0/24 }" in rules
    assert "elements = { 1.2.3.4/32 }" in rules


@pytest.mark.skipif(not nft_usable(), reason="needs a working nft (root)")
def test_overlapping_networks_install_in_nft():
    rules = guard.guard_ruleset(T, ["10.99.0.0/24", "10.99.0.53/32", "10.0.0.0/8"], ["1.2.3.4"])
    p = subprocess.run(["nft", "-c", "-f", "-"], input=rules, text=True, capture_output=True)
    assert p.returncode == 0, p.stderr


INCLUDE = 'fe80::%x};include"passwd";/64'


def test_guard_ruleset_holds_only_address_text():
    # A scope id is refused by profile.parse; should one get past it, its text
    # still never reaches the root nft script.
    rules = guard.guard_ruleset(T, ["10.0.0.0/8", INCLUDE], [INCLUDE, "fe80::1%a }\nb"])
    assert "set subnets6 { type ipv6_addr; flags interval; elements = { fe80::/64 } }" in rules
    assert "set exclude6 { type ipv6_addr; flags interval; elements = { fe80::/64 } }" in rules
    assert "%" not in rules and "include" not in rules and '"' not in rules


@pytest.mark.skipif(not nft_usable(), reason="needs a working nft (root)")
def test_guard_ruleset_with_a_scope_parses_in_nft():
    rules = guard.guard_ruleset(T, ["10.0.0.0/8", INCLUDE], [INCLUDE])
    p = subprocess.run(["nft", "-c", "-f", "-"], input=rules, text=True, capture_output=True)
    assert p.returncode == 0, p.stderr


def test_install_guard_reports_only_the_error_line_of_nft(monkeypatch):
    # nft quotes the lines it could not parse, including those of a file that
    # the script included; only the line naming the error is passed on.
    out = ("Warning: a warning first\n"
           "/etc/passwd:1:1-4: Error: syntax error, unexpected string\n"
           "root:x:0:0:root:/root:/bin/bash\n^^^^")
    monkeypatch.setattr(guard, "run", lambda argv, input=None: (False, out))
    with pytest.raises(RuntimeError) as e:
        guard.install_guard(T, ["10.0.0.0/8"], [])
    assert str(e.value) == ("cannot install the guard table: "
                            "/etc/passwd:1:1-4: Error: syntax error, unexpected string")
    monkeypatch.setattr(guard, "run", lambda argv, input=None: (False, "nft: odd\nmore"))
    with pytest.raises(RuntimeError, match="table: nft: odd$"):
        guard.install_guard(T, ["10.0.0.0/8"], [])
    monkeypatch.setattr(guard, "run", lambda argv, input=None: (False, ""))
    with pytest.raises(RuntimeError, match="nft failed"):
        guard.install_guard(T, ["10.0.0.0/8"], [])
