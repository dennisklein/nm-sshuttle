# SPDX-License-Identifier: MIT
"""The lifecycle state machine against design §4.4, with fake events and clock."""

import logging

import pytest

from fakes import AC, DEVICE, NM, bring_up, drop_and_retry, make, settings
from nm_sshuttle.const import (AC_ACTIVATED, AC_ACTIVATING, AC_DEACTIVATED, AC_DEACTIVATING,
                               FAIL_CONNECT, FAIL_LOGIN, ST_STARTED, ST_STARTING, ST_STOPPED,
                               ST_STOPPING)

REAL_IP4 = {"address": "192.0.0.8", "prefix": 32, "never-default": True,
            "dns": ["10.1.0.53"], "domains": ["~corp.example"]}
SENTINEL_IP4 = dict(REAL_IP4, nbns=["192.0.0.10"])
CONFIG = {"gateway": "203.0.113.7", "tundev": "nmss0", "has-ip4": True, "has-ip6": False,
          "can-persist": True}


@pytest.fixture
def up():
    fx, sup = make()
    bring_up(fx, sup)
    fx.clear()
    return fx, sup


def gap(fx, sup):
    sup.on_tunnel_inactive()
    assert sup.phase == "gap"
    sup.on_ac_state(AC, AC_ACTIVATING, 6)    # NM's answer to STARTING (reason 6)
    fx.clear()


# ------------------------------------------------------------------ start
def test_startup_cleanup_runs_before_a_connect():
    fx, sup = make()
    sup.start()
    assert fx.names() == ["cleanup"] and sup.phase == "cleaning"
    sup.connect(settings(), NM)
    assert "create_link" not in fx.names()
    fx.cleanup_cb()
    assert sup.phase == "connecting"
    assert fx.names()[1:3] == ["state", "find_ac"]


def test_new_instance_after_a_kill_answers_disconnect_and_exits():
    # After a kill, NM's Disconnect D-Bus-activates a new instance (R4m).
    fx, sup = make()
    sup.start()
    sup.disconnect()
    fx.cleanup_cb()
    assert fx.signals() == []          # nothing to report: never activated here
    fx.advance(59)
    assert not fx.quit_called
    fx.advance(1)
    assert fx.quit_called


def test_sigterm_during_startup_cleanup_exits_once_it_is_done():
    fx, sup = make()
    sup.start()
    sup.on_term()
    assert not fx.quit_called
    fx.cleanup_cb()
    assert fx.quit_called


# --------------------------------------------------------------- connect
def test_first_connect_order_and_content():
    fx, sup = make()
    sup.connect(settings(exclude="10.0.5.0/24"), NM)
    fx.first_hop_cbs.pop(0)("203.0.113.7", None)
    assert fx.names() == ["state", "find_ac", "first_hop", "sweep", "create_link",
                          "install_guard", "spec", "start_tunnel"]
    assert fx.events[0] == ("state", ST_STARTING)
    assert fx.events[5] == ("install_guard", ["10.0.0.0/8"],
                            ["203.0.113.7", "10.0.5.0/24"])
    fx.clear()
    fx.finish_start(True)
    assert fx.events == [("find_device", "nmss0"), ("config", CONFIG), ("ip4", REAL_IP4),
                         ("state", ST_STARTED)]


def test_gateway_from_the_profile_skips_the_lookup():
    fx, sup = make()
    sup.connect(settings(gateway="198.51.100.9"), NM)
    assert "first_hop" not in fx.names()
    assert fx.events[-1] == ("start_tunnel",)


def test_config_waits_for_nm_device():
    fx, sup = make()
    fx.device_answer = None
    sup.connect(settings(gateway="198.51.100.9"), NM)
    fx.finish_start(True)
    fx.advance(0.5)
    assert "config" not in fx.names()
    fx.device_answer = DEVICE
    fx.advance(0.1)
    assert "config" in fx.names()
    assert fx.names().count("find_device") == 7      # at once, then every 100 ms


def test_no_device_within_10s_fails():
    fx, sup = make()
    fx.device_answer = None
    sup.connect(settings(gateway="198.51.100.9"), NM)
    fx.finish_start(True)
    fx.advance(10.1)
    assert ("failure", FAIL_CONNECT) in fx.events and "config" not in fx.names()


def test_leftover_link_device_is_not_taken_for_the_new_one():
    fx, sup = make()
    fx.link = 7                       # left by somebody
    answers = iter(["/dev/old", "/dev/old", "/dev/old", DEVICE])
    fx.device_answer = lambda: next(answers)
    sup.connect(settings(gateway="198.51.100.9"), NM)
    assert fx.link == 40
    fx.finish_start(True)
    fx.advance(0.3)
    assert sup.device_path == DEVICE


def test_invalid_profile_fails_without_touching_the_host():
    fx, sup = make()
    sup.connect(settings(remote="-oProxyCommand=evil"), NM)
    assert fx.signals() == [("state", ST_STARTING), ("failure", FAIL_CONNECT),
                            ("state", ST_STOPPED)]
    assert "create_link" not in fx.names()


def test_first_connect_auth_failure_is_login_failed():
    fx, sup = make()
    sup.connect(settings(gateway="198.51.100.9"), NM)
    fx.finish_start(False, "start job failed\nalice@corp: Permission denied (publickey).")
    assert ("failure", FAIL_LOGIN) in fx.events


def test_host_error_becomes_a_failure():
    fx, sup = make()
    fx.raise_on["create_link"] = RuntimeError("cannot create nmss0: no dummy module")
    sup.connect(settings(gateway="198.51.100.9"), NM)
    assert fx.signals()[-2:] == [("failure", FAIL_CONNECT), ("state", ST_STOPPED)]


def test_unexpected_exception_becomes_a_failure(up):
    fx, sup = up
    fx.raise_on["config"] = KeyError("boom")
    drop_and_retry(fx, sup)
    assert fx.signals()[-2:] == [("failure", FAIL_CONNECT), ("state", ST_STOPPED)]


# ------------------------------------------------------- unmanaged check
def test_unmanaged_check_after_first_activated(caplog):
    fx, sup = make()
    bring_up(fx, sup)
    assert fx.events[-1] == ("device_state", DEVICE)
    with caplog.at_level(logging.WARNING, logger="nm-sshuttle"):
        fx.device_state_cbs.pop(0)(10)
    assert sup.link_managed is False and "risk 11" not in caplog.text


def test_managed_link_is_logged(caplog):
    fx, sup = make()
    bring_up(fx, sup)
    with caplog.at_level(logging.WARNING, logger="nm-sshuttle"):
        fx.device_state_cbs.pop(0)(100)
    assert sup.link_managed is True and "risk 11" in caplog.text


def test_unmanaged_check_runs_once_per_activation(up):
    fx, sup = up
    drop_and_retry(fx, sup)
    sup.on_ac_state(AC, AC_ACTIVATING, 6)
    sup.on_ac_state(AC, AC_ACTIVATED, 0)
    assert "device_state" not in fx.names()


# ------------------------------------------------------------ disconnect
def test_disconnect_sends_stopped_at_once_then_tears_down(up):
    # A slow teardown must not deliver STOPPED to a connection activated right after.
    fx, sup = up
    sup.disconnect()
    assert fx.events[:3] == [("state", ST_STOPPING), ("state", ST_STOPPED), ("stop_tunnel",)]
    fx.advance(0.1)
    assert "remove_link" in fx.names() and "remove_guard" in fx.names()
    fx.finish_stops()
    assert sup.phase == "idle"
    assert fx.names().count("state") == 2
    sup.disconnect()
    assert fx.names().count("state") == 2


def test_switching_off_during_a_pending_reconnect_cancels_it(up):
    fx, sup = up
    gap(fx, sup)
    sup.on_ac_state(AC, AC_DEACTIVATING, 2)
    sup.disconnect()
    fx.advance(5)
    fx.finish_stops()
    assert "start_tunnel" not in fx.names()
    assert sup.phase == "idle"


def test_connect_after_stopped_starts_clean(up):
    fx, sup = up
    sup.disconnect()
    fx.advance(0.1)
    fx.finish_stops()
    sup.connect(settings(gateway="198.51.100.9"), NM)
    assert sup.activated_once is False and sup.ac_path is None
    fx.finish_start(True)
    assert sup.ifindex == 41 and sup.phase == "up"


def test_connect_during_teardown_waits_for_it(up):
    fx, sup = up
    sup.disconnect()
    sup.connect(settings(gateway="198.51.100.9"), NM)
    assert sup.phase == "stopping"
    fx.advance(0.1)
    fx.finish_stops()
    assert sup.phase == "connecting"


def test_idle_exit_timer():
    fx, sup = make()
    sup.start()
    fx.cleanup_cb()
    fx.advance(30)
    sup.connect(settings(gateway="198.51.100.9"), NM)   # cancels the exit timer
    fx.advance(100)
    assert not fx.quit_called


# ------------------------------------------------------------- reconnect
def test_drop_sends_starting_and_keeps_link_and_guard(up):
    fx, sup = up
    sup.on_tunnel_inactive()
    assert fx.events == [("stop_tunnel",), ("state", ST_STARTING)]
    assert sup.phase == "gap" and fx.link == 40


def test_reconnect_burst(up):
    fx, sup = up
    drop_and_retry(fx, sup)
    burst = [e for e in fx.events if e[0] in ("config", "ip4", "state")]
    assert burst == [("state", ST_STARTING), ("config", CONFIG), ("ip4", SENTINEL_IP4),
                     ("ip4", REAL_IP4), ("state", ST_STARTED)]
    # nothing at all between the burst's signals
    i = fx.names().index("config")
    assert fx.names()[i:i + 4] == ["config", "ip4", "ip4", "state"]
    sup.on_ac_state(AC, AC_ACTIVATED, 0)
    assert sup.phase == "up"


def test_reconnect_escalation_sends_sentinel_alone_then_real(up):
    fx, sup = up
    drop_and_retry(fx, sup)
    sup.on_ac_state(AC, AC_ACTIVATING, 6)
    fx.clear()
    fx.advance(10)
    assert fx.signals() == [("config", CONFIG), ("ip4", SENTINEL_IP4), ("state", ST_STARTED)]
    fx.clear()
    sup.on_ac_state(AC, AC_ACTIVATED, 0)
    assert fx.signals() == [("ip4", REAL_IP4)] and sup.phase == "up"


def test_reconnect_never_activated_gives_up_after_60s(up):
    fx, sup = up
    drop_and_retry(fx, sup)
    sup.on_ac_state(AC, AC_ACTIVATING, 6)
    fx.advance(59.9)
    assert sup.phase == "reconfig"
    fx.advance(0.2)
    assert sup.phase == "stopping" and ("failure", FAIL_CONNECT) in fx.events


def test_backoff_and_reconnect_timeout(up):
    fx, sup = up
    sup.on_tunnel_inactive()
    delays = []
    for _ in range(8):
        when = min(w for w, h, _ in fx.timers if h not in fx.cancelled)
        delays.append(round(when - fx.t))
        fx.advance(when - fx.t)
        fx.finish_start(False, "start job failed\nConnection timed out")
    assert delays == [1, 2, 4, 8, 16, 32, 60, 60]
    fx.advance(600)
    assert ("failure", FAIL_CONNECT) in fx.events and sup.phase != "gap"


def test_reconnect_auth_failure_is_login_failed(up):
    fx, sup = up
    gap(fx, sup)
    fx.advance(1)
    fx.finish_start(False, "alice@corp: Host key verification failed.")
    assert ("failure", FAIL_LOGIN) in fx.events


def test_drop_before_activated_waits_for_activated_then_starting():
    fx, sup = make()
    bring_up(fx, sup, activated=False)
    fx.clear()
    sup.on_tunnel_inactive()
    assert ("state", ST_STARTING) not in fx.events
    sup.on_ac_state(AC, AC_ACTIVATED, 0)
    assert fx.events[-1] == ("state", ST_STARTING) or ("state", ST_STARTING) in fx.events


def test_drop_before_activated_gives_up_after_2s():
    fx, sup = make()
    bring_up(fx, sup, activated=False)
    sup.on_tunnel_inactive()
    fx.advance(2)
    assert ("failure", FAIL_CONNECT) in fx.events


def test_invisible_reconnect_without_active_connection():
    fx, sup = make()
    sup.connect(settings(gateway="198.51.100.9"), NM)
    fx.finish_start(True)
    fx.ac_cbs.pop(0)(None, None)
    fx.clear()
    sup.on_tunnel_inactive()
    fx.advance(1)
    fx.finish_start(True)
    assert fx.signals() == [] and sup.phase == "up"


def test_flip_during_gap_is_bounced_three_times_then_gives_up(up):
    fx, sup = up
    gap(fx, sup)
    for _ in range(3):
        sup.on_ac_state(AC, AC_ACTIVATED, 0)
        assert fx.signals()[-2:] == [("state", ST_STARTED), ("state", ST_STARTING)]
        sup.on_ac_state(AC, AC_ACTIVATING, 6)
    sup.on_ac_state(AC, AC_ACTIVATED, 0)
    assert ("failure", FAIL_CONNECT) in fx.events


def test_burst_waits_while_a_flip_is_bounced(up):
    fx, sup = up
    gap(fx, sup)
    fx.advance(1)
    sup.on_ac_state(AC, AC_ACTIVATED, 0)        # flip, bounced
    fx.finish_start(True)
    assert "config" not in fx.names()
    sup.on_ac_state(AC, AC_ACTIVATING, 6)
    fx.advance(0.1)
    assert "config" in fx.names()


def test_nm_deactivating_during_gap_stops_reconnecting(up):
    fx, sup = up
    gap(fx, sup)
    sup.on_ac_state(AC, AC_DEACTIVATING, 2)
    fx.advance(5)
    assert "start_tunnel" not in fx.names()


# --------------------------------------------------- give-up and link loss
def assert_burst(fx, expected):
    names = fx.names()
    first = names.index(expected[0])
    assert names[first:first + len(expected)] == expected


def test_link_lost_in_gap_gives_up_at_once_branch4(up):
    fx, sup = up
    gap(fx, sup)
    fx.link = None
    sup.check_link()
    assert fx.signals() == [("config", CONFIG), ("failure", FAIL_CONNECT),
                            ("state", ST_STOPPED)]
    assert_burst(fx, ["config", "failure", "state", "stop_tunnel"])


def test_link_lost_noticed_on_nm_flip(up):
    # Deleting nmss0 flips NM to "activated" (R4p, R8p); no bounce, give up at once.
    fx, sup = up
    gap(fx, sup)
    fx.link = None
    sup.on_ac_state(AC, AC_ACTIVATED, 0)
    assert fx.signals() == [("config", CONFIG), ("failure", FAIL_CONNECT),
                            ("state", ST_STOPPED)]


def test_link_lost_while_up_is_branch4(up):
    fx, sup = up
    fx.link = None
    sup.check_link()
    assert fx.signals() == [("config", CONFIG), ("failure", FAIL_CONNECT),
                            ("state", ST_STOPPED)]


def test_link_replaced_sends_no_config(up):
    fx, sup = up
    gap(fx, sup)
    fx.link = 99
    sup.check_link()
    assert fx.signals() == [("failure", FAIL_CONNECT), ("state", ST_STOPPED)]


def test_give_up_in_gap_with_link_is_branch3(up):
    fx, sup = up
    gap(fx, sup)
    sup.give_up(FAIL_CONNECT, "test")
    assert fx.signals() == [("config", CONFIG), ("failure", FAIL_CONNECT),
                            ("state", ST_STOPPED)]


def test_give_up_while_activated_is_branch2(up):
    fx, sup = up
    sup.give_up(FAIL_CONNECT, "test")
    assert fx.signals() == [("failure", FAIL_CONNECT), ("state", ST_STOPPED)]


def test_give_up_while_deactivating_is_branch1(up):
    fx, sup = up
    gap(fx, sup)
    sup.on_ac_state(AC, AC_DEACTIVATING, 2)
    sup.give_up(FAIL_CONNECT, "test")
    assert fx.signals() == [("state", ST_STOPPED)]


def test_link_removed_100ms_after_deactivated(up):
    fx, sup = up
    gap(fx, sup)
    sup.give_up(FAIL_CONNECT, "test")
    fx.advance(0.5)
    assert "remove_link" not in fx.names()
    sup.on_ac_state(AC, AC_DEACTIVATED, 0)
    fx.advance(0.09)
    assert "remove_link" not in fx.names()
    fx.advance(0.02)
    assert "remove_link" in fx.names()


def test_link_removed_at_most_2s_after_stopped(up):
    fx, sup = up
    gap(fx, sup)
    sup.give_up(FAIL_CONNECT, "test")
    fx.advance(1.99)
    assert "remove_link" not in fx.names()
    fx.advance(0.02)
    assert fx.names()[-2:] == ["remove_link", "remove_guard"]


def test_disconnect_after_give_up_gets_no_second_stopped(up):
    fx, sup = up
    gap(fx, sup)
    sup.give_up(FAIL_CONNECT, "test")
    sup.disconnect()
    fx.advance(0.1)
    fx.finish_stops()
    assert fx.names().count("state") == 1 and sup.phase == "idle"


# ---------------------------------------------------------------- SIGTERM
def test_sigterm_in_gap_gives_up_then_waits_for_disconnect(up):
    fx, sup = up
    gap(fx, sup)
    sup.on_term()
    assert fx.signals() == [("config", CONFIG), ("failure", FAIL_CONNECT),
                            ("state", ST_STOPPED)]
    assert_burst(fx, ["config", "failure", "state", "stop_tunnel"])
    fx.finish_stops()
    fx.advance(0.001)
    assert not fx.quit_called           # still waiting for NM's Disconnect
    sup.on_ac_state(AC, AC_DEACTIVATED, 0)
    sup.disconnect()
    assert not fx.quit_called           # link goes 100 ms after "deactivated"
    fx.advance(0.1)
    assert fx.quit_called
    assert fx.names().index("remove_link") < fx.names().index("quit")
    assert fx.names().count("state") == 1


def test_sigterm_without_disconnect_exits_after_the_wait(up):
    fx, sup = up
    gap(fx, sup)
    sup.on_term()
    fx.finish_stops()
    sup.on_ac_state(AC, AC_DEACTIVATED, 0)
    fx.advance(0.5)
    assert not fx.quit_called
    fx.advance(0.5)
    assert fx.quit_called


def test_sigterm_while_up_is_branch2_and_exits(up):
    fx, sup = up
    sup.on_term()
    assert fx.signals() == [("failure", FAIL_CONNECT), ("state", ST_STOPPED)]
    fx.finish_stops()
    fx.advance(2)
    assert fx.quit_called and "remove_guard" in fx.names()


def test_sigterm_exits_within_5s_even_if_the_stop_hangs(up):
    fx, sup = up
    sup.on_term()
    fx.advance(4.9)
    assert not fx.quit_called
    fx.advance(0.2)
    assert fx.quit_called and "remove_link" in fx.names()


def test_sigterm_when_idle_exits_at_once():
    fx, sup = make()
    sup.on_term()
    assert fx.quit_called


def test_sigterm_during_disconnect_teardown_exits_when_done(up):
    fx, sup = up
    sup.disconnect()
    sup.on_term()
    fx.advance(0.1)
    assert not fx.quit_called
    fx.finish_stops()
    assert fx.quit_called and fx.events[-2:] == [("flush",), ("quit",)]


# ------------------------------------------------------------ NM vanishes
def test_nm_vanishing_removes_link_first_and_says_nothing(up):
    fx, sup = up
    sup.on_nm_owner(None)
    assert fx.names()[:2] == ["remove_link", "stop_tunnel"]
    assert fx.signals() == []
    fx.finish_stops()
    assert fx.names()[-3:] == ["remove_guard", "flush", "quit"]


def test_reconnect_burst_needs_the_same_nm(up):
    fx, sup = up
    gap(fx, sup)
    sup.nm_current_owner = ":1.99"
    fx.advance(1)
    fx.finish_start(True)
    assert fx.signals() == []          # never to an NM that did not connect
    assert sup.phase == "stopping"


# ------------------------------------------------- findings of the code review
def test_connect_then_disconnect_during_cleanup_never_connects():
    fx, sup = make()
    sup.start()
    sup.connect(settings(gateway="198.51.100.9"), NM)
    sup.disconnect()
    fx.cleanup_cb()
    assert "create_link" not in fx.names() and sup.phase == "idle"


def test_off_on_off_during_teardown_never_connects(up):
    fx, sup = up
    sup.disconnect()
    sup.connect(settings(gateway="198.51.100.9"), NM)
    sup.disconnect()
    fx.advance(0.1)
    fx.finish_stops()
    assert "create_link" not in fx.names() and sup.phase == "idle"


def test_first_config_is_not_sent_once_nm_is_deactivating():
    fx, sup = make()
    fx.auto_device = False
    sup.connect(settings(gateway="198.51.100.9"), NM)
    fx.ac_cbs.pop(0)(AC, AC_ACTIVATING)
    fx.finish_start(True)
    fx.clear()
    sup.on_ac_state(AC, AC_DEACTIVATING, 2)
    fx.device_cbs.pop(0)(DEVICE)
    assert "config" not in fx.names() and "ip4" not in fx.names()
    assert ("state", ST_STARTED) not in fx.events


def test_first_config_is_not_sent_to_another_nm():
    fx, sup = make()
    fx.auto_device = False
    sup.connect(settings(gateway="198.51.100.9"), NM)
    fx.finish_start(True)
    sup.nm_current_owner = ":1.77"
    fx.clear()
    fx.device_cbs.pop(0)(DEVICE)
    assert fx.signals() == []


def test_tunnel_dying_before_the_first_config_gives_up():
    fx, sup = make()
    fx.auto_device = False
    sup.connect(settings(gateway="198.51.100.9"), NM)
    fx.finish_start(True)
    sup.on_tunnel_inactive()
    assert sup.phase == "stopping" and ("failure", FAIL_CONNECT) in fx.events
    assert "config" not in fx.names()


def test_failed_retry_after_nm_began_deactivating_does_not_retry(up):
    fx, sup = up
    gap(fx, sup)
    fx.advance(1)
    sup.on_ac_state(AC, AC_DEACTIVATING, 2)
    fx.clear()
    fx.finish_start(False, "start job failed\nConnection timed out")
    fx.advance(120)
    assert "start_tunnel" not in fx.names()


def test_retry_result_after_nm_began_deactivating_sends_nothing(up):
    fx, sup = up
    gap(fx, sup)
    fx.advance(1)
    sup.on_ac_state(AC, AC_DEACTIVATING, 2)
    fx.clear()
    fx.finish_start(True)
    fx.advance(1)
    assert fx.signals() == []


def test_tunnel_stopping_again_while_waiting_for_the_device_retries(up):
    fx, sup = up
    gap(fx, sup)
    fx.advance(1)
    fx.auto_device = False
    fx.finish_start(True)
    fx.clear()
    sup.on_tunnel_inactive()
    fx.auto_device = True
    fx.advance(2.1)
    assert "config" not in fx.names() and "start_tunnel" in fx.names()


def test_give_up_right_after_starting_is_branch3_not_2(up):
    # After STARTING, NM still shows "activated" until its own signal arrives.
    fx, sup = up
    sup.on_tunnel_inactive()
    assert sup.ac_state == AC_ACTIVATED and sup.starting_pending
    fx.clear()
    sup.on_term()
    assert fx.signals() == [("config", CONFIG), ("failure", FAIL_CONNECT),
                            ("state", ST_STOPPED)]


def test_giving_up_after_a_bounce_is_branch3(up):
    fx, sup = up
    gap(fx, sup)
    sup.on_ac_state(AC, AC_ACTIVATED, 0)      # flip, bounced
    fx.clear()
    sup.give_up(FAIL_CONNECT, "test")
    assert fx.signals()[0] == ("config", CONFIG)


def test_deadline_restarts_with_a_drop_during_reconfig(up):
    fx, sup = up
    drop_and_retry(fx, sup)
    first_burst = fx.t
    sup.on_tunnel_inactive()                  # the tunnel dies in the middle of reconfig
    assert sup.phase == "gap"
    while fx.t < first_burst + 70:            # attempts keep failing past the old deadline
        when = min(w for w, h, _ in fx.timers if h not in fx.cancelled)
        fx.advance(when - fx.t)
        fx.finish_start(False, "start job failed\nConnection timed out")
    when = min(w for w, h, _ in fx.timers if h not in fx.cancelled)
    fx.advance(when - fx.t)
    fx.finish_start(True)
    assert sup.phase == "reconfig"
    fx.advance(59)
    assert sup.phase == "reconfig"            # a stale first_burst_at would give up at once


def test_escalation_checks_the_link_and_nm(up):
    fx, sup = up
    drop_and_retry(fx, sup)
    fx.clear()
    fx.link = 99
    fx.advance(10)
    assert ("config", CONFIG) not in fx.events
    assert ("failure", FAIL_CONNECT) in fx.events


def test_touch_restarts_the_idle_timer():
    fx, sup = make()
    sup.start()
    fx.cleanup_cb()
    fx.advance(50)
    sup.touch()
    fx.advance(50)
    assert not fx.quit_called
    fx.advance(11)
    assert fx.quit_called
