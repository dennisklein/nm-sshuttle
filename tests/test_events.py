# SPDX-License-Identifier: MIT
"""Sleep, network changes, health checks, the lock screen and the NM version
mark (design §4.4), with fake events and a fake clock."""

import pytest

from fakes import AC, NM, bring_up, drop_and_retry, make, settings
from nm_sshuttle.const import (AC_ACTIVATED, AC_ACTIVATING, AC_DEACTIVATED, AC_DEACTIVATING,
                               CONN_FULL, CONN_LIMITED, CONN_NONE, CONN_PORTAL, FAIL_CONNECT,
                               FAIL_LOGIN, ST_STARTED, ST_STARTING, ST_STOPPED)

ONLINE = True      # an uplink is activated
OFFLINE = False
WIFI = ("wifi-1", ("192.168.1.20/24",), "192.168.1.1")
HOTEL = ("wifi-2", ("10.20.0.7/16",), "10.20.0.1")
AUTH_FAIL = "start job failed\nalice@corp: Permission denied (publickey)."


@pytest.fixture
def up():
    fx, sup = make()
    sup.on_network(ONLINE, CONN_FULL, WIFI)
    bring_up(fx, sup)
    fx.clear()
    return fx, sup


def starts(fx):
    return fx.names().count("start_tunnel")


def answer_starting(sup):
    sup.on_ac_state(AC, AC_ACTIVATING, 6)


def reconnect(fx, sup, ok=True):
    """Finish the running attempt and let NM show "activated"."""
    fx.finish_start(ok)
    if ok:
        sup.on_ac_state(AC, AC_ACTIVATED, 0)


# ------------------------------------------------------------------ sleep
def test_inhibitor_held_while_on_and_released_at_teardown(up):
    fx, sup = up
    assert fx.inhibited
    sup.disconnect()
    fx.finish_stops()
    fx.advance(0.2)
    assert sup.phase == "idle" and not fx.inhibited


def test_suspend_stops_the_tunnel_then_releases_the_inhibitor(up):
    fx, sup = up
    sup.on_prepare_sleep(True)
    assert fx.events[:2] == [("stop_tunnel",), ("state", ST_STARTING)]
    assert sup.phase == "gap" and fx.inhibited       # until the stop is confirmed
    answer_starting(sup)
    fx.finish_stops()
    assert not fx.inhibited
    fx.advance(300)
    assert starts(fx) == 0                            # no attempts while asleep


def test_suspend_releases_the_inhibitor_after_3s_without_a_stop(up):
    fx, sup = up
    sup.on_prepare_sleep(True)
    fx.advance(2.9)
    assert fx.inhibited
    fx.advance(0.1)
    assert not fx.inhibited


def test_resume_takes_the_inhibitor_and_reconnects_at_once(up):
    fx, sup = up
    sup.on_prepare_sleep(True)
    answer_starting(sup)
    fx.finish_stops()
    fx.clear()
    sup.on_prepare_sleep(False)
    assert fx.inhibited
    fx.advance(0)
    # the first hop is resolved again first: the network may be another one
    assert fx.names() == ["first_hop"]
    fx.first_hop_cbs.pop(0)("203.0.113.7", None)
    assert starts(fx) == 1
    reconnect(fx, sup)
    assert sup.phase == "up"


def test_resume_while_nm_is_still_asleep_waits_for_the_network(up):
    fx, sup = up
    sup.on_prepare_sleep(True)
    answer_starting(sup)
    fx.finish_stops()
    sup.on_network(OFFLINE, CONN_NONE, None)
    sup.on_prepare_sleep(False)
    fx.advance(30)
    assert "first_hop" not in fx.names() and starts(fx) == 0
    sup.on_network(ONLINE, CONN_FULL, WIFI)
    fx.advance(0)
    assert "first_hop" in fx.names()


def test_suspend_during_the_first_connect_gives_up():
    fx, sup = make()
    sup.connect(settings(gateway="198.51.100.9"), NM)
    sup.on_prepare_sleep(True)
    assert ("failure", FAIL_CONNECT) in fx.events and sup.phase == "stopping"


def test_suspend_abandons_a_running_attempt(up):
    fx, sup = up
    sup.on_tunnel_inactive()
    answer_starting(sup)
    fx.advance(1)
    assert starts(fx) == 1
    sup.on_prepare_sleep(True)
    fx.finish_start(True)                             # the old attempt's late result
    assert sup.phase == "gap" and "find_device" not in fx.names()


def test_asleep_time_does_not_count_against_the_reconnect_timeout(up):
    fx, sup = up
    sup.on_tunnel_inactive()
    answer_starting(sup)
    fx.advance(500)                                   # attempts fail for 500 s ...
    while fx.start_cbs:
        fx.finish_start(False, "Connection refused")
        fx.advance(0)
    sup.on_prepare_sleep(True)
    fx.finish_stops()
    fx.advance(3600)                                  # ... then a long sleep
    sup.on_prepare_sleep(False)
    fx.advance(0)
    fx.first_hop_cbs.pop(0)("203.0.113.7", None)
    assert sup.phase == "gap" and "failure" not in fx.names()


# --------------------------------------------------------------- network
def test_network_change_while_up_restarts_at_once_with_a_new_first_hop(up):
    fx, sup = up
    sup.on_network(ONLINE, CONN_FULL, HOTEL)
    assert sup.phase == "gap"
    assert fx.events[:2] == [("stop_tunnel",), ("state", ST_STARTING)]
    answer_starting(sup)
    fx.advance(0)
    assert "first_hop" not in fx.names()              # waits for the stop first
    fx.finish_stops()
    assert fx.names()[-1] == "first_hop"
    fx.clear()
    fx.first_hop_cbs.pop(0)("198.51.100.200", None)
    assert fx.events[0] == ("install_guard", ["10.0.0.0/8"], ["198.51.100.200"])
    assert fx.events[1][0] == "spec" and fx.events[1][1]["first_hop"] == "198.51.100.200"
    fx.finish_start(True)
    config = [e[1] for e in fx.events if e[0] == "config"]
    assert config and config[0]["gateway"] == "198.51.100.200"


def test_give_up_before_the_new_first_hop_was_sent_repeats_the_old_config(up):
    # Branch 3 needs the Config NM has; the new first hop only goes out with a burst.
    fx, sup = up
    sup.on_network(ONLINE, CONN_FULL, HOTEL)
    answer_starting(sup)
    fx.finish_stops()
    fx.advance(0)
    fx.first_hop_cbs.pop(0)("198.51.100.200", None)
    fx.clear()
    sup.give_up(FAIL_CONNECT, "test")
    config = [e[1] for e in fx.events if e[0] == "config"]
    assert config and config[0]["gateway"] == "203.0.113.7"


def test_first_hop_answer_after_nm_began_deactivating_starts_nothing(up):
    fx, sup = up
    sup.on_network(ONLINE, CONN_FULL, HOTEL)
    answer_starting(sup)
    fx.finish_stops()
    fx.advance(0)
    sup.on_ac_state(AC, AC_DEACTIVATING, 0)
    fx.clear()
    fx.first_hop_cbs.pop(0)("198.51.100.200", None)
    assert fx.events == []


def test_same_network_after_a_renewal_changes_nothing(up):
    fx, sup = up
    sup.on_network(ONLINE, CONN_FULL, WIFI)
    sup.on_network(ONLINE, CONN_LIMITED, WIFI)
    assert sup.phase == "up" and fx.events == []


def test_no_primary_connection_for_a_moment_is_not_a_change(up):
    fx, sup = up
    sup.on_network(ONLINE, CONN_FULL, None)
    sup.on_network(ONLINE, CONN_FULL, WIFI)
    assert sup.phase == "up" and fx.events == []


def test_first_hop_lookup_failing_keeps_the_old_address(up):
    fx, sup = up
    sup.on_network(ONLINE, CONN_FULL, HOTEL)
    answer_starting(sup)
    fx.finish_stops()
    fx.advance(0)
    fx.clear()
    fx.first_hop_cbs.pop(0)(None, "Name or service not known")
    assert fx.names() == ["start_tunnel"]
    assert sup.gateway == "203.0.113.7"


def test_a_profile_gateway_is_not_resolved_again():
    fx, sup = make()
    sup.on_network(ONLINE, CONN_FULL, WIFI)
    bring_up(fx, sup, gateway="198.51.100.9")
    sup.on_network(ONLINE, CONN_FULL, HOTEL)
    answer_starting(sup)
    fx.finish_stops()
    fx.advance(0)
    assert "first_hop" not in fx.names() and fx.names()[-1] == "start_tunnel"


def test_offline_while_up_drops_and_waits(up):
    fx, sup = up
    sup.on_network(OFFLINE, CONN_NONE, None)
    assert sup.phase == "gap" and ("state", ST_STARTING) in fx.events
    answer_starting(sup)
    fx.advance(1200)
    assert starts(fx) == 0 and "failure" not in fx.names()   # offline time does not count
    sup.on_network(ONLINE, CONN_FULL, WIFI)
    fx.finish_stops()
    fx.advance(0)
    assert "first_hop" in fx.names()


def test_connectivity_none_with_an_activated_uplink_is_not_offline(up):
    # A LAN without a default route can still reach a first hop on it.
    fx, sup = up
    sup.on_network(ONLINE, CONN_NONE, WIFI)
    assert sup.phase == "up" and fx.events == []


def test_network_change_in_a_gap_abandons_an_old_attempt(up):
    fx, sup = up
    sup.on_tunnel_inactive()
    answer_starting(sup)
    fx.advance(1)
    assert starts(fx) == 1
    fx.advance(5)                                     # the attempt hangs on the old network
    fx.clear()
    sup.on_network(ONLINE, CONN_FULL, HOTEL)
    assert fx.names() == ["stop_tunnel"]
    fx.finish_stops()
    fx.advance(0)
    assert fx.names()[-1] == "first_hop"
    fx.finish_start(True)                             # the abandoned attempt's result
    assert sup.phase == "gap" and "find_device" not in fx.names()


def test_network_change_right_after_an_attempt_started_keeps_it(up):
    fx, sup = up
    sup.on_tunnel_inactive()
    answer_starting(sup)
    fx.advance(1)
    fx.clear()
    sup.on_network(ONLINE, CONN_FULL, HOTEL)
    assert fx.names() == []
    fx.finish_start(True)
    assert sup.phase == "reconfig"


def test_captive_portal_allows_one_attempt_a_minute(up):
    fx, sup = up
    sup.on_network(ONLINE, CONN_PORTAL, WIFI)
    sup.on_tunnel_inactive()
    answer_starting(sup)
    fx.finish_stops()
    fx.advance(59)
    assert starts(fx) == 0
    fx.advance(1)
    assert starts(fx) == 1
    fx.finish_start(False, "Connection refused")
    fx.advance(59)
    assert starts(fx) == 1
    sup.on_network(ONLINE, CONN_FULL, WIFI)           # signed in: the portal is gone
    fx.advance(0)
    assert starts(fx) == 2


# ---------------------------------------------------------------- health
def test_health_runs_every_10s_while_up(up):
    fx, sup = up
    fx.unit_active = None
    fx.advance(9.9)
    assert fx.tunnel_cbs == []
    fx.advance(0.1)
    assert len(fx.tunnel_cbs) == 1
    fx.advance(10)
    assert len(fx.tunnel_cbs) == 2


def test_health_restores_a_flushed_guard(up):
    fx, sup = up
    fx.health = {"guard": False, "sshuttle": True}
    fx.advance(10)
    assert fx.events == [("install_guard", ["10.0.0.0/8"], ["203.0.113.7"])]
    assert sup.phase == "up"


def test_health_drops_without_a_live_sshuttle_table(up):
    fx, sup = up
    fx.health = {"guard": False, "sshuttle": False}    # nft flush ruleset
    fx.advance(10)
    assert fx.names()[0] == "install_guard"
    assert sup.phase == "gap" and ("state", ST_STARTING) in fx.events


def test_health_skips_the_table_check_for_the_nat_method():
    fx, sup = make()
    bring_up(fx, sup, method="nat")
    fx.health = {"guard": True, "sshuttle": False}
    fx.advance(10)
    assert sup.phase == "up"


def test_health_drops_when_the_unit_is_inactive(up):
    fx, sup = up
    fx.unit_active = False
    fx.advance(10)
    assert sup.phase == "gap"


def test_health_ignores_an_unreadable_unit_and_nft(up):
    fx, sup = up
    fx.health = None
    fx.unit_active = None
    fx.advance(10)
    fx.tunnel_cbs.pop(0)(None)
    assert sup.phase == "up"


def test_health_stops_with_the_gap_and_comes_back(up):
    fx, sup = up
    fx.unit_active = None
    sup.on_tunnel_inactive()
    answer_starting(sup)
    fx.advance(1)
    fx.advance(30)
    assert fx.tunnel_cbs == []
    reconnect(fx, sup)
    assert sup.phase == "up"
    fx.advance(10)
    assert len(fx.tunnel_cbs) == 1


@pytest.fixture
def probed():
    fx, sup = make()
    bring_up(fx, sup, probe="10.1.0.10:22")
    fx.clear()
    return fx, sup


def probe_round(fx, ok):
    fx.advance(10)
    target, done = fx.probe_cbs.pop(0)
    assert target == ("10.1.0.10", 22)
    done(ok, "" if ok else "no data within 5 s")


def test_two_probe_failures_in_a_row_restart_the_tunnel(probed):
    fx, sup = probed
    probe_round(fx, True)
    probe_round(fx, False)
    assert sup.phase == "up"
    probe_round(fx, False)
    assert sup.phase == "gap"


def test_a_probe_success_resets_the_count(probed):
    fx, sup = probed
    probe_round(fx, True)
    probe_round(fx, False)
    probe_round(fx, True)
    probe_round(fx, False)
    assert sup.phase == "up"


def test_a_probe_that_never_answered_does_not_count(probed):
    fx, sup = probed
    for _ in range(5):
        probe_round(fx, False)
    assert sup.phase == "up"


def test_a_late_probe_result_from_before_a_drop_is_ignored(probed):
    fx, sup = probed
    probe_round(fx, True)
    fx.advance(10)
    _target, late = fx.probe_cbs.pop(0)
    sup.on_tunnel_inactive()
    late(False, "x")
    late(False, "x")
    assert sup.phase == "gap" and sup.probe_failures == 0


# ----------------------------------------------------------- lock screen
def locked_gap(fx, sup):
    """A reconnect fails on authentication behind the lock screen."""
    sup.on_tunnel_inactive()
    answer_starting(sup)
    fx.finish_stops()
    fx.advance(1)
    fx.finish_start(False, AUTH_FAIL)
    fx.lock_cbs.pop(0)(True)
    fx.clear()


def test_auth_failure_behind_the_lock_screen_waits_for_the_unlock(up):
    fx, sup = up
    locked_gap(fx, sup)
    assert sup.waiting_unlock and sup.phase == "gap"
    fx.advance(1800)                                  # locked time does not count
    assert starts(fx) == 0 and "failure" not in fx.names()
    assert len(fx.lock_cbs) == 360                    # asked again every 5 s


def test_unlock_retries_once_with_time_for_the_prompt(up):
    fx, sup = up
    locked_gap(fx, sup)
    sup.on_lock_changed()
    fx.lock_cbs.pop()(False)
    fx.advance(0)
    assert starts(fx) == 1
    fx.advance(100)                                   # the prompt is up; 45 s is not enough
    assert sup.phase == "gap" and "failure" not in fx.names()
    reconnect(fx, sup)
    assert sup.phase == "up"


def test_no_answer_from_logind_while_waiting_is_no_unlock(up):
    fx, sup = up
    locked_gap(fx, sup)
    sup.on_lock_changed()
    fx.lock_cbs.pop()(None)
    fx.advance(0)
    assert starts(fx) == 0 and sup.waiting_unlock
    fx.advance(5)                                     # the next poll asks again
    fx.lock_cbs.pop()(False)
    fx.advance(0)
    assert starts(fx) == 1


def test_retry_after_the_unlock_failing_on_auth_is_login_failed(up):
    fx, sup = up
    locked_gap(fx, sup)
    sup.on_lock_changed()
    fx.lock_cbs.pop()(False)
    fx.advance(0)
    fx.finish_start(False, AUTH_FAIL)
    assert ("failure", FAIL_LOGIN) in fx.events and fx.lock_cbs == []


def test_retry_after_the_unlock_timing_out_is_login_failed(up):
    fx, sup = up
    locked_gap(fx, sup)
    fx.advance(5)                                     # the poll finds it unlocked
    fx.lock_cbs.pop()(False)
    fx.advance(0)
    fx.advance(110)
    assert ("failure", FAIL_LOGIN) in fx.events


def test_retry_after_the_unlock_failing_otherwise_backs_off(up):
    fx, sup = up
    locked_gap(fx, sup)
    sup.on_lock_changed()
    fx.lock_cbs.pop()(False)
    fx.advance(0)
    fx.finish_start(False, "Connection refused")
    assert sup.phase == "gap" and "failure" not in fx.names()
    fx.advance(2)
    fx.finish_start(False, AUTH_FAIL)                 # the unlock retry was spent
    assert ("failure", FAIL_LOGIN) in fx.events


def test_lock_query_without_an_answer_from_logind_gives_up(up):
    fx, sup = up
    sup.on_tunnel_inactive()
    answer_starting(sup)
    fx.advance(1)
    fx.finish_start(False, AUTH_FAIL)
    fx.lock_cbs.pop(0)(None)
    assert ("failure", FAIL_LOGIN) in fx.events


def test_disconnect_while_waiting_for_the_unlock(up):
    fx, sup = up
    locked_gap(fx, sup)
    sup.disconnect()
    fx.advance(30)
    assert fx.lock_cbs == [] and ("state", ST_STOPPED) in fx.events


# ------------------------------------------------- NM version and parking
def test_reconnect_deadline_marks_the_nm_version(up):
    fx, sup = up
    sup.on_nm_version("1.99.0")
    drop_and_retry(fx, sup)
    fx.clear()
    fx.advance(60)
    names = fx.names()
    assert names.index("failure") < names.index("mark_invisible")
    assert ("mark_invisible", "1.99.0") in fx.events


def test_a_marked_version_reconnects_invisibly():
    fx, sup = make()
    fx.invisible = {"1.99.0"}
    sup.on_nm_version("1.99.0")
    bring_up(fx, sup)
    assert sup.invisible
    fx.clear()
    sup.on_tunnel_inactive()
    assert ("state", ST_STARTING) not in fx.events
    fx.advance(1)
    fx.finish_start(True)
    assert sup.phase == "up" and "config" not in fx.names()


def test_other_versions_are_not_affected():
    fx, sup = make()
    fx.invisible = {"1.99.0"}
    sup.on_nm_version("1.56.1")
    bring_up(fx, sup)
    assert not sup.invisible


def test_park_candidate_sends_a_lone_config_once_per_gap(up, monkeypatch):
    fx, sup = up
    monkeypatch.setattr(sup, "PARK_GAP", True)
    sup.on_tunnel_inactive()
    answer_starting(sup)
    answer_starting(sup)
    assert fx.signals() == [("state", ST_STARTING), ("config", sup.last_config)]
    fx.advance(1)
    fx.finish_start(True)
    assert [e[0] for e in fx.signals()][2:] == ["config", "ip4", "ip4", "state"]
    assert fx.signals()[-1] == ("state", ST_STARTED)


def test_park_candidate_is_off_by_default(up):
    fx, sup = up
    sup.on_tunnel_inactive()
    answer_starting(sup)
    assert fx.signals() == [("state", ST_STARTING)]


def test_deactivated_while_asleep_tears_down(up):
    fx, sup = up
    sup.on_prepare_sleep(True)
    answer_starting(sup)
    sup.on_ac_state(AC, AC_DEACTIVATED, 0)
    sup.disconnect()
    fx.finish_stops()
    fx.advance(0.2)
    assert sup.phase == "idle"


def test_after_a_reconnect_the_probe_must_answer_again_before_it_counts(probed):
    fx, sup = probed
    probe_round(fx, True)
    probe_round(fx, False)
    probe_round(fx, False)
    assert sup.phase == "gap"
    sup.on_ac_state(AC, AC_ACTIVATING, 6)
    fx.finish_stops()
    fx.advance(1)
    reconnect(fx, sup)
    assert sup.phase == "up"
    for _ in range(3):
        probe_round(fx, False)
    assert sup.phase == "up"
    probe_round(fx, True)
    probe_round(fx, False)
    probe_round(fx, False)
    assert sup.phase == "gap"
