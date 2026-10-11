# SPDX-License-Identifier: MIT
"""Several tunnels (docs/later/multiple-tunnels.md): slots, the dispatcher's
choice of slot, a tunnel unit's own ports, the supervisor's names and its
exit after one activation, and cleanup per slot."""

import os
from types import SimpleNamespace

import pytest

from fakes import NM, bring_up, fake_getpwnam, make, settings
from nm_sshuttle import cli, const, dispatcher, tunnel
from nm_sshuttle.const import Slot, slot_number
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


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(const, "STATE_DIR", str(tmp_path))
    return tmp_path


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
    socket 222 (port 0x303a); both listen."""
    cg = tmp_path / "cg"
    slice_dir = cg / "system.slice" / "system-nm\\x2dsshuttle\\x2dtunnel.slice"
    slice_dir.mkdir(parents=True)
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
        + "   1: 0100007F:303A 00000000:0000 0A 00000000:00000000 00:00000000 00000000 0 0 222\n")
    (proc / "net" / "tcp6").write_text(head)
    for pid, inode in ((10, 111), (20, 222)):
        (proc / str(pid) / "fd").mkdir(parents=True)
        os.symlink(f"socket:[{inode}]", proc / str(pid) / "fd" / "3")
        os.symlink("/dev/null", proc / str(pid) / "fd" / "0")
    return str(cg), str(proc)


def test_unit_ports_are_only_the_units_own(tmp_path):
    unit = "nm-sshuttle-tunnel@1.service"
    cg, proc = proc_tree(tmp_path, unit, [10])
    assert tunnel.unit_ports(unit, cg, proc) == {0x3039}


def test_unit_ports_of_a_stopped_unit_and_an_unknown_layout(tmp_path):
    unit = "nm-sshuttle-tunnel@1.service"
    cg, proc = proc_tree(tmp_path, unit, None)
    assert tunnel.unit_ports(unit, cg, proc) == set()
    # no slice where expected: the caller falls back to every listener
    assert tunnel.unit_ports(unit, str(tmp_path / "elsewhere"), proc) is None


def test_unit_cgroup_of_a_plain_and_a_template_unit():
    assert tunnel.unit_cgroup("a.service", "/c") == "/c/system.slice/a.service"
    assert tunnel.unit_cgroup("nm-sshuttle-tunnel@0.service", "/c") == (
        "/c/system.slice/system-nm\\x2dsshuttle\\x2dtunnel.slice/nm-sshuttle-tunnel@0.service")


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


# ---------------------------------------------------------------------- cli
@pytest.fixture
def host(monkeypatch, tmp_path):
    """Record what cleanup does; plugin units in `running` are active."""
    monkeypatch.setattr(const, "STATE_DIR", str(tmp_path))
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
    assert cli.main(["cleanup", "--slot", "2"]) == 0
    assert host.stopped == ["nm-sshuttle-tunnel@2.service"]
    assert host.removed == ["nmss2", "nm-sshuttle-guard-2"]
    assert host.routing == [2] and host.swept == 1
    assert not (tmp_path / "2" / "tunnel.json").exists()


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


