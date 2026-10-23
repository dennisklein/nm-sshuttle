# SPDX-License-Identifier: MIT
"""Several tunnels (docs/later/multiple-tunnels.md): slots, the claims and
their conflict rules, the dispatcher's choice of slot, a tunnel unit's own
ports, the supervisor's claim, and cleanup per slot."""

import argparse
import json
import os
from types import SimpleNamespace

import pytest

from fakes import AC, NM, bring_up, fake_getpwnam, make, settings
from test_cli import Nmcli, conn, ssh_g_ok
from nm_sshuttle import claims, cli, const, dispatcher, tunnel
from nm_sshuttle.const import (AC_ACTIVATING, CONN_FULL, FAIL_CONNECT, ST_STOPPED, Slot,
                               slot_number)
from nm_sshuttle.profile import parse


# -------------------------------------------------------------------- slots
def test_a_slot_names_everything_an_activation_creates():
    s = Slot(3)
    assert (s.plugin_unit, s.tunnel_unit, s.link, s.guard_table) == (
        "nm-sshuttle@3.service", "nm-sshuttle-tunnel@3.service", "nmss3", "nm-sshuttle-guard-3")
    assert s.spec == os.path.join(const.STATE_DIR, "3", "tunnel.json")
    assert s.tproxy_mark == 0x4e530003
    # ssh reads it as the user, so it lives beside the single-tunnel one.
    assert s.ssh_config == os.path.join(os.path.dirname(const.SSH_CONFIG), "ssh_config-3")


@pytest.mark.parametrize("text", ["-1", "8", "x", ""])
def test_slot_number_refuses_what_is_not_0_to_7(text):
    with pytest.raises(ValueError):
        slot_number(text)


def test_the_spec_carries_the_slots_own_mark():
    p = parse(settings(method="tproxy"), fake_getpwnam)
    assert p.tunnel_spec(["203.0.113.7"], slot=5)["tmark"] == 0x4e530005


# ------------------------------------------------------------------- claims
def claim(name, networks=(), dns="split", domains=(), hops=()):
    return {"id": name, "uuid": "u-" + name, "pid": 1, "networks": list(networks),
            "dns": dns, "domains": list(domains), "first_hop": list(hops)}


def test_effective_networks_take_the_excludes_out():
    assert claims.effective_networks(["10.0.0.0/8"], ["10.0.0.0/9"]) == ["10.128.0.0/9"]
    assert claims.effective_networks(["10.1.0.0/16", "2001:db8::/48"], ["10.0.0.0/8"]) == [
        "2001:db8::/48"]
    assert claims.effective_networks(["10.1.0.0/16", "10.0.0.0/8"], []) == ["10.0.0.0/8"]


def test_make_claim_from_a_profile():
    p = parse(settings(subnets="10.0.0.0/8", exclude="10.200.0.0/16",
                       **{"dns-domains": "~Corp.Example., lab.example"}), fake_getpwnam)
    c = claims.make_claim(p, ["203.0.113.7"], pid=42)
    assert c["id"] == "corp" and c["uuid"] == "u-1" and c["pid"] == 42
    assert "10.200.0.0/16" not in c["networks"] and "10.0.0.0/9" in c["networks"]
    assert c["domains"] == ["corp.example", "lab.example"]
    assert c["first_hop"] == ["203.0.113.7"]
    none = parse(settings(dns="none", **{"dns-servers": "", "dns-domains": ""}), fake_getpwnam)
    assert claims.make_claim(none, [])["domains"] == []


def test_overlapping_subnets_conflict():
    mine = claim("lab", ["10.50.1.0/24"])
    why = claims.conflict(mine, [claim("corp", ["10.50.0.0/16"])])
    assert why == "subnets overlap with the active profile corp: 10.50.1.0/24"
    assert claims.conflict(claim("lab", ["10.51.0.0/16"]), [claim("corp", ["10.50.0.0/16"])]) \
        is None


def test_an_exclude_that_carves_out_the_other_avoids_the_overlap():
    corp = claim("corp", claims.effective_networks(["10.0.0.0/8"], ["10.50.0.0/16"]))
    assert claims.conflict(claim("lab", ["10.50.0.0/16"]), [corp]) is None


def test_ipv4_and_ipv6_networks_never_overlap():
    assert claims.conflict(claim("a", ["2001:db8::/32"]), [claim("b", ["0.0.0.0/0"])]) is None
    assert claims.conflict(claim("a", ["2001:db8:1::/48"]), [claim("b", ["2001:db8::/32"])])


def test_two_dns_all_conflict():
    assert claims.conflict(claim("a", dns="all"), [claim("b", dns="all")]) == \
        "the active profile b also uses dns=all"
    assert claims.conflict(claim("a", dns="all"), [claim("b", dns="split")]) is None


def test_a_shared_dns_domain_conflicts():
    why = claims.conflict(claim("a", domains=["corp.example", "x.example"]),
                          [claim("b", domains=["corp.example"])])
    assert why == "the active profile b also has the DNS domain corp.example"


def test_a_first_hop_inside_another_tunnel_is_fine_but_a_loop_is_not():
    corp = claim("corp", ["10.0.0.0/8"], hops=["203.0.113.7"])
    lab = claim("lab", ["192.168.50.0/24"], hops=["10.1.2.3"])     # ssh to lab runs over corp
    assert claims.conflict(lab, [corp]) is None
    looped = claim("corp", ["10.0.0.0/8"], hops=["192.168.50.9"])  # and corp's over lab
    assert claims.conflict(lab, [looped]) == \
        "the first hops would run in a loop: lab -> corp -> lab"


def test_a_three_way_loop_is_found():
    a = claim("a", ["10.1.0.0/16"], hops=["10.2.0.1"])
    b = claim("b", ["10.2.0.0/16"], hops=["10.3.0.1"])
    c = claim("c", ["10.3.0.0/16"], hops=["10.1.0.1"])
    assert claims.first_hop_loop(a, [b, c]) == ["a", "b", "c", "a"]
    assert claims.first_hop_loop(a, [b]) is None


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(const, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(claims, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(claims, "CLAIMS_LOCK", str(tmp_path / "claims.lock"))
    return tmp_path


def test_take_writes_the_claim_and_refuses_a_conflicting_one(state_dir):
    def alive(pid, start):
        return pid in {1, 2}
    assert claims.take(0, dict(claim("corp", ["10.0.0.0/8"]), pid=1), alive) is None
    path = state_dir / "0" / "claim.json"
    assert json.loads(path.read_text())["id"] == "corp"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    why = claims.take(1, dict(claim("lab", ["10.1.0.0/16"]), pid=2), alive)
    assert why.startswith("subnets overlap with the active profile corp")
    assert not (state_dir / "1" / "claim.json").exists()


def test_a_dead_instances_claim_does_not_count(state_dir):
    claims.take(0, dict(claim("corp", ["10.0.0.0/8"]), pid=1), lambda pid, start: True)
    assert claims.take(1, dict(claim("lab", ["10.1.0.0/16"]), pid=2),
                       lambda pid, start: pid != 1) is None


def test_an_instance_may_retake_its_own_slot(state_dir):
    alive = lambda pid, start: True   # noqa: E731
    claims.take(2, dict(claim("corp", ["10.0.0.0/8"]), pid=1), alive)
    assert claims.take(2, dict(claim("corp", ["10.0.0.0/8"], hops=["1.2.3.4"]), pid=1),
                       alive) is None
    assert claims.read_claims(skip=None, alive=alive)[0]["first_hop"] == ["1.2.3.4"]


def test_release_removes_the_claim(state_dir):
    claims.take(0, claim("corp"), lambda pid, start: True)
    claims.release(0)
    claims.release(0)                 # twice is fine
    assert claims.read_claims(skip=None, alive=lambda pid, start: True) == []


def test_a_broken_claim_file_is_ignored(state_dir):
    (state_dir / "3").mkdir()
    (state_dir / "3" / "claim.json").write_text("{")
    assert claims.read_claims(skip=None, alive=lambda pid, start: True) == []


def test_alive_needs_a_plugin_process():
    assert not claims._alive(None)
    assert not claims._alive(os.getpid())     # pytest, not nm-sshuttle-service


def fake_proc(tmp_path, pid, cmdline, start, uids):
    """/proc/PID with a cmdline, user IDs and a stat whose command name has spaces."""
    d = tmp_path / "proc" / str(pid)
    d.mkdir(parents=True)
    (d / "cmdline").write_bytes(cmdline)
    fields = ["S"] + ["0"] * 18 + [str(start)] + ["0"] * 10
    (d / "stat").write_text(f"{pid} (python3 (x) y) " + " ".join(fields) + "\n")
    (d / "status").write_text("Name:\tx\nUid:\t" + "\t".join(map(str, uids)) + "\nGid:\t0\n")
    return str(tmp_path / "proc")


def test_alive_wants_roots_plugin_with_the_claimed_start_time(tmp_path):
    plugin = b"/usr/bin/python3\0/usr/libexec/nm-sshuttle-service\0--slot\0001\0"
    proc = fake_proc(tmp_path, 42, plugin, 777, (0, 0, 0, 0))
    assert claims.process_start(42, proc) == 777
    assert claims._alive(42, 777, proc)
    assert claims._alive(42, None, proc)          # a claim from before start times
    assert not claims._alive(42, 778, proc)       # the PID went to a later process
    # any user can name a process so: only root's counts
    other = fake_proc(tmp_path / "u", 43, b"sleep\0001d\0nm-sshuttle-service\0", 5, (1000,) * 4)
    assert not claims._alive(43, None, other) and not claims._alive(43, 5, other)
    # nor one with euid 0: sudo waiting for a password keeps the caller's real ID
    sudo = fake_proc(tmp_path / "s", 44, b"sudo\0-S\0nm-sshuttle-service\0", 5, (1000, 0, 0, 0))
    assert not claims._alive(44, None, sudo)
    assert claims.process_start(44, proc) is None


# --------------------------------------------------------------- dispatcher
def test_pick_slot_takes_the_lowest_free_one():
    states = {0: "active", 1: "inactive", 2: "failed"}
    assert dispatcher.pick_slot(states, {}, 100.0) == 1
    assert dispatcher.pick_slot({0: "activating", 1: "deactivating"}, {}, 100.0) is None


def test_pick_slot_skips_one_just_handed_out():
    # systemd may not show the unit as activating yet
    states = {n: "inactive" for n in range(8)}
    assert dispatcher.pick_slot(states, {0: 95.0}, 100.0) == 1
    assert dispatcher.pick_slot(states, {0: 89.0}, 100.0) == 0


@pytest.mark.parametrize("name, ok", [
    ("org.freedesktop.NetworkManager.sshuttle", True),
    ("org.freedesktop.NetworkManager.sshuttle.Connection_12", True),
    ("org.freedesktop.NetworkManager.sshuttle.Dispatcher", False),
    ("org.freedesktop.NetworkManager.openvpn.Connection_1", False),
    ("org.freedesktop.NetworkManager.sshuttle.Connection_1\n", False),
])
def test_the_dispatcher_starts_only_nm_sshuttle_names(name, ok):
    assert bool(dispatcher.NAME_RE.fullmatch(name)) is ok


class FakeDispatcher(dispatcher.Dispatcher):
    def __init__(self, states, uid=0):
        super().__init__(None, None)
        self.states, self.uid, self.started = states, uid, []

    def caller_uid(self, sender):
        return self.uid

    def unit_state(self, unit):
        return self.states.get(unit, "inactive")

    def _call(self, dest, path, iface, method, args, rtype):
        assert method == "StartUnit"
        self.started.append(args.unpack()[0])


def test_start_writes_the_name_and_starts_the_slots_unit(state_dir):
    d = FakeDispatcher({"nm-sshuttle@0.service": "active"})
    name = "org.freedesktop.NetworkManager.sshuttle.Connection_3"
    assert d.start(":1.9", name) == 1
    assert d.started == ["nm-sshuttle@1.service"]
    assert (state_dir / "1" / "bus-name").read_text() == name + "\n"
    assert d.start(":1.9", name) == 2          # slot 1 is still starting


def test_start_refuses_a_non_root_caller_and_a_full_house(state_dir):
    with pytest.raises(PermissionError):
        FakeDispatcher({}, uid=1000).start(":1.9", "org.freedesktop.NetworkManager.sshuttle")
    full = FakeDispatcher({f"nm-sshuttle@{n}.service": "active" for n in range(8)})
    with pytest.raises(RuntimeError, match="all 8 slots"):
        full.start(":1.9", "org.freedesktop.NetworkManager.sshuttle")


def test_the_plugin_reads_its_bus_name(state_dir):
    from nm_sshuttle import service
    dispatcher.write_bus_name(4, "org.freedesktop.NetworkManager.sshuttle.Connection_7")
    assert service.read_bus_name(4) == "org.freedesktop.NetworkManager.sshuttle.Connection_7"


# --------------------------------------------------------- one tunnel's ports
def proc_tree(tmp_path, unit, pids):
    """A fake cgroup and /proc: pid 10 holds socket 111 (port 0x3039), pid 20
    socket 222 (port 0x303a), both root's, and socket 333 (port 0x303b) of
    uid 1000, as a user's ssh with a LocalForward would; all listen."""
    cg = tmp_path / "cg"
    slice_dir = cg / "system.slice" / "system-nm\\x2dsshuttle\\x2dtunnel.slice"
    if pids is not None:
        slice_dir.mkdir(parents=True)
    else:
        (cg / "system.slice").mkdir(parents=True)     # an empty slice goes too
    if pids is not None:
        (slice_dir / unit).mkdir()
        (slice_dir / unit / "cgroup.procs").write_text("".join(f"{p}\n" for p in pids))
    proc = tmp_path / "proc"
    (proc / "net").mkdir(parents=True)
    head = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  " \
           "timeout inode\n"
    (proc / "net" / "tcp").write_text(
        head
        + "   0: 0100007F:3039 00000000:0000 0A 00000000:00000000 00:00000000 00000000 0 0 111\n"
        + "   1: 0100007F:303A 00000000:0000 0A 00000000:00000000 00:00000000 00000000 0 0 222\n"
        + "   2: 0100007F:303B 00000000:0000 0A 00000000:00000000 00:00000000 00000000 "
          "1000 0 333\n")
    (proc / "net" / "tcp6").write_text(head)
    for pid, inode in ((10, 111), (20, 222), (30, 333)):
        (proc / str(pid) / "fd").mkdir(parents=True)
        os.symlink(f"socket:[{inode}]", proc / str(pid) / "fd" / "3")
        os.symlink("/dev/null", proc / str(pid) / "fd" / "0")
    return (str(cg),), str(proc)


def test_unit_ports_are_only_the_units_own(tmp_path):
    unit = "nm-sshuttle-tunnel@1.service"
    cg, proc = proc_tree(tmp_path, unit, [10, 30])
    assert tunnel.unit_ports(unit, cg, proc) == {4: {0x3039}, 6: set()}


def test_unit_ports_of_a_stopped_unit_and_an_unknown_layout(tmp_path):
    unit = "nm-sshuttle-tunnel@1.service"
    cg, proc = proc_tree(tmp_path, unit, None)
    assert tunnel.unit_ports(unit, cg, proc) == {4: set(), 6: set()}
    # no hierarchy with systemd's units: the caller gives up (design §4.8)
    assert tunnel.unit_ports(unit, (str(tmp_path / "elsewhere"),), proc) is None


def test_unit_runs_finds_a_command_among_the_units_processes(tmp_path):
    unit = "nm-sshuttle-tunnel@1.service"
    cg, proc = proc_tree(tmp_path, unit, [10, 20])
    (tmp_path / "proc" / "10" / "comm").write_text("sshuttle\n")
    (tmp_path / "proc" / "20" / "comm").write_text("ssh\n")
    assert tunnel.unit_runs(unit, "ssh", cg, proc) is True
    cg, proc = proc_tree(tmp_path / "b", unit, [10])
    (tmp_path / "b" / "proc" / "10" / "comm").write_text("sshuttle\n")
    assert tunnel.unit_runs(unit, "ssh", cg, proc) is False   # sshuttle outlived its ssh
    cg, proc = proc_tree(tmp_path / "c", unit, None)
    assert tunnel.unit_runs(unit, "ssh", cg, proc) is False   # the unit stopped
    assert tunnel.unit_runs(unit, "ssh", (str(tmp_path / "elsewhere"),), proc) is None


def test_unit_cgroup_on_unified_hybrid_and_legacy_hosts(tmp_path):
    roots = tuple(str(tmp_path / r) for r in ("v2", "v2/unified", "v2/systemd"))
    assert tunnel.unit_cgroup("a.service", roots) is None
    for root in reversed(roots):         # legacy (name=systemd), hybrid, then cgroup v2
        os.makedirs(os.path.join(root, "system.slice"))
        assert tunnel.unit_cgroup("a.service", roots) == f"{root}/system.slice/a.service"
    assert tunnel.unit_cgroup("nm-sshuttle-tunnel@0.service", roots) == (
        f"{roots[0]}/system.slice/system-nm\\x2dsshuttle\\x2dtunnel.slice/"
        "nm-sshuttle-tunnel@0.service")


# --------------------------------------------------------------- supervisor
def test_the_supervisor_names_everything_after_its_slot():
    fx, sup = make()
    fx.slot = Slot(2)
    from nm_sshuttle.supervisor import Supervisor
    from fakes import parse as fparse
    sup = Supervisor(fx, parse_profile=fparse)
    sup.nm_current_owner = NM
    bring_up(fx, sup, method="tproxy")
    cfg = [e[1] for e in fx.events if e[0] == "config"][0]
    assert cfg["tundev"] == "nmss2"
    assert fx.guard_mark == 0x4e530002
    assert [e[1] for e in fx.events if e[0] == "spec"][0]["tmark"] == 0x4e530002


def test_the_claim_is_taken_before_anything_is_created():
    fx, sup = make()
    bring_up(fx, sup)
    assert len(fx.claims) == 1
    c = fx.claims[0]
    assert c["id"] == "corp" and c["first_hop"] == ["203.0.113.7"]
    assert c["domains"] == ["corp.example"]


def test_a_conflict_fails_the_activation_untouched():
    fx, sup = make()
    fx.conflict = "subnets overlap with the active profile lab: 10.50.0.0/16"
    sup.connect(settings(), NM)
    fx.first_hop_cbs.pop(0)(["203.0.113.7"], None)
    names = fx.names()
    assert "create_link" not in names and "install_guard" not in names
    assert ("failure", FAIL_CONNECT) in fx.events
    assert ("state", ST_STOPPED) in fx.events


def test_a_claim_error_is_a_failure():
    fx, sup = make()
    fx.take_claim = lambda c: (_ for _ in ()).throw(OSError("read-only"))
    sup.connect(settings(), NM)
    fx.first_hop_cbs.pop(0)(["203.0.113.7"], None)
    assert ("failure", FAIL_CONNECT) in fx.events and "create_link" not in fx.names()


def test_the_claim_is_released_once_stopped():
    fx, sup = make()
    bring_up(fx, sup)
    sup.disconnect()
    assert fx.released == 1
    fx.advance(120)
    assert fx.released == 1


def test_no_release_without_a_claim():
    fx, sup = make()
    fx.conflict = "x"
    sup.connect(settings(), NM)
    fx.first_hop_cbs.pop(0)(["203.0.113.7"], None)
    assert fx.released == 0


def test_a_new_first_hop_is_claimed_again_and_a_loop_gives_up():
    fx, sup = make()
    sup.on_network(True, CONN_FULL, ("wifi-1", ("192.168.1.20/24",), "192.168.1.1"))
    bring_up(fx, sup)
    sup.on_network(True, CONN_FULL, ("wifi-2", ("10.20.0.7/16",), "10.20.0.1"))
    sup.on_ac_state(AC, AC_ACTIVATING, 6)
    fx.advance(0)
    fx.finish_stops()
    fx.conflict = "the first hops would run in a loop: corp -> lab -> corp"
    fx.clear()
    fx.first_hop_cbs.pop(0)(["10.9.9.9"], None)
    assert "install_guard" not in fx.names() and "start_tunnel" not in fx.names()
    assert ("failure", FAIL_CONNECT) in fx.events


def test_a_new_first_hop_that_fits_is_claimed():
    fx, sup = make()
    sup.on_network(True, CONN_FULL, ("wifi-1", ("192.168.1.20/24",), "192.168.1.1"))
    bring_up(fx, sup)
    sup.on_network(True, CONN_FULL, ("wifi-2", ("10.20.0.7/16",), "10.20.0.1"))
    sup.on_ac_state(AC, AC_ACTIVATING, 6)
    fx.advance(0)
    fx.finish_stops()
    fx.first_hop_cbs.pop(0)(["198.51.100.200"], None)
    assert [c["first_hop"] for c in fx.claims] == [["203.0.113.7"], ["198.51.100.200"]]
    assert "start_tunnel" in fx.names()


def test_one_activation_exits_after_the_teardown():
    fx, sup = make()
    sup.one_activation = True
    bring_up(fx, sup)
    sup.disconnect()
    fx.advance(5)
    assert fx.quit_called


def test_the_fixed_name_stays_for_the_idle_window():
    fx, sup = make()
    bring_up(fx, sup)
    sup.disconnect()
    fx.advance(5)
    assert not fx.quit_called


def gap_of(fx, sup):
    sup.on_tunnel_inactive()
    sup.on_ac_state(AC, AC_ACTIVATING, 6)
    assert sup.phase == "gap"


def test_another_vpn_activated_in_a_gap_kicks_a_reconnect():
    fx, sup = make()
    bring_up(fx, sup)
    other = "/org/freedesktop/NetworkManager/ActiveConnection/8"
    sup.on_vpns([AC])                 # the first read only learns what is there
    gap_of(fx, sup)
    fx.advance(1.0)
    fx.finish_start(False, "connection refused")   # attempt 1 fails; backoff follows
    fx.clear()
    sup.on_vpns([AC, other])
    fx.advance(0)
    fx.finish_stops()
    assert "start_tunnel" in fx.names()


def test_vpns_already_there_or_this_one_kick_nothing():
    fx, sup = make()
    bring_up(fx, sup)
    other = "/org/freedesktop/NetworkManager/ActiveConnection/8"
    sup.on_vpns([AC, other])
    gap_of(fx, sup)
    fx.advance(1.0)
    fx.finish_start(False, "connection refused")
    fx.clear()
    sup.on_vpns([AC, other])
    fx.advance(0)
    fx.finish_stops()
    assert "start_tunnel" not in fx.names()


# ---------------------------------------------------------------------- cli
@pytest.fixture
def host(monkeypatch, tmp_path):
    """Record what cleanup does; plugin units in `running` are active."""
    monkeypatch.setattr(const, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(claims, "STATE_DIR", str(tmp_path))
    rec = SimpleNamespace(stopped=[], removed=[], routing=[], running=set(), swept=0)

    def systemctl(*args, timeout=30):
        if args[0] == "stop":
            rec.stopped.append(args[1])
        if args[:2] == ("is-active", "--quiet"):
            return SimpleNamespace(returncode=0 if args[2] in rec.running else 3)
        return SimpleNamespace(returncode=0, stderr="")
    monkeypatch.setattr(cli, "systemctl", systemctl)
    monkeypatch.setattr(cli.guard, "link_ifindex", lambda name: 5)
    monkeypatch.setattr(cli.guard, "remove_link", lambda name: rec.removed.append(name) or True)
    monkeypatch.setattr(cli.guard, "guard_present", lambda table: True)
    monkeypatch.setattr(cli.guard, "remove_guard", lambda table: rec.removed.append(table) or True)
    monkeypatch.setattr(cli.guard, "remove_policy_routing",
                        lambda slot: rec.routing.append(slot) or True)

    def sweep():
        rec.swept += 1
    monkeypatch.setattr(cli.tunnel, "sweep", sweep)
    return rec


def test_cleanup_of_one_slot_touches_only_that_slot(host, tmp_path):
    (tmp_path / "2").mkdir()
    (tmp_path / "2" / "tunnel.json").write_text("{}")
    (tmp_path / "2" / "claim.json").write_text("{}")
    assert cli.main(["cleanup", "--slot", "2"]) == 0
    assert host.stopped == ["nm-sshuttle-tunnel@2.service"]
    assert host.removed == ["nmss2", "nm-sshuttle-guard-2"]
    assert host.routing == [2] and host.swept == 1
    assert not (tmp_path / "2" / "tunnel.json").exists()
    assert not (tmp_path / "2" / "claim.json").exists()


def test_cleanup_all_skips_running_slots_and_takes_the_v1_names(host):
    host.running = {"nm-sshuttle@0.service", "nm-sshuttle@5.service"}
    assert cli.main(["cleanup", "--all"]) == 0
    assert "nm-sshuttle-tunnel@0.service" not in host.stopped
    assert "nm-sshuttle-tunnel@5.service" not in host.stopped
    assert "nm-sshuttle-tunnel@1.service" in host.stopped
    assert "nm-sshuttle-tunnel.service" in host.stopped       # a single-tunnel version's
    assert "nm-sshuttle-guard" in host.removed
    assert sorted(host.routing) == [1, 2, 3, 4, 6, 7]


def test_cleanup_all_leaves_a_running_v1_plugin_alone(host):
    host.running = {"nm-sshuttle.service"}
    cli.main(["cleanup"])
    assert "nm-sshuttle-tunnel.service" not in host.stopped
    assert "nm-sshuttle-guard" not in host.removed


def test_cleanup_refuses_a_bad_slot(host):
    with pytest.raises(SystemExit):
        cli.main(["cleanup", "--slot", "9"])
    with pytest.raises(SystemExit):
        cli.main(["cleanup", "--slot", "1", "--all"])


@pytest.fixture
def dennis(monkeypatch):
    pw = SimpleNamespace(pw_uid=1000, pw_gid=1000, pw_name="dennis")
    monkeypatch.setattr(cli.pwd, "getpwnam", lambda n: pw if n == "dennis" else 1 / 0)
    monkeypatch.setattr(cli.pwd, "getpwuid", lambda uid: pw)


def run_ok(argv):
    return SimpleNamespace(returncode=0)


def test_check_fails_on_a_conflict_with_an_active_profile(dennis, capsys):
    nmcli = Nmcli({"u1": conn(), "u2": conn("lab", **{
        "vpn.data": "remote = lab, local-user = dennis, subnets = 10.0.0.0/16"})},
        active={"u2"})
    assert cli.check(argparse.Namespace(name="corp"), nmcli, ssh_g_ok, run_ok) == 1
    out = capsys.readouterr().out
    assert "FAIL subnets overlap with the active profile lab: 10.0.0.0/16" in out


def test_check_ignores_the_profile_itself_and_others_that_fit(dennis, capsys):
    nmcli = Nmcli({"u1": conn(), "u2": conn("lab", **{
        "vpn.data": "remote = lab, local-user = dennis, subnets = 192.168.7.0/24"})},
        active={"u1", "u2"})
    assert cli.check(argparse.Namespace(name="corp"), nmcli, ssh_g_ok, run_ok) == 0
    assert "FAIL" not in capsys.readouterr().out


def test_list_marks_active_profiles():
    nmcli = Nmcli({"u1": conn(), "u2": conn("lab")}, active={"u2"})
    lines = []
    cli.list_profiles(nmcli, lines.append)
    assert lines[0].split("\t")[0] == "corp" and not lines[0].endswith("\tactive")
    assert lines[1].startswith("lab\t") and lines[1].endswith("\tactive")
