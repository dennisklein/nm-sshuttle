# SPDX-License-Identifier: MIT
"""The plugin's lifecycle as a state machine (design §4.4).

Pure logic: everything that touches D-Bus, systemd, the kernel or the clock
goes through an Effects object, and every result comes back as a method call.
service.py provides the real Effects; the tests drive this class with fake
events and a fake clock.

Phases:
  cleaning    startup cleanup of a dead predecessor's leftovers; a Connect waits
  idle        nothing held (NM sees INIT or STOPPED); exits after IDLE_EXIT
  connecting  first connect: first hop, link, guard, tunnel, NM's device
  up          STARTED sent and the tunnel runs
  gap         reconnecting: STARTING sent, link and guard kept, tunnel restarting
  reconfig    reconnect burst sent, waiting for NM's "activated"
  stopping    give-up or Disconnect: tunnel stopping, link removed after NM
              has deactivated the connection
  exiting     NetworkManager vanished: tearing down without telling anyone
  exited      the main loop was asked to quit
"""

import functools
import logging
import traceback

from . import profile as profile_mod
from .const import (AC_ACTIVATED, AC_ACTIVATING, AC_DEACTIVATED, AC_DEACTIVATING,
                    AC_NAMES, AC_UNKNOWN, DEVICE_UNMANAGED, FAIL_CONNECT, FAIL_LOGIN,
                    LINK, LINK_ADDRESS, NBNS_SENTINEL, ST_INIT, ST_STARTED, ST_STARTING,
                    ST_STOPPED, ST_STOPPING, STATE_NAMES)
from .tunnel import classify_failure

log = logging.getLogger("nm-sshuttle")

ACTIVE = ("connecting", "up", "gap", "reconfig")


def guarded(fn):
    """Every entry point and callback: an exception becomes a give-up, never a
    silent hang that NM would wait out (design §4.1)."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except Exception as e:  # noqa: BLE001 - every exception must reach NM
            self._internal_error(fn.__name__, e)
            return None
    return wrapper


class Supervisor:
    DEVICE_POLL = 0.1          # GetDeviceByIpIface at once, then every 100 ms ...
    DEVICE_TIMEOUT = 10.0      # ... for up to 10 s (design §4.1)
    TUNNEL_START_TIMEOUT = 60.0  # the unit's own TimeoutStartSec=45 fails first
    STARTING_WAIT = 2.0        # a drop during a reconfiguration waits for "activated"
    ESCALATE_AFTER = 10.0      # not "activated": sentinel alone (design §4.4)
    RECONFIG_DEADLINE = 60.0   # still not "activated": give up
    MAX_BOUNCES = 3            # early "activated" during a gap, per gap
    BACKOFF = (1, 2, 4, 8, 16, 32, 60)
    RECONNECT_TIMEOUT = 600.0  # attempt time before giving up
    LINK_DELAY = 0.1           # remove nmss0 100 ms after "deactivated" ...
    LINK_CAP = 2.0             # ... and at most 2 s after STOPPED
    DISCONNECT_WAIT = 1.0      # on SIGTERM, wait this long for NM's Disconnect
    QUIT_CAP = 5.0             # whatever happens, a stopping process exits by then
    STOP_CAP = 20.0            # tunnel stop not confirmed: carry on regardless
    IDLE_EXIT = 60.0

    def __init__(self, fx, parse_profile=profile_mod.parse):
        self.fx = fx
        self.parse_profile = parse_profile
        self.phase = "idle"
        self.state = ST_INIT
        self.timers = {}
        self.activation_id = 0
        self.attempt_id = 0
        self.poll_id = 0
        self.pending_connect = None
        self.nm_current_owner = None
        self.quit_requested = False
        self.handling_error = False
        self._reset_activation()

    def _reset_activation(self):
        self.profile = None
        self.nm_owner = None           # NM's unique name that called Connect
        self.ac_path = None
        self.ac_state = AC_UNKNOWN
        self.ac_seen = {}              # AC states seen before our own path was known
        self.invisible = False         # no active connection found: invisible reconnect
        self.gateway = None
        self.ifindex = None
        self.device_path = None
        self.stale_device = None
        self.last_config = None
        self.guard_installed = False
        self.activated_once = False
        self.link_managed = None       # result of the unmanaged check (design §4.1)
        self.stopped_sent = False
        self.nm_deactivating = False
        self.gap_started = None
        self.attempts = 0
        self.bounces = 0
        self.starting_sent = False
        self.first_burst_at = None
        self.escalated = False
        self.stopping_since = None
        self.disconnect_seen = False
        self.disconnect_wait_over = False
        self.tunnel_stopped = False
        self.link_removed = False
        self.tunnel_running = False    # a start job succeeded and no stop was asked for since
        self.starting_pending = False  # STARTING sent from "activated"; NM has not answered

    # ------------------------------------------------------------ helpers
    def _set_state(self, state):
        log.info("state %s -> %s", STATE_NAMES.get(self.state), STATE_NAMES.get(state))
        self.state = state
        if state == ST_STOPPED:
            self.stopped_sent = True
        self.fx.emit_state(state)

    def _timer(self, name, seconds, fn, *args):
        self._cancel(name)

        def fire():
            self.timers.pop(name, None)
            self._run_guarded(fn.__name__, fn, *args)
        self.timers[name] = self.fx.call_later(seconds, fire)

    def _cancel(self, *names):
        for name in names:
            handle = self.timers.pop(name, None)
            if handle is not None:
                self.fx.cancel(handle)

    def _cancel_all(self):
        self._cancel(*list(self.timers))

    def _run_guarded(self, where, fn, *args):
        try:
            fn(*args)
        except Exception as e:  # noqa: BLE001
            self._internal_error(where, e)

    def _callback(self, fn, *bound):
        """A guarded callback for an Effects call."""
        def cb(*args):
            self._run_guarded(fn.__name__, fn, *bound, *args)
        return cb

    def _internal_error(self, where, exc):
        log.error("unexpected error in %s: %r\n%s", where, exc, traceback.format_exc())
        if self.handling_error:
            raise exc
        self.handling_error = True
        try:
            if self.phase in ACTIVE:
                self.give_up(FAIL_CONNECT, f"internal error in {where}: {exc}")
            elif self.phase == "stopping":
                self._remove_link_and_guard()
                self.tunnel_stopped = True
                self.disconnect_wait_over = True
                self._maybe_finish()
        except Exception as e2:  # noqa: BLE001
            log.error("error while handling that error: %r", e2)
            if not self.stopped_sent and self.state != ST_INIT:
                try:
                    self._set_state(ST_STOPPED)
                except Exception:  # noqa: BLE001
                    pass
            self.phase = "idle"
            self._arm_idle()
        finally:
            self.handling_error = False

    def _safe(self, fn, *args):
        """For the give-up's signals: one failing must not stop the next."""
        try:
            fn(*args)
        except Exception as e:  # noqa: BLE001
            log.error("%s failed: %r", fn.__name__, e)

    def _arm_idle(self, seconds=None):
        self._timer("idle", self.IDLE_EXIT if seconds is None else seconds, self._idle_exit)

    def _idle_exit(self):
        if self.phase == "idle":
            log.info("idle; exiting")
            self._quit_now()

    def _quit_now(self):
        if self.phase == "exited":
            return
        self._cancel_all()
        self.phase = "exited"
        self.fx.flush()
        self.fx.quit()

    def _foreign_nm(self):
        """NM is not the instance that called Connect: say nothing to it."""
        return bool(self.nm_owner and self.nm_current_owner != self.nm_owner)

    def _nm_ending(self):
        """NM is deactivating our connection, or is not the NM that connected."""
        return (self.nm_deactivating or self.ac_state in (AC_DEACTIVATING, AC_DEACTIVATED)
                or self._foreign_nm())

    def _link_intact(self):
        return self.ifindex is not None and self.fx.link_ifindex() == self.ifindex

    def _config(self):
        return {"gateway": self.gateway, "tundev": LINK, "has-ip4": True,
                "has-ip6": False, "can-persist": True}

    def _ip4_config(self, sentinel=False):
        p = self.profile
        cfg = {"address": LINK_ADDRESS, "prefix": 32, "never-default": True}
        if p.dns != "none":
            cfg["dns"] = list(p.dns_servers)
            cfg["domains"] = list(p.dns_domains)
        if sentinel:
            # Same address, dns and domains: NM keeps whichever it commits (design §2.1).
            cfg["nbns"] = [NBNS_SENTINEL]
        return cfg

    # ------------------------------------------------------- process start
    @guarded
    def start(self):
        """At every process start: remove a dead predecessor's leftovers before
        any Connect is served (design §4.4, "The plugin starts")."""
        self.phase = "cleaning"
        self.fx.run_cleanup(self._callback(self._cleanup_done))

    def _cleanup_done(self):
        if self.phase != "cleaning":
            return
        self.phase = "idle"
        if self.quit_requested:
            self._quit_now()
        elif self.pending_connect:
            settings, sender = self.pending_connect
            self.pending_connect = None
            self._begin_connect(settings, sender)
        else:
            self._arm_idle()

    def touch(self):
        """Any VPN.Plugin call restarts the idle timer (design §4.1)."""
        if self.phase == "idle":
            self._arm_idle()

    # -------------------------------------------------------------- connect
    @guarded
    def connect(self, settings, sender=None):
        self._cancel("idle")
        if self.phase in ("cleaning", "stopping"):
            log.info("Connect arrived during %s; it runs once that is done", self.phase)
            self.pending_connect = (settings, sender)
            return
        if self.phase != "idle":
            log.warning("Connect ignored: an activation is already running (%s)", self.phase)
            return
        self._begin_connect(settings, sender)

    def _begin_connect(self, settings, sender):
        # A Connect after STOPPED in the same process starts from clean state.
        self._cancel_all()
        self._reset_activation()
        self.activation_id += 1
        self.attempt_id += 1
        self.poll_id += 1
        self.nm_owner = sender
        self.phase = "connecting"
        self._set_state(ST_STARTING)
        try:
            self.profile = self.parse_profile(settings)
        except profile_mod.ProfileError as e:
            self.give_up(FAIL_CONNECT, f"invalid profile: {e}")
            return
        p = self.profile
        log.info("connecting %s (%s): remote %s as %s, subnets %s, dns %s", p.id, p.uuid,
                 p.remote, p.user, ",".join(p.subnets), p.dns)
        if p.ignored_keys:
            log.warning("ignoring unknown profile keys: %s", ", ".join(p.ignored_keys))
        self.fx.find_active_connection(p.uuid,
                                       self._callback(self._on_ac_found, self.activation_id))
        if p.gateway:
            self._have_first_hop(p.gateway, None)
        else:
            self.fx.resolve_first_hop(p.user, p.remote,
                                      self._callback(self._first_hop_result, self.activation_id))

    def _on_ac_found(self, aid, path, state):
        if aid != self.activation_id or self.phase not in ACTIVE:
            return
        if path is None:
            log.warning("cannot find this profile's active connection; reconnects will "
                        "not show in NetworkManager (invisible reconnect)")
            self.invisible = True
            return
        self.ac_path = path
        seen = self.ac_seen.pop(path, None)
        self.ac_seen.clear()
        self._ac_changed(seen if seen is not None else state, None)

    def _first_hop_result(self, aid, addr, error):
        if aid != self.activation_id or self.phase != "connecting":
            return
        if addr is None:
            self.give_up(FAIL_CONNECT, f"cannot work out the first hop: {error}")
            return
        self._have_first_hop(addr, None)

    def _have_first_hop(self, addr, stale_device):
        self.gateway = addr
        if self.fx.link_ifindex() is not None and stale_device is None:
            # A leftover nmss0: note NM's device for it, so that the poll below does
            # not take it for the new link's (design §4.1).
            self.fx.find_device(LINK, self._callback(self._leftover_device, self.activation_id))
            return
        self.stale_device = stale_device
        try:
            self.fx.sweep()
            self.ifindex = self.fx.create_link()
            if self.profile.fail_closed:
                self.fx.install_guard(self.profile.guarded_networks(),
                                      [addr] + list(self.profile.exclude))
                self.guard_installed = True
            self.fx.write_tunnel_spec(self.profile.tunnel_spec(addr))
        except (OSError, RuntimeError) as e:
            self.give_up(FAIL_CONNECT, str(e))
            return
        log.info("first hop %s; %s has ifindex %s", addr, LINK, self.ifindex)
        self._start_tunnel(self._first_tunnel_result)

    def _leftover_device(self, aid, path):
        if aid != self.activation_id or self.phase != "connecting":
            return
        log.info("a leftover %s exists (NM device %s); replacing it", LINK, path)
        self._have_first_hop(self.gateway, path or "")

    def _first_tunnel_result(self, ok, detail):
        if self.phase != "connecting":
            return
        if not ok:
            code = FAIL_LOGIN if classify_failure(detail) == "login" else FAIL_CONNECT
            self.give_up(code, f"the tunnel did not start: {detail}")
            return
        self._wait_device(self._first_device)

    def _first_device(self, path):
        if self.phase != "connecting":
            return
        if path is None:
            self.give_up(FAIL_CONNECT, f"NetworkManager never created a device for {LINK}")
            return
        if not self._link_intact():
            self.give_up(FAIL_CONNECT, f"{LINK} was lost before the first Config")
            return
        if self._nm_ending():
            self.give_up(FAIL_CONNECT, "NetworkManager is ending the connection; no Config")
            return
        if not self.tunnel_running:
            self.give_up(FAIL_CONNECT, "the tunnel stopped before the first Config")
            return
        self.device_path = path
        self.last_config = self._config()
        self.fx.emit_config(self.last_config)
        self.fx.emit_ip4_config(self._ip4_config())
        self.phase = "up"
        self._set_state(ST_STARTED)

    # ----------------------------------------------------- tunnel and device
    def _start_tunnel(self, cb):
        self.attempt_id += 1
        aid = self.attempt_id
        self._timer("tunnel", self.TUNNEL_START_TIMEOUT, self._tunnel_timeout, aid, cb)
        self.fx.start_tunnel(self._callback(self._tunnel_result, aid, cb))

    def _tunnel_result(self, aid, cb, ok, detail):
        if aid != self.attempt_id:
            return
        self._cancel("tunnel")
        self.tunnel_running = ok
        cb(ok, detail)

    def _tunnel_timeout(self, aid, cb):
        if aid != self.attempt_id:
            return
        self.attempt_id += 1        # a late result no longer counts
        self.tunnel_running = False
        self.fx.stop_tunnel(lambda: None)
        cb(False, f"no result from systemd within {self.TUNNEL_START_TIMEOUT:.0f} s")

    def _wait_device(self, cb):
        """Ask NM for its device for nmss0 at once, then every 100 ms."""
        self.poll_id += 1
        pid = self.poll_id
        deadline = self.fx.now() + self.DEVICE_TIMEOUT
        self._device_ask(pid, deadline, cb)

    def _device_ask(self, pid, deadline, cb):
        if pid == self.poll_id:
            self.fx.find_device(LINK, self._callback(self._device_answer, pid, deadline, cb))

    def _device_answer(self, pid, deadline, cb, path):
        if pid != self.poll_id:
            return
        if path and path != self.stale_device:
            cb(path)
        elif self.fx.now() >= deadline:
            cb(None)
        else:
            self._timer("device", self.DEVICE_POLL, self._device_ask, pid, deadline, cb)

    # ------------------------------------------------- NM's active connection
    @guarded
    def on_ac_state(self, path, state, reason):
        """StateChanged of any active connection; only our own counts (design §4.1)."""
        if self.phase not in ACTIVE and self.phase != "stopping":
            return
        if self.ac_path is None:
            self.ac_seen[path] = state
            return
        if path == self.ac_path:
            self._ac_changed(state, reason)

    def _ac_changed(self, state, reason):
        prev, self.ac_state = self.ac_state, state
        if state == AC_ACTIVATING:
            self.starting_pending = False
        if prev != state:
            log.info("NM: active connection is %s%s", AC_NAMES.get(state, state),
                     f" (reason {reason})" if reason is not None else "")
        if self.phase in ACTIVE and self.ifindex is not None and not self._link_intact():
            self.give_up(FAIL_CONNECT, f"{LINK} was lost")
            return
        if state == AC_ACTIVATED and prev != AC_ACTIVATED:
            self._on_activated()
        elif state in (AC_DEACTIVATING, AC_DEACTIVATED):
            self._on_deactivating(state)

    def _on_activated(self):
        if not self.activated_once:
            self.activated_once = True
            if self.device_path:
                self.fx.device_state(self.device_path, self._callback(self._check_unmanaged))
        if self.phase == "reconfig":
            if self.escalated:
                self.fx.emit_ip4_config(self._ip4_config())
            log.info("reconnected after %.1f s", self.fx.now() - self.gap_started)
            self._end_gap()
        elif self.phase == "gap":
            if not self.starting_sent:
                self._send_starting()
            elif not self._link_intact():
                self.give_up(FAIL_CONNECT, f"{LINK} was lost during a reconnect")
            elif self.bounces < self.MAX_BOUNCES:
                # NM flipped to "activated" while the tunnel is down (design §4.4).
                self.bounces += 1
                log.info("NM shows activated during a reconnect; bouncing it back (%d/%d)",
                         self.bounces, self.MAX_BOUNCES)
                self.starting_pending = True
                self._set_state(ST_STARTED)
                self._set_state(ST_STARTING)
            else:
                self.give_up(FAIL_CONNECT, "NM keeps showing activated during a reconnect")

    def _check_unmanaged(self, state):
        """After the first "activated": state 10 means unmanaged (design §4.1)."""
        if self.phase not in ACTIVE:
            return
        if state is None:
            log.warning("cannot read NetworkManager's state for %s; the unmanaged "
                        "setting is unchecked", LINK)
        elif state == DEVICE_UNMANAGED:
            self.link_managed = False
            log.info("%s is unmanaged by NetworkManager, as configured", LINK)
        else:
            self.link_managed = True
            log.warning("%s is managed by NetworkManager (device state %s); DNS may take "
                        "the default route (risk 11). Is 90-nm-sshuttle.conf masked or "
                        "overridden, or has NetworkManager not reloaded it yet?", LINK, state)

    def _on_deactivating(self, state):
        if self.phase in ACTIVE:
            # NM is ending the VPN; its Disconnect follows. Never reconnect now.
            self.nm_deactivating = True
            self._cancel("retry", "escalate", "deadline", "starting-wait", "burst-wait")
        elif self.phase == "stopping" and state == AC_DEACTIVATED:
            self._timer("link", self.LINK_DELAY, self._remove_link_and_guard)

    # ------------------------------------------------------------ reconnect
    @guarded
    def on_tunnel_inactive(self):
        """The tunnel unit went inactive or failed. Expected stops and failing
        start jobs do not count: they report through their own results."""
        if not self.tunnel_running:
            return
        self.tunnel_running = False
        if self.phase == "connecting":
            self.give_up(FAIL_CONNECT, "the tunnel stopped before the first Config")
        elif self.phase in ("up", "reconfig"):
            self._drop("the tunnel unit stopped")
        elif self.phase == "gap":
            log.info("the tunnel stopped again during a reconnect")
            self.poll_id += 1
            self.attempt_id += 1
            self._cancel("burst-wait", "device")
            self.fx.stop_tunnel(lambda: None)
            if not self._nm_ending():
                self._schedule_retry()

    @guarded
    def simulate_drop(self):
        """Test hook (SIGUSR1): stop the tunnel as if it had dropped."""
        if self.phase != "up":
            log.info("simulated drop ignored during %s", self.phase)
            return
        log.info("simulating a drop")
        self._drop("simulated drop")

    def _drop(self, why):
        log.info("tunnel dropped (%s)", why)
        if self.nm_deactivating or self.ac_state in (AC_DEACTIVATING, AC_DEACTIVATED):
            self.give_up(FAIL_CONNECT, f"{why} while NM deactivates the connection")
            return
        self._cancel("escalate", "deadline", "burst-wait")
        self.poll_id += 1
        self.attempt_id += 1
        self.tunnel_running = False
        self.first_burst_at = None
        if self.phase == "up":
            self.gap_started = self.fx.now()
            self.attempts = 0
            self.bounces = 0
            self.starting_sent = False
        self.phase = "gap"
        self.escalated = False
        self.fx.stop_tunnel(lambda: None)
        if self.invisible:
            self._schedule_retry()
        elif self.ac_state == AC_ACTIVATED:
            self._send_starting()
        elif self.starting_sent and self.ac_state == AC_ACTIVATING:
            self._schedule_retry()      # NM already shows "connecting" from our STARTING
        else:
            # STARTING only while NM shows "activated" (design §4.4).
            self.starting_sent = False
            self._timer("starting-wait", self.STARTING_WAIT, self.give_up, FAIL_CONNECT,
                        "the connection did not reach activated before a reconnect")

    def _send_starting(self):
        self._cancel("starting-wait")
        self.starting_sent = True
        self.starting_pending = self.ac_state == AC_ACTIVATED
        self._set_state(ST_STARTING)
        self._schedule_retry()

    def _schedule_retry(self):
        if self._nm_ending():
            return      # NM's Disconnect follows; nothing may start the tunnel now
        if self.fx.now() - self.gap_started >= self.RECONNECT_TIMEOUT:
            self.give_up(FAIL_CONNECT, f"no reconnect within {self.RECONNECT_TIMEOUT:.0f} s")
            return
        delay = self.BACKOFF[min(self.attempts, len(self.BACKOFF) - 1)]
        log.info("reconnect attempt %d in %d s", self.attempts + 1, delay)
        self._timer("retry", delay, self._retry)

    def _retry(self):
        if self.phase != "gap":
            return
        if not self._link_intact():
            self.give_up(FAIL_CONNECT, f"{LINK} was lost during a reconnect")
            return
        self.attempts += 1
        self._start_tunnel(self._retry_result)

    def _retry_result(self, ok, detail):
        if self.phase != "gap":
            return
        if self._foreign_nm():
            self.give_up(FAIL_CONNECT, "NetworkManager is not the instance that connected")
            return
        if self._nm_ending():
            return
        if not ok:
            if classify_failure(detail) == "login":
                # TODO(M2): with logind's LockedHint set, wait for the unlock and retry once.
                self.give_up(FAIL_LOGIN, f"reconnect failed on authentication: {detail}")
            else:
                log.info("reconnect attempt %d failed: %s", self.attempts, detail.strip())
                self._schedule_retry()
            return
        if self.invisible:
            log.info("tunnel is back (invisible reconnect)")
            self._end_gap()
            return
        self._wait_device(self._retry_device)

    def _retry_device(self, path, waited_since=None):
        if self.phase != "gap":
            return
        if path is None:
            self.give_up(FAIL_CONNECT, f"NetworkManager has no device for {LINK}")
            return
        if not self._link_intact():
            self.give_up(FAIL_CONNECT, f"{LINK} was lost during a reconnect")
            return
        if self._foreign_nm():
            self.give_up(FAIL_CONNECT, "NetworkManager is not the instance that connected")
            return
        if self._nm_ending():
            return      # deactivating: its Disconnect follows; send nothing
        if not self.tunnel_running:
            return      # it stopped again; on_tunnel_inactive has scheduled a retry
        if self.ac_state != AC_ACTIVATING:
            # A flip is being bounced; wait until NM is back in "connecting".
            now = self.fx.now()
            waited_since = now if waited_since is None else waited_since
            if now - waited_since >= self.STARTING_WAIT:
                self.give_up(FAIL_CONNECT, "the connection is not activating; cannot "
                             "send the reconnect config")
            else:
                self._timer("burst-wait", self.DEVICE_POLL, self._retry_device, path,
                            waited_since)
            return
        self.device_path = path
        self._burst()

    def _burst(self):
        """Config, sentinel Ip4Config, real Ip4Config, STARTED, from one callback
        with nothing in between (design §4.4, "Coming back")."""
        self.fx.emit_config(self.last_config)
        self.fx.emit_ip4_config(self._ip4_config(sentinel=True))
        self.fx.emit_ip4_config(self._ip4_config())
        self.phase = "reconfig"
        self._set_state(ST_STARTED)
        now = self.fx.now()
        if self.first_burst_at is None:
            self.first_burst_at = now
        self._timer("escalate", self.ESCALATE_AFTER, self._escalate)
        self._timer("deadline", max(0.0, self.first_burst_at + self.RECONFIG_DEADLINE - now),
                    self.give_up, FAIL_CONNECT,
                    "NetworkManager never showed the reconnect as activated")

    def _escalate(self):
        if self.phase != "reconfig" or self.escalated:
            return
        if not self._link_intact():
            self.give_up(FAIL_CONNECT, f"{LINK} was lost during a reconnect")
            return
        if self._foreign_nm():
            self.give_up(FAIL_CONNECT, "NetworkManager is not the instance that connected")
            return
        if self._nm_ending():
            return
        log.info("not activated %.0f s after the reconnect config; sending the sentinel "
                 "alone", self.ESCALATE_AFTER)
        self.escalated = True
        self.fx.emit_config(self.last_config)
        self.fx.emit_ip4_config(self._ip4_config(sentinel=True))
        self._set_state(ST_STARTED)

    def _end_gap(self):
        self.starting_pending = False
        self.starting_sent = False
        self._cancel("escalate", "deadline", "burst-wait", "starting-wait")
        self.phase = "up"
        self.gap_started = None
        self.first_burst_at = None
        self.escalated = False

    # ------------------------------------------------------------- link loss
    @guarded
    def check_link(self):
        """Called periodically: nmss0 lost means give up at once (design §4.4, branch 4)."""
        if self.phase in ACTIVE and self.ifindex is not None and not self._link_intact():
            self.give_up(FAIL_CONNECT, f"{LINK} was lost")

    # --------------------------------------------------------------- give up
    def _pick_branch(self):
        """Design §4.4: chosen from cached state, with no D-Bus call before the signals."""
        if self.nm_owner and self.nm_current_owner != self.nm_owner:
            return "0"                          # not the NM that connected: say nothing
        if self.nm_deactivating or self.ac_state in (AC_DEACTIVATING, AC_DEACTIVATED):
            return "1"
        if self.last_config is None:
            return "2"                          # nothing sent yet: first connect
        current = self.fx.link_ifindex()
        if current is None:
            return "4"
        if current != self.ifindex:
            return "4-stale"
        if (self.ac_state == AC_ACTIVATED and not self.starting_pending) \
                or not self.activated_once:
            return "2"
        if self.phase in ("gap", "reconfig") and self.device_path:
            return "3"
        return "2"

    @guarded
    def give_up(self, reason, message, quit=False):
        if self.phase in ("idle", "cleaning"):
            if quit:
                self.quit_requested = True
                if self.phase == "idle":
                    self._quit_now()
            return
        if self.phase == "exiting":
            return
        if self.phase == "stopping":
            if quit:
                self._request_quit()
            return
        self._cancel_all()
        self.attempt_id += 1
        self.poll_id += 1
        branch = self._pick_branch()
        log.warning("giving up (branch %s, NM shows %s): %s", branch,
                    AC_NAMES.get(self.ac_state, self.ac_state), message)
        if branch in ("3", "4"):
            self._safe(self.fx.emit_config, self.last_config)
        if branch not in ("0", "1"):
            self._safe(self.fx.emit_failure, reason)
        if not self.stopped_sent and branch != "0":
            self._safe(self._set_state, ST_STOPPED)
        if branch == "4-stale":
            log.warning("a link named %s with another ifindex exists; NetworkManager may "
                        "keep a stale DNS entry for the old one", LINK)
        self._enter_stopping()
        if quit:
            self._request_quit()

    def _enter_stopping(self):
        self.phase = "stopping"
        self.stopping_since = self.fx.now()
        self.tunnel_running = False
        self.tunnel_stopped = False
        self.link_removed = False
        self.fx.stop_tunnel(self._callback(self._tunnel_stopped))
        self._timer("stop-cap", self.STOP_CAP, self._tunnel_stop_cap)
        self._timer("link-cap", self.LINK_CAP, self._remove_link_and_guard)
        if self.disconnect_seen or self.ac_state == AC_DEACTIVATED:
            self._timer("link", self.LINK_DELAY, self._remove_link_and_guard)

    def _request_quit(self):
        self.quit_requested = True
        if self.disconnect_seen:
            self.disconnect_wait_over = True
        elif "disconnect-wait" not in self.timers and not self.disconnect_wait_over:
            self._timer("disconnect-wait", self.DISCONNECT_WAIT, self._disconnect_wait_over)
        if "quit-cap" not in self.timers:
            self._timer("quit-cap", self.QUIT_CAP, self._quit_cap)
        self._maybe_finish()

    def _disconnect_wait_over(self):
        if not self.disconnect_seen:
            log.warning("no Disconnect from NetworkManager within %.0f s", self.DISCONNECT_WAIT)
        self.disconnect_wait_over = True
        self._maybe_finish()

    def _quit_cap(self):
        log.warning("teardown did not finish within %.0f s; exiting anyway", self.QUIT_CAP)
        self._remove_link_and_guard()
        self._quit_now()

    def _tunnel_stopped(self):
        if self.phase in ("stopping", "exiting"):
            self._cancel("stop-cap")
            self.tunnel_stopped = True
            self._maybe_finish()

    def _tunnel_stop_cap(self):
        log.warning("systemd did not confirm the tunnel stop within %.0f s", self.STOP_CAP)
        self._tunnel_stopped()

    def _remove_link_and_guard(self):
        if self.link_removed:
            return
        self._cancel("link", "link-cap")
        self.link_removed = True
        self.fx.remove_link()
        if self.guard_installed:
            self.fx.remove_guard()
            self.guard_installed = False
        self._maybe_finish()

    def _maybe_finish(self):
        if self.phase != "stopping" or not (self.tunnel_stopped and self.link_removed):
            return
        if self.quit_requested and not self.disconnect_wait_over:
            return
        self._cancel_all()
        self.phase = "idle"
        log.info("torn down")
        if self.quit_requested:
            self._quit_now()
        elif self.pending_connect:
            settings, sender = self.pending_connect
            self.pending_connect = None
            self._begin_connect(settings, sender)
        else:
            self._arm_idle()

    # ----------------------------------------------------------- disconnect
    @guarded
    def disconnect(self):
        """Toggle off, or NM ending the VPN after a failure."""
        if self.pending_connect:
            # The Connect that was queued behind a cleanup or a teardown is
            # the activation NM is now ending: never run it.
            log.info("Disconnect: dropping the queued Connect")
            self.pending_connect = None
        if self.phase in ("idle", "cleaning", "exiting"):
            # NM also calls Disconnect after a failure we reported, and after a
            # kill it is the first call a new instance gets (R4m).
            log.info("Disconnect: already stopped")
            if self.phase == "idle":
                self._arm_idle()
            return
        if self.phase == "stopping":
            # Answer it without a second STOPPED (design §4.4, step 5).
            self.disconnect_seen = True
            self.disconnect_wait_over = True
            if not self.link_removed:
                self._timer("link", self.LINK_DELAY, self._remove_link_and_guard)
            self._maybe_finish()
            return
        log.info("Disconnect: switching off")
        self._cancel_all()
        self.attempt_id += 1
        self.poll_id += 1
        self.nm_deactivating = True
        self.disconnect_seen = True
        self.disconnect_wait_over = True
        self._set_state(ST_STOPPING)
        # STOPPED at once: NM has dropped this connection, and a slow teardown
        # must not deliver it to a connection that is activated right after.
        self._set_state(ST_STOPPED)
        self._enter_stopping()

    # ---------------------------------------------------- signals and NM bus
    @guarded
    def on_term(self):
        """SIGTERM, or a stop or restart of nm-sshuttle.service (design §4.4)."""
        log.info("SIGTERM during %s", self.phase)
        if self.phase == "idle":
            self._quit_now()
        elif self.phase == "cleaning":
            self.quit_requested = True
        elif self.phase == "stopping":
            self._request_quit()
        elif self.phase in ACTIVE:
            self.give_up(FAIL_CONNECT, "the plugin is stopping", quit=True)

    @guarded
    def on_nm_owner(self, owner):
        """NetworkManager's bus name appeared (owner) or vanished (None)."""
        if owner:
            self.nm_current_owner = owner
            return
        if self.nm_current_owner is None:
            return
        self.nm_current_owner = None
        self._teardown_and_exit("NetworkManager vanished; tearing down, nobody else will")

    def _teardown_and_exit(self, why):
        log.warning("%s", why)
        if self.phase in ("idle", "exiting", "exited"):
            self._quit_now()
            return
        if self.phase == "cleaning":
            self.quit_requested = True
            return
        # nmss0 first, so that a quickly restarted NM cannot pick it up (design §4.4).
        self._cancel_all()
        self.attempt_id += 1
        self.poll_id += 1
        self.phase = "exiting"
        self.fx.remove_link()
        self.link_removed = True
        self.fx.stop_tunnel(self._callback(self._exit_after_stop))
        self._timer("quit-cap", self.QUIT_CAP, self._exit_after_stop)

    def _exit_after_stop(self):
        if self.phase != "exiting":
            return
        if self.guard_installed:
            self.fx.remove_guard()
            self.guard_installed = False
        self._quit_now()

    @guarded
    def on_name_lost(self):
        self._teardown_and_exit("lost the plugin's bus name; tearing down and exiting")
