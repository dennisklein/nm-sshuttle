# SPDX-License-Identifier: MIT
import shutil
import subprocess

import pytest

from nm_sshuttle import guard


def test_guard_ruleset_matches_the_design():
    rules = guard.guard_ruleset(["10.0.0.0/8", "192.168.1.0/24"], ["203.0.113.7"])
    assert "table inet nm-sshuttle-guard {" in rules
    assert "elements = { 10.0.0.0/8, 192.168.1.0/24 }" in rules
    assert "elements = { 203.0.113.7/32 }" in rules
    lines = [ln.strip() for ln in rules.splitlines()]
    i = lines.index("ip daddr @exclude4 accept")
    assert lines[i:i + 4] == [
        "ip daddr @exclude4 accept",
        "ip daddr @subnets4 ct status dnat accept",
        "ip daddr @subnets4 meta l4proto tcp reject with tcp reset",
        "ip daddr @subnets4 reject with icmp admin-prohibited",
    ]


def nft_usable():
    if not shutil.which("nft"):
        return False
    return subprocess.run(["nft", "list", "tables"], capture_output=True).returncode == 0


@pytest.mark.skipif(not nft_usable(), reason="needs a working nft (root)")
def test_guard_ruleset_parses_in_nft():
    for exclude in (["203.0.113.7", "10.0.5.0/24"], []):
        rules = guard.guard_ruleset(["10.0.0.0/8"], exclude)
        p = subprocess.run(["nft", "-c", "-f", "-"], input=rules, text=True,
                           capture_output=True)
        assert p.returncode == 0, p.stderr


def test_overlapping_networks_are_merged_for_nft():
    # A DNS server's /32 inside a subnet made nft refuse the set (first VM run).
    rules = guard.guard_ruleset(["10.99.0.0/24", "10.99.0.53/32"], ["1.2.3.4", "1.2.3.4/32"])
    assert "elements = { 10.99.0.0/24 }" in rules
    assert "elements = { 1.2.3.4/32 }" in rules


@pytest.mark.skipif(not nft_usable(), reason="needs a working nft (root)")
def test_overlapping_networks_install_in_nft():
    rules = guard.guard_ruleset(["10.99.0.0/24", "10.99.0.53/32", "10.0.0.0/8"], ["1.2.3.4"])
    p = subprocess.run(["nft", "-c", "-f", "-"], input=rules, text=True, capture_output=True)
    assert p.returncode == 0, p.stderr
