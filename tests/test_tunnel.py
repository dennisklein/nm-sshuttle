# SPDX-License-Identifier: MIT
import json
import os
import shlex
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from nm_sshuttle import const, profile, tunnel
from nm_sshuttle.const import LISTEN_PORTS, SLOTS, Slot

SPEC = {"remote": "corp", "user": "alice", "subnets": ["10.0.0.0/8"], "exclude": ["10.0.5.0/24"],
        "dns": "split", "dns_servers": ["10.1.0.53", "10.1.0.54"], "method": "nft",
        "first_hop": ["203.0.113.7"]}


def test_sshuttle_argv():
    argv = tunnel.sshuttle_argv(SPEC, "/usr/bin/sshuttle")
    assert argv[:5] == ["/usr/bin/sshuttle", "-v", "--method", "nft", "--disable-ipv6"]
    assert argv[argv.index("-r") + 1] == "127.0.0.1"    # the stand-in: never looked up
    assert ["-x", "203.0.113.7", "-x", "10.0.5.0/24"] == argv[7:11]
    assert argv[argv.index("-e") + 1].endswith("/nm-sshuttle ssh-as-user alice --remote corp")
    assert argv[argv.index("--to-ns") + 1] == "10.1.0.53"
    assert argv[argv.index("--ns-hosts") + 1] == "10.1.0.53,10.1.0.54"
    assert argv[-1] == "10.0.0.0/8"      # the DNS /32s lie inside it and are merged
    assert "--dns" not in argv and "--auto-nets" not in argv


def test_sshuttle_argv_reads_a_spec_from_before_ipv6():
    argv = tunnel.sshuttle_argv(dict(SPEC, first_hop="203.0.113.7"), "sshuttle")
    assert "--disable-ipv6" in argv and argv[argv.index("-x") + 1] == "203.0.113.7"


def test_sshuttle_argv_with_ipv6():
    spec = dict(SPEC, subnets=["10.0.0.0/8", "2001:db8:10::/48"], exclude=["2001:db8:10:5::/64"],
                first_hop=["203.0.113.7", "2001:db8:ffff::7"], ipv6=True)
    argv = tunnel.sshuttle_argv(spec, "sshuttle")
    assert "--disable-ipv6" not in argv
    xs = [argv[i + 1] for i, a in enumerate(argv) if a == "-x"]
    assert xs == ["203.0.113.7", "2001:db8:ffff::7", "2001:db8:10:5::/64"]
    assert argv[-2:] == ["10.0.0.0/8", "2001:db8:10::/48"]


def test_sshuttle_argv_with_ipv6_off_on_the_host():
    # The plugin found IPv6 off: sshuttle gets no IPv6 at all, the guard still has it.
    spec = dict(SPEC, subnets=["10.0.0.0/8", "2001:db8:10::/48"], exclude=["2001:db8:10:5::/64"],
                first_hop=["203.0.113.7", "2001:db8:ffff::7"], ipv6=False)
    argv = tunnel.sshuttle_argv(spec, "sshuttle")
    assert "--disable-ipv6" in argv
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "-x"] == ["203.0.113.7"]
    assert argv[-1] == "10.0.0.0/8"


def test_ipv6_available(tmp_path):
    lo, inet6 = tmp_path / "disable_ipv6", tmp_path / "if_inet6"
    lo.write_text("0\n")
    inet6.write_text("")
    assert tunnel.ipv6_available(str(lo), str(inet6))
    lo.write_text("1\n")
    assert not tunnel.ipv6_available(str(lo), str(inet6))
    lo.write_text("0\n")
    assert not tunnel.ipv6_available(str(lo), str(tmp_path / "missing"))
    assert not tunnel.ipv6_available(str(tmp_path / "missing"), str(inet6))


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
    assert tunnel.first_hop("alice", "corp", ssh_g=ssh_g) == (["127.0.0.2"], True)
    assert asked == [("alice", "corp"), ("alice", "bastion")]


def test_first_hop_direct_and_nested_jumps():
    assert tunnel.first_hop("a", "h", ssh_g=lambda u, t: "hostname 127.0.0.3\nproxyjump none\n") \
        == (["127.0.0.3"], True)
    chain = {"a": "hostname x\nproxyjump b\n", "b": "hostname y\nproxyjump c\n",
             "c": "hostname 127.0.0.4\nproxyjump none\n"}
    assert tunnel.first_hop("u", "a", ssh_g=lambda u, t: chain[t]) == (["127.0.0.4"], True)


def test_first_hop_returns_every_address_of_either_family():
    def gai(answers):
        def getaddrinfo(host, port, family, kind):
            assert (host, family) == ("gw.example", socket.AF_UNSPEC)
            return [(socket.AF_INET6 if ":" in a else socket.AF_INET, kind, 6, "",
                     (a, port, 0, 0) if ":" in a else (a, port)) for a in answers]
        return getaddrinfo

    def ssh_g(u, t):
        return "hostname gw.example\nproxyjump none\n"
    assert tunnel.first_hop("u", "h", ssh_g, gai(["198.51.100.1"])) == (["198.51.100.1"], False)
    assert tunnel.first_hop("u", "h", ssh_g, gai(["2001:db8::1"])) == (["2001:db8::1"], False)
    assert tunnel.first_hop("u", "h", ssh_g, gai(["2001:db8::1", "198.51.100.1",
                                                  "2001:db8::1", "fe80::1%eth0"])) == ([
        "2001:db8::1", "198.51.100.1", "fe80::1"], False)


def test_a_first_hop_given_as_an_address_is_marked():
    # Only a name is looked up, and only a lookup can be spoofed (design §4.2).
    for hostname, addr in (("203.0.113.7", "203.0.113.7"), ("2001:db8::7", "2001:db8::7"),
                           ("fe80::7%eth0", "fe80::7")):
        out = f"hostname {hostname}\nproxyjump none\n"
        assert tunnel.first_hop("u", "h", lambda u, t, out=out: out) == ([addr], True)


def test_first_hop_gives_up_on_a_loop_and_on_option_like_hosts():
    import pytest
    with pytest.raises(ValueError, match="nested"):
        tunnel.first_hop("u", "a", ssh_g=lambda u, t: "hostname x\nproxyjump a\n")
    with pytest.raises(ValueError, match="unusable"):
        tunnel.first_hop("u", "a", ssh_g=lambda u, t: "hostname x\nproxyjump -oFoo=bar\n")


BASTION = {"corp": "hostname gw.corp.example\nproxyjump bastion\n",
           "bastion": "user alice\nhostname bastion.corp.example\nproxyjump none\n"}


def pinned(spec=SPEC, config=BASTION, resolves=lambda name: False):
    return tunnel.pinned_ssh_config(spec, lambda u, t: config[t], resolves,
                                    lambda u: SimpleNamespace(pw_dir="/home/alice"))


def test_a_first_hop_that_does_not_resolve_gets_its_address_from_connect():
    looked_up = []
    text = pinned(resolves=lambda name: looked_up.append(name))
    assert looked_up == ["bastion.corp.example"]
    assert ("Host bastion\n    HostName 203.0.113.7\n    HostKeyAlias bastion.corp.example\n"
            "\nMatch all\nInclude /home/alice/.ssh/config\nInclude /etc/ssh/ssh_config\n") in text


def test_no_pin_when_the_first_hop_resolves_or_cannot_be_shadowed():
    assert pinned(resolves=lambda name: True) is None
    assert pinned(dict(SPEC, dns="none")) is None
    assert pinned(config={"corp": "hostname 192.0.2.9\nproxyjump none\n"}) is None
    assert pinned(config={"corp": "hostname gw\nproxycommand nc %h %p\n"}) is None

    def ssh_g_fails(u, t):
        raise ValueError("ssh -G corp failed")
    assert tunnel.pinned_ssh_config(SPEC, ssh_g_fails, lambda name: False) is None
    assert pinned(config={"corp": "hostname a%h\nproxyjump none\n"}) is None


def test_a_first_hop_that_also_resolved_into_the_tunnel_is_pinned_anyway():
    # The guard rejects the refused addresses, so ssh must not follow its own
    # lookup (design §4.2); dns does not matter, and nothing is looked up.
    looked_up = []
    text = pinned(dict(SPEC, dns="none", pin=True), resolves=lambda name: looked_up.append(name))
    assert looked_up == []
    assert "also resolved into the tunnelled" in text
    assert "Host bastion\n    HostName 203.0.113.7\n    HostKeyAlias bastion.corp.example\n" in text
    assert pinned(dict(SPEC, pin=True, first_hop=[]), resolves=lambda name: True) is None
    assert pinned(dict(SPEC, pin=True), config={"corp": "hostname 192.0.2.9\n"}) is None


def test_a_pin_prefers_an_ipv4_address():
    text = pinned(dict(SPEC, first_hop=["2001:db8::7", "203.0.113.7"]))
    assert "    HostName 203.0.113.7\n" in text
    assert "    HostName 2001:db8::7\n" in pinned(dict(SPEC, first_hop=["2001:db8::7"]))


def test_a_pin_keeps_the_users_host_key_alias():
    text = pinned(config={"corp": "hostname gw.corp.example\nhostkeyalias gw-key\n"
                                  "proxyjump none\n"})
    assert "Host corp\n    HostName 203.0.113.7\n    HostKeyAlias gw-key\n" in text


def test_the_pinned_config_goes_to_ssh(tmp_path):
    path = tunnel.write_ssh_config("Host x\n", str(tmp_path / "ssh" / "ssh_config"))
    assert open(path).read() == "Host x\n"
    assert (tmp_path / "ssh" / "ssh_config").stat().st_mode & 0o777 == 0o644
    argv = tunnel.sshuttle_argv(SPEC, "sshuttle", path)
    assert argv[argv.index("-e") + 1].endswith(
        f"/nm-sshuttle ssh-as-user alice --remote corp -F {path}")


def sshuttle_runs(argv):
    """What sshuttle 1.3.1 and 2.0.0 run for -e and the stand-in -r, which has
    no user and no port (their ssh.connect)."""
    return shlex.split(argv[argv.index("-e") + 1]) + [argv[argv.index("-r") + 1], "--",
                                                      "/bin/sh -c 'exec python3'"]


# ssh's arguments as sshuttle 1.3.1 and 2.0.0 split these remotes (parse_hostport):
# the same user, host and port. The host goes on as written, as `check` gives it to
# ssh; sshuttle wrote an IPv6 address in short form and, with a port, in lower case.
@pytest.mark.parametrize("remote,ssh", [
    ("Corp:2222", ["-p", "2222", "Corp"]),
    ("2001:DB8:0::7", ["2001:DB8:0::7"]),
    ("corp", ["corp"]),
    ("alice@corp", ["alice@corp"]),
    ("corp:2222", ["-p", "2222", "corp"]),
    ("alice@gw.corp.example:022", ["-p", "22", "alice@gw.corp.example"]),
    ("2001:db8::7", ["2001:db8::7"]),
    ("alice@[2001:db8::7]:2222", ["-p", "2222", "alice@2001:db8::7"]),
    ("alice@[2001:db8::7]", ["alice@2001:db8::7"]),
    ("fe80::7%eth0", ["fe80::7%eth0"]),
])
def test_the_bridge_gives_ssh_the_remote_split_as_sshuttle_splits_it(remote, ssh):
    for config in (None, "/run/nm-sshuttle-ssh/ssh_config-0"):
        argv = tunnel.sshuttle_argv(dict(SPEC, remote=remote), "sshuttle", config)
        bridge = sshuttle_runs(argv)
        assert bridge[1:4] == ["ssh-as-user", "alice", "--remote"] and bridge[4] == remote
        assert tunnel.bridged_args(bridge[4], bridge[5:]) == (
            (["-F", config] if config else []) + ssh + ["--", "/bin/sh -c 'exec python3'"])


@pytest.mark.parametrize("args", [
    ["127.0.0.1"],
    ["-G", "127.0.0.1"],                          # a patched sshuttle's lookup (upstream §3)
    ["-p", "22", "127.0.0.1", "--", "x"],
    ["alice@127.0.0.1", "--", "x"],
    ["corp", "--", "x"],
    ["127.0.0.1", "x", "y"],
    ["127.0.0.1", "--", "x", "y"],
    ["-F", "/c", "-o", "ProxyCommand=x", "127.0.0.1", "--", "x"],
])
def test_the_bridge_refuses_other_arguments(args):
    with pytest.raises(ValueError, match="unexpected arguments"):
        tunnel.bridged_args("corp", args)


def test_the_bridge_checks_the_remote_again():
    with pytest.raises(profile.ProfileError):
        tunnel.bridged_args("-oProxyCommand=x", ["127.0.0.1", "--", "x"])


def test_ssh_as_user_execs_ssh_with_the_remote(monkeypatch):
    pw = SimpleNamespace(pw_uid=1000, pw_gid=1000, pw_dir="/home/alice")
    monkeypatch.setattr(tunnel.pwd, "getpwnam", lambda n: pw)
    monkeypatch.setattr(tunnel, "user_environment", lambda u, p: {"SSH_AUTH_SOCK": "/a"})
    monkeypatch.setattr(tunnel, "reset_oom_score_adj", lambda: None)
    ran = []
    monkeypatch.setattr(tunnel.os, "execvp", lambda f, argv: ran.append(argv))
    tunnel.ssh_as_user("alice", ["--remote", "corp:2222", "127.0.0.1", "--", "cmd"])
    assert ran[0][-5:] == ["-p", "2222", "corp", "--", "cmd"]
    assert ran[0][ran[0].index("ssh") + 1:][:len(tunnel.SSH_OPTIONS)] == tunnel.SSH_OPTIONS
    tunnel.ssh_as_user("alice", ["-G", "--", "corp"])      # ssh_g: passed on as before
    assert ran[1][-3:] == ["-G", "--", "corp"]
    with pytest.raises(ValueError):
        tunnel.ssh_as_user("alice", ["--remote", "corp", "corp", "--", "cmd"])
    assert len(ran) == 2


def test_ssh_as_user_resets_the_oom_score_before_anything_runs_as_the_user(monkeypatch):
    # The plugin unit's OOMScoreAdjust=-500 must not reach ssh -G's Match exec
    # or the tunnel's ssh (design §4.7).
    pw = SimpleNamespace(pw_uid=1000, pw_gid=1000, pw_dir="/home/alice")
    monkeypatch.setattr(tunnel.pwd, "getpwnam", lambda n: pw)
    order = []
    monkeypatch.setattr(tunnel, "reset_oom_score_adj", lambda: order.append("oom"))
    monkeypatch.setattr(tunnel, "user_environment", lambda u, p: order.append("env") or {})
    monkeypatch.setattr(tunnel.os, "execvp", lambda f, argv: order.append(argv[0]))
    tunnel.ssh_as_user("alice", ["-G", "--", "corp"])
    assert order == ["oom", "env", "setpriv"]
    order.clear()
    with pytest.raises(ValueError):           # refused arguments: nothing runs at all
        tunnel.ssh_as_user("alice", ["--remote", "corp", "corp", "--", "cmd"])
    assert order == []


def test_reset_oom_score_adj(tmp_path, capsys):
    adj = tmp_path / "oom_score_adj"
    adj.write_text("-500\n")
    tunnel.reset_oom_score_adj(str(adj))
    assert adj.read_text() == "0"
    tunnel.reset_oom_score_adj(str(tmp_path / "missing" / "oom_score_adj"))   # warns only
    assert "cannot reset the OOM score adjustment" in capsys.readouterr().err


OOM_FLOOR_CHECK = """
import ctypes, os, sys
from nm_sshuttle import tunnel

def write(value):
    try:
        with open(tunnel.OOM_SCORE_ADJ, "w") as f:
            f.write(value)
    except PermissionError:
        return False
    return True

if not write("-500"):
    print("no CAP_SYS_RESOURCE")
else:
    if sys.argv[1] == "reset":
        tunnel.reset_oom_score_adj()
    os.setgroups([])
    os.setresgid(65534, 65534, 65534)
    os.setresuid(65534, 65534, 65534)
    # The uid change made the process non-dumpable, which gives /proc/self to
    # root; exec would undo that, as for ssh. PR_SET_DUMPABLE is 4.
    ctypes.CDLL(None, use_errno=True).prctl(4, 1, 0, 0, 0)
    with open(tunnel.OOM_SCORE_ADJ) as f:
        now = f.read().strip()
    print(now, "writable" if write(now) else "not writable",
          "allowed" if write("-500") else "refused")
"""


@pytest.mark.skipif(os.geteuid() != 0 or not os.path.exists(tunnel.OOM_SCORE_ADJ),
                    reason="needs root and /proc")
def test_reset_oom_score_adj_also_resets_the_floor():
    # As systemd does for OOMScoreAdjust=-500: root writes it, which also sets
    # the floor that an unprivileged process may go back down to.
    root = os.path.dirname(os.path.dirname(os.path.abspath(tunnel.__file__)))

    def check(mode):
        p = subprocess.run([sys.executable, "-c", OOM_FLOOR_CHECK, mode], capture_output=True,
                           text=True, cwd=root, timeout=30)
        assert p.returncode == 0, p.stderr
        if p.stdout.strip() == "no CAP_SYS_RESOURCE":
            pytest.skip(p.stdout.strip())
        return p.stdout.strip()
    assert check("keep") == "-500 writable allowed"      # the floor without the reset
    assert check("reset") == "0 writable refused"


def test_resolves():
    assert tunnel.resolves("a", getaddrinfo=lambda *a: [("x",)])
    assert not tunnel.resolves("a", getaddrinfo=lambda *a: [])

    def fails(*a):
        raise socket.gaierror(-3, "Temporary failure in name resolution")
    assert not tunnel.resolves("a", getaddrinfo=fails)

    def hangs(*a):
        time.sleep(1)
        return [("x",)]
    assert not tunnel.resolves("a", timeout=0.05, getaddrinfo=hangs)


def test_classify_failure():
    assert tunnel.classify_failure("alice@corp: Permission denied (publickey).") == "login"
    assert tunnel.classify_failure("Host key verification failed.") == "login"
    assert tunnel.classify_failure("sign_and_send_pubkey: agent refused operation") == "login"
    assert tunnel.classify_failure("ssh: connect to host corp port 22: timed out") == "connect"


def test_port_failure_names_the_port_sshuttle_found_none_for():
    in_use = "OSError: [Errno 98] Address already in use\n"
    tcp = "c : TCP redirector listening on ('127.0.0.1', 225).\n"
    none_free = "exec-tunnel: no free redirect port among 225-240; trying 225\n"
    assert tunnel.port_failure("ssh: connect to host corp port 22: timed out", True) is None
    assert "DNS redirector" in tunnel.port_failure(tcp + in_use, False)
    assert "IPv6 redirector" in tunnel.port_failure(in_use, True)
    assert "redirect port (225-240) was taken" in tunnel.port_failure(in_use, False)
    assert "redirect port (225-240) was taken" in tunnel.port_failure(none_free + in_use, True)


HEAD = ("  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  "
        "timeout inode\n")


def tcp_line(local, state="0A", uid=0, inode=1):
    return (f"   0: {local} 00000000:0000 {state} 00000000:00000000 00:00000000 00000000 "
            f"{uid} 0 {inode}\n")


def fake_net(tmp_path, tcp, tcp6=""):
    (tmp_path / "net").mkdir()
    (tmp_path / "net" / "tcp").write_text(HEAD + tcp)
    (tmp_path / "net" / "tcp6").write_text(HEAD + tcp6)
    return str(tmp_path)


def test_listening_ports_are_roots_on_the_address_sshuttle_listens_on(tmp_path):
    lo6 = tunnel.socket.inet_pton(socket.AF_INET6, "::1")
    hex6 = "".join(f"{tunnel.struct.unpack('=I', lo6[i:i + 4])[0]:08X}" for i in range(0, 16, 4))
    proc = fake_net(tmp_path,
                    tcp_line("0100007F:3011")                    # 127.0.0.1:12305, root
                    + tcp_line("0100007F:300C", state="01")      # established
                    + tcp_line("0100007F:3012", uid=1000)        # another user's
                    + tcp_line("0200007F:3013")                  # 127.0.0.2
                    + tcp_line("00000000:3014"),                 # 0.0.0.0, an admin's --listen
                    tcp_line(f"{hex6}:300C")                     # [::1]:12300
                    + tcp_line(f"{hex6}:3015", uid=1000))
    assert tunnel.listening_ports(proc) == {4: {12305, 12308}, 6: {12300}}
    assert tunnel.listening_ports(str(tmp_path / "missing")) == {4: set(), 6: set()}


def test_stale_tables():
    ports = {4: {12305}, 6: {12300}}
    tables = ("table inet sshuttle-ipv4-12305\ntable inet sshuttle-ipv4-12300\n"
              "table inet nm-sshuttle-guard-0\ntable ip nat\ntable inet sshuttle-ipv6-12299\n"
              "table inet sshuttle-ipv6-12300\ntable inet sshuttle-ipv6-12305\n")
    assert tunnel.stale_tables(tables, ports) == ["sshuttle-ipv4-12300", "sshuttle-ipv6-12299",
                                                  "sshuttle-ipv6-12305"]
    assert tunnel.sshuttle_tables(tables, ports, live=True) == ["sshuttle-ipv4-12305",
                                                                "sshuttle-ipv6-12300"]


def run_sweep(monkeypatch, proc, tables):
    ran = []

    def run(argv, **kw):
        ran.append(argv)
        return subprocess.CompletedProcess(argv, 0, tables if argv[1] == "list" else "", "")
    monkeypatch.setattr(tunnel.subprocess, "run", run)
    monkeypatch.setattr(tunnel.shutil, "which", lambda *a, **k: None)
    tunnel.sweep(proc)
    return ran[1:]


def test_the_sweep_ignores_another_users_listener(tmp_path, monkeypatch, caplog):
    # a user listening on a leftover table's port must not keep the table
    caplog.set_level("INFO", "nm-sshuttle")
    proc = fake_net(tmp_path, tcp_line("0100007F:3011") + tcp_line("0100007F:300C", uid=1000)
                    + tcp_line("0200007F:3012"))
    ran = run_sweep(monkeypatch, proc, "table inet sshuttle-ipv4-12305\n"
                    "table inet sshuttle-ipv4-12300\ntable inet sshuttle-ipv4-12306\n")
    assert ran == [["nft", "delete", "table", "inet", "sshuttle-ipv4-12300"],
                   ["nft", "delete", "table", "inet", "sshuttle-ipv4-12306"]]
    assert "deleted table sshuttle-ipv4-12300 although uid 1000 listens on its port" in \
        caplog.text
    assert "deleted stale table sshuttle-ipv4-12306" in caplog.text


def fake_process(proc, pid, ppid, cmdline, uids=(0, 0, 0, 0), sockets=()):
    d = proc / str(pid)
    (d / "fd").mkdir(parents=True)
    (d / "stat").write_text(f"{pid} (x) S {ppid} " + "0 " * 40 + "\n")
    (d / "cmdline").write_bytes(b"\0".join(cmdline) + b"\0")
    (d / "status").write_text("Uid:\t" + "\t".join(map(str, uids)) + "\n")
    for n, inode in enumerate(sockets):
        os.symlink(f"socket:[{inode}]", d / "fd" / str(n + 3))


def test_the_sweep_spares_an_sshuttle_a_user_runs(tmp_path, monkeypatch):
    # the client listens as the user; its firewall helper runs as root via sudo
    proc = fake_net(tmp_path, tcp_line("0100007F:300C", uid=1000, inode=50)
                    + tcp_line("0100007F:3011", uid=1001, inode=60)
                    + tcp_line("0100007F:3012", uid=1002, inode=70))
    root = tmp_path
    user = (1000,) * 4
    fake_process(root, 100, 1, [b"/usr/bin/python3", b"/usr/bin/sshuttle", b"-r", b"h", b"0/0"],
                 user, [50])
    fake_process(root, 101, 100, [b"sudo", b"-p", b"x", b"/usr/bin/sshuttle", b"--firewall"])
    fake_process(root, 102, 101, [b"/usr/bin/python3", b"/usr/bin/sshuttle", b"-v",
                                  b"--method", b"nft", b"--firewall"])
    # 1001 has sudo wait for a password below its listener: euid 0, real ID 1001
    fake_process(root, 200, 1, [b"camp"], (1001,) * 4, [60])
    fake_process(root, 201, 200, [b"sudo", b"-S", b"/usr/bin/sshuttle", b"--firewall"],
                 (1001, 0, 0, 0))
    # 1002's listener has no helper below it, though root runs one elsewhere
    fake_process(root, 300, 1, [b"camp"], (1002,) * 4, [70])
    fake_process(root, 400, 1, [b"/usr/bin/python3", b"/usr/bin/sshuttle", b"--firewall"])
    ran = run_sweep(monkeypatch, proc, "table inet sshuttle-ipv4-12300\n"
                    "table inet sshuttle-ipv4-12305\ntable inet sshuttle-ipv4-12306\n")
    assert ran == [["nft", "delete", "table", "inet", "sshuttle-ipv4-12305"],
                   ["nft", "delete", "table", "inet", "sshuttle-ipv4-12306"]]
    assert tunnel.user_sshuttle_ports(
        [(4, 12300, 1000, "50"), (4, 12305, 1001, "60")], proc) == {4: {12300}, 6: set()}


# -------------------------------------------------------------- /etc/hosts
def sshuttle_hosts_line(ip, name, port):
    """A line as sshuttle's firewall helper writes it (rewrite_etc_hosts)."""
    return "%-30s %s\n" % (f"{ip} {name}", f"# sshuttle-firewall-{port} AUTOCREATED")


def fake_hosts(tmp_path, text, mode=0o644):
    (tmp_path / "etc").mkdir()
    hosts = tmp_path / "etc" / "hosts"
    hosts.write_text(text)
    hosts.chmod(mode)
    return hosts


def test_strip_etc_hosts_removes_what_a_stopped_sshuttle_left(tmp_path, caplog):
    caplog.set_level("INFO", "nm-sshuttle")
    # root listens on 226, so that sshuttle still runs; 1000 on 12300, without
    # sshuttle's helper below it, which does not count (design §4.8)
    proc = fake_net(tmp_path, tcp_line("0100007F:00E2") + tcp_line("0100007F:300C", uid=1000))
    localhost = "127.0.0.1   localhost\n"
    not_sshuttles = ("# 203.0.113.6 x # sshuttle-firewall-225 AUTOCREATED\n"
                     "192.0.2.1 by-hand  # sshuttle-firewall-225 AUTOCREATED, kept\n")
    live = sshuttle_hosts_line("10.0.0.1", "kept.example", 226)
    hosts = fake_hosts(tmp_path, localhost + sshuttle_hosts_line("203.0.113.6", "pypi.org", 225)
                       + not_sshuttles + live
                       + sshuttle_hosts_line("203.0.113.7", "git.corp.example", 12300), 0o640)
    assert tunnel.strip_etc_hosts(str(hosts), proc)
    assert hosts.read_text() == localhost + not_sshuttles + live
    assert oct(hosts.stat().st_mode & 0o7777) == oct(0o640)
    assert os.listdir(hosts.parent) == ["hosts"]          # no temporary file left
    assert "removed from %s what sshuttle left there: 203.0.113.6 pypi.org" % hosts in caplog.text
    assert "203.0.113.7 git.corp.example" in caplog.text


def test_strip_etc_hosts_keeps_the_lines_of_an_sshuttle_a_user_runs(tmp_path):
    proc = fake_net(tmp_path, tcp_line("0100007F:300C", uid=1000, inode=50))
    fake_process(tmp_path, 100, 1, [b"/usr/bin/sshuttle", b"-r", b"h", b"0/0"], (1000,) * 4,
                 [50])
    fake_process(tmp_path, 101, 100, [b"sudo", b"/usr/bin/sshuttle", b"--firewall"])
    assert tunnel.live_sshuttle_ports({12300, 225}, proc) == {12300}
    text = sshuttle_hosts_line("10.0.0.1", "auto.example", 12300)
    hosts = fake_hosts(tmp_path, text)
    inode = hosts.stat().st_ino
    assert tunnel.strip_etc_hosts(str(hosts), proc)
    assert hosts.read_text() == text and hosts.stat().st_ino == inode    # not rewritten


def test_strip_etc_hosts_follows_no_symlink(tmp_path):
    proc = fake_net(tmp_path, "")
    text = sshuttle_hosts_line("203.0.113.6", "pypi.org", 225)
    (tmp_path / "real").write_text(text)
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc" / "hosts").symlink_to(tmp_path / "real")
    assert tunnel.strip_etc_hosts(str(tmp_path / "etc" / "hosts"), proc)
    assert (tmp_path / "etc" / "hosts").is_symlink() and (tmp_path / "real").read_text() == text
    assert tunnel.strip_etc_hosts(str(tmp_path / "etc" / "missing"), proc)


def test_strip_etc_hosts_keeps_the_selinux_label(tmp_path, monkeypatch):
    proc = fake_net(tmp_path, "")
    text = sshuttle_hosts_line("203.0.113.6", "pypi.org", 225)
    hosts = fake_hosts(tmp_path, text)
    label = b"system_u:object_r:net_conf_t:s0\0"
    labelled = []
    monkeypatch.setattr(tunnel.os, "getxattr", lambda fd, name: label)
    monkeypatch.setattr(tunnel.os, "setxattr",
                        lambda fd, name, value: labelled.append((name, value)))
    assert tunnel.strip_etc_hosts(str(hosts), proc)
    assert labelled == [("security.selinux", label)] and hosts.read_text() == ""

    def refused(fd, name, value):
        raise PermissionError(13, "Permission denied")
    hosts.write_text(text)
    monkeypatch.setattr(tunnel.os, "setxattr", refused)
    assert not tunnel.strip_etc_hosts(str(hosts), proc)      # never with another label
    assert hosts.read_text() == text and os.listdir(hosts.parent) == ["hosts"]


def test_strip_etc_hosts_leaves_a_file_that_changed_meanwhile(tmp_path, monkeypatch, caplog):
    proc = fake_net(tmp_path, "")
    hosts = fake_hosts(tmp_path, sshuttle_hosts_line("203.0.113.6", "pypi.org", 225))
    mkstemp = tunnel.tempfile.mkstemp

    def racing(**kw):
        with open(hosts, "a") as f:
            f.write("192.0.2.9 new\n")
        return mkstemp(**kw)
    monkeypatch.setattr(tunnel.tempfile, "mkstemp", racing)
    assert not tunnel.strip_etc_hosts(str(hosts), proc)
    assert hosts.read_text().endswith("192.0.2.9 new\n") and "pypi.org" in hosts.read_text()
    assert os.listdir(hosts.parent) == ["hosts"]
    assert "it changed meanwhile" in caplog.text


def test_nft_health(monkeypatch):
    out = "table inet sshuttle-ipv4-12305\ntable inet nm-sshuttle-guard-0\n"
    monkeypatch.setattr(tunnel.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, out, ""))
    assert tunnel.nft_health("nft", "nm-sshuttle-guard-0", {4: {12305}, 6: set()}) == \
        {"guard": True, "sshuttle": {4}}
    assert tunnel.nft_health("nft", "nm-sshuttle-guard-0", {4: set(), 6: set()}) == \
        {"guard": True, "sshuttle": set()}
    # the unit's cgroup was not found: no listener counts
    assert tunnel.nft_health("nft", "nm-sshuttle-guard-0", {}) == \
        {"guard": True, "sshuttle": set()}
    out = ("table inet sshuttle-ipv4-225\ntable inet sshuttle-ipv6-12300\n"
           "table inet sshuttle-ipv6-12299\n")
    assert tunnel.nft_health("nft", "nm-sshuttle-guard-0", {4: {225}, 6: {12300}}) == \
        {"guard": False, "sshuttle": {4, 6}}
    assert tunnel.nft_health("nft", "g", {4: {12300}, 6: {225}}) == {"guard": False,
                                                                       "sshuttle": set()}
    monkeypatch.setattr(tunnel.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "denied"))
    assert tunnel.nft_health("nft", "nm-sshuttle-guard-0", {4: {12305}}) is None


# -------------------------------------------------------------- redirect port
def test_each_slot_tries_its_own_privileged_ports_first():
    assert Slot(0).listen_ports[:2] == [225, 226] and Slot(7).listen_ports[:2] == [239, 240]
    own = [p for n in range(SLOTS) for p in Slot(n).listen_ports[:2]]
    assert sorted(own) == list(LISTEN_PORTS) and max(own) < 1024
    # then the others, from those of the slots used least
    assert Slot(0).listen_ports[2:4] == [240, 239] and Slot(7).listen_ports[2] == 238
    assert all(sorted(Slot(n).listen_ports) == list(LISTEN_PORTS) for n in range(SLOTS))


def test_quick_restarts_take_another_slots_port():
    # two stops of slot 0 within a minute left both its own ports in TIME_WAIT
    taken = {225, 226}
    port = tunnel.listen_port(Slot(0).listen_ports, bindable=lambda p, k=0: p not in taken,
                              port_start=lambda: 1024)
    assert port == 240


def test_exec_tunnel_passes_the_slots_port(tmp_path, monkeypatch):
    monkeypatch.setattr(const, "STATE_DIR", str(tmp_path))
    os.makedirs(Slot(2).dir)
    with open(Slot(2).spec, "w") as f:
        json.dump(SPEC, f)
    asked, ran = [], []
    monkeypatch.setattr(tunnel.shutil, "which", lambda *a, **k: "/usr/bin/sshuttle")
    monkeypatch.setattr(tunnel, "pinned_ssh_config", lambda spec: None)
    monkeypatch.setattr(tunnel, "listen_port",
                        lambda ports, udp=False: asked.append((ports, udp)) or ports[1])
    monkeypatch.setattr(tunnel.os, "execv", lambda path, argv: ran.append(argv))
    tunnel.exec_tunnel(2)
    assert asked == [(Slot(2).listen_ports, False)]
    assert ran[0][ran[0].index("--listen") + 1] == "127.0.0.1:230"


def test_sshuttle_argv_listens_on_the_slots_port():
    argv = tunnel.sshuttle_argv(SPEC, "sshuttle", port=227)
    assert argv[argv.index("--listen") + 1] == "127.0.0.1:227"
    # sshuttle fails when both families get a port: IPv6 keeps its own search
    argv = tunnel.sshuttle_argv(dict(SPEC, ipv6=True), "sshuttle", port=227)
    assert argv[argv.index("--listen") + 1] == "127.0.0.1:227,[::1]:0"
    assert "--listen" not in tunnel.sshuttle_argv(SPEC, "sshuttle")


def test_listen_port_skips_a_port_in_use(capsys):
    taken = {(225, socket.SOCK_STREAM)}

    def bindable(port, kind=socket.SOCK_STREAM):
        return (port, kind) not in taken
    assert tunnel.listen_port([225, 226], bindable=bindable, port_start=lambda: 1024) == 226
    taken.add((226, socket.SOCK_DGRAM))
    # tproxy binds UDP on it too; with neither free, sshuttle gets the first and fails
    assert tunnel.listen_port([225, 226], True, bindable, lambda: 1024) == 225
    assert "no free redirect port among 225-226" in capsys.readouterr().err
    assert tunnel.listen_port([225, 226], bindable=lambda p, k=0: True,
                              port_start=lambda: 0) == 225
    assert "ip_unprivileged_port_start is 0" in capsys.readouterr().err


def test_bindable_sees_a_port_in_time_wait():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.listen(1)
    assert not tunnel.bindable(port)
    client = socket.create_connection(("127.0.0.1", port))
    conn, _ = listener.accept()
    conn.close()                  # the listener's side closes first: TIME_WAIT
    listener.close()
    time.sleep(0.05)
    client.close()
    assert not tunnel.bindable(port)


def test_resolv_conf_must_point_at_resolveds_stub(tmp_path):
    f = tmp_path / "resolv.conf"
    f.write_text("# stub\nnameserver 127.0.0.53\noptions edns0 trust-ad\nsearch .\n")
    assert tunnel.resolv_conf_problem(str(f)) is None
    f.write_text("nameserver 127.0.0.54\n")
    assert tunnel.resolv_conf_problem(str(f)) is None
    # resolved's own list of the real servers (/run/systemd/resolve/resolv.conf)
    f.write_text("nameserver 10.1.0.53\nnameserver 192.168.1.1\nsearch corp.example\n")
    assert tunnel.resolv_conf_problem(str(f)) == \
        f"{f} lists 10.1.0.53, 192.168.1.1, not only resolved's stub 127.0.0.53"
    f.write_text("search corp.example\n")       # glibc then asks 127.0.0.1
    assert tunnel.resolv_conf_problem(str(f)) == f"{f} lists no nameserver"
    assert "cannot read" in tunnel.resolv_conf_problem(str(tmp_path / "missing"))


def test_unprivileged_port_start(tmp_path):
    f = tmp_path / "start"
    f.write_text("1024\n")
    assert tunnel.unprivileged_port_start(str(f)) == 1024
    assert tunnel.unprivileged_port_start(str(tmp_path / "missing")) is None
