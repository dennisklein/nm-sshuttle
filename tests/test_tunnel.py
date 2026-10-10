# SPDX-License-Identifier: MIT
from nm_sshuttle import tunnel

SPEC = {"remote": "corp", "user": "alice", "subnets": ["10.0.0.0/8"], "exclude": ["10.0.5.0/24"],
        "dns": "split", "dns_servers": ["10.1.0.53", "10.1.0.54"], "method": "nft",
        "first_hop": "203.0.113.7"}


def test_sshuttle_argv():
    argv = tunnel.sshuttle_argv(SPEC, "/usr/bin/sshuttle")
    assert argv[:5] == ["/usr/bin/sshuttle", "-v", "--method", "nft", "--disable-ipv6"]
    assert argv[argv.index("-r") + 1] == "corp"
    assert ["-x", "203.0.113.7", "-x", "10.0.5.0/24"] == argv[7:11]
    assert argv[argv.index("-e") + 1].endswith("/nm-sshuttle ssh-as-user alice")
    assert argv[argv.index("--to-ns") + 1] == "10.1.0.53"
    assert argv[argv.index("--ns-hosts") + 1] == "10.1.0.53,10.1.0.54"
    assert argv[-1] == "10.0.0.0/8"      # the DNS /32s lie inside it and are merged
    assert "--dns" not in argv and "--auto-nets" not in argv


def test_sshuttle_argv_merges_a_dns_server_inside_a_subnet():
    spec = dict(SPEC, subnets=["10.0.0.0/8"], dns_servers=["10.1.0.53"])
    assert tunnel.sshuttle_argv(spec, "sshuttle")[-1:] == ["10.0.0.0/8"]


def test_sshuttle_argv_without_dns():
    argv = tunnel.sshuttle_argv(dict(SPEC, dns="none", dns_servers=[]), "sshuttle")
    assert "--to-ns" not in argv and argv[-1] == "10.0.0.0/8"


def test_parse_ssh_g():
    assert tunnel.parse_ssh_g("user alice\nhostname corp.example\nproxyjump none\n"
                              "proxycommand none\n") == ("corp.example", None)
    assert tunnel.parse_ssh_g("hostname 10.0.0.5\nproxyjump bob@jump.example:2222,"
                              "other\n") == ("10.0.0.5", "jump.example")
    assert tunnel.parse_ssh_g("hostname x\nproxyjump [2001:db8::1]:22\n") == ("x", "2001:db8::1")
    try:
        tunnel.parse_ssh_g("hostname x\nproxycommand nc %h %p\n")
    except ValueError as e:
        assert "gateway" in str(e)
    else:
        raise AssertionError("ProxyCommand must be refused")


def test_first_hop_follows_a_jump_alias_from_the_users_config():
    config = {
        "corp": "hostname 10.0.0.5\nproxyjump bastion\n",
        "bastion": "hostname 127.0.0.2\nproxyjump none\n",    # the alias resolves here
    }
    asked = []

    def ssh_g(user, target):
        asked.append((user, target))
        return config[target]
    assert tunnel.first_hop("alice", "corp", ssh_g=ssh_g) == "127.0.0.2"
    assert asked == [("alice", "corp"), ("alice", "bastion")]


def test_first_hop_direct_and_nested_jumps():
    assert tunnel.first_hop("a", "h", ssh_g=lambda u, t: "hostname 127.0.0.3\nproxyjump none\n") \
        == "127.0.0.3"
    chain = {"a": "hostname x\nproxyjump b\n", "b": "hostname y\nproxyjump c\n",
             "c": "hostname 127.0.0.4\nproxyjump none\n"}
    assert tunnel.first_hop("u", "a", ssh_g=lambda u, t: chain[t]) == "127.0.0.4"


def test_first_hop_gives_up_on_a_loop_and_on_option_like_hosts():
    import pytest
    with pytest.raises(ValueError, match="nested"):
        tunnel.first_hop("u", "a", ssh_g=lambda u, t: "hostname x\nproxyjump a\n")
    with pytest.raises(ValueError, match="unusable"):
        tunnel.first_hop("u", "a", ssh_g=lambda u, t: "hostname x\nproxyjump -oFoo=bar\n")


def test_classify_failure():
    assert tunnel.classify_failure("alice@corp: Permission denied (publickey).") == "login"
    assert tunnel.classify_failure("Host key verification failed.") == "login"
    assert tunnel.classify_failure("sign_and_send_pubkey: agent refused operation") == "login"
    assert tunnel.classify_failure("ssh: connect to host corp port 22: timed out") == "connect"


def test_stale_tables(tmp_path):
    tcp = tmp_path / "tcp"
    tcp.write_text(
        "  sl  local_address rem_address   st\n"
        "   0: 0100007F:3011 00000000:0000 0A 00000000:00000000\n"     # 12305 listening
        "   1: 0100007F:300C 0100007F:9999 01 00000000:00000000\n")    # 12300 established
    ports = tunnel.listening_ports((str(tcp), str(tmp_path / "missing")))
    assert ports == {12305}
    tables = ("table inet sshuttle-ipv4-12305\ntable inet sshuttle-ipv4-12300\n"
              "table inet nm-sshuttle-guard\ntable ip nat\ntable inet sshuttle-ipv6-12299\n")
    assert tunnel.stale_tables(tables, ports) == ["sshuttle-ipv4-12300", "sshuttle-ipv6-12299"]
