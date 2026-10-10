# VPN: after a plugin-side reconnect with unchanged config, the VPN never returns to "activated" and never times out (regression in 1.36)

**Component:** `src/core/vpn/nm-vpn-connection.c` (together with `src/core/nm-l3cfg.c`)

## Summary

NetworkManager supports reconnects driven by the VPN plugin. The comment at `nm-vpn-connection.c:1802` reads "The VPN service got disconnected and is attempting to reconnect". The sequence is:

1. The plugin sends `StateChanged(STARTED)` and later `StateChanged(STARTING)`.
2. When it is back, it sends `Config` and `Ip4Config` again, followed by `StateChanged(STARTED)`.

If the re-sent configuration is identical to the one already applied, the VPN never returns to `activated`:

- its internal state stays `ip-config-get`;
- the active connection stays `activating`;
- no timeout fires;
- nothing is logged at the default log level.

The tunnel itself keeps working; only NetworkManager's state is wrong. We reproduced this with the small fake plugin below on 1.46.0. The connection stayed `activating` for 130 s until we deleted it. We also saw it with our own plugin on 1.46.0, and twice on 1.56.1. The second time, with trace logging, it stayed stuck for 41 s after the re-sent config, until an unrelated commit released it.

Cause: commit 58287cbcc0c8 ("core: rework IP configuration in NetworkManager using layer 3 configuration") was first released in 1.36.0. Since then, the move to `pre-up` waits for a `POST_COMMIT` notification from the tunnel interface's l3cfg. When the content has not changed, the VPN schedules no commit. The notification then never arrives, unless something else happens to commit that l3cfg. In 1.34 the VPN went to `pre-up` directly after applying the config.

A second, smaller problem is in the same code. `wait_for_pre_up_state` is set once and never cleared. During a reconnect, any commit on the tunnel's l3cfg therefore marks the VPN activated before the plugin has reconnected. We saw this on 1.56.1 with `nmcli device reapply` on the tunnel device, and when the tunnel device was deleted. The first 2026-10-10 run saw the deletion case three more times, and the second saw the same three again, plus once with the tunnel device unmanaged. With the tunnel device unmanaged, NetworkManager refuses the reapply.

## Affected versions

| Version | Result | Basis |
|---|---|---|
| 1.34.0 | not affected | Source only: `nm_vpn_connection_apply_config()` sets `STATE_PRE_UP` directly (1.34.0 `nm-vpn-connection.c:1189-1191`). |
| 1.36.0 | affected | Source only. It is the first stable tag that contains 58287cbcc0c8. It has the same `POST_COMMIT` gate, equality check and early return as 1.56.1. |
| 1.46.0 (Ubuntu 24.04, `network-manager 1.46.0-1ubuntu2.8`) | affected | Observed, in a container with debug logging, with the reproducer below and with our plugin. |
| 1.56.1 (Fedora 44, `NetworkManager-1.56.1-2.fc44`) | affected | Observed with our plugin, first with default logging, then with VPN and CORE trace logging (below). |
| 1.58.0, and main at ed1f38cd449b (2026-10-01) | affected | Source only. Their `nm-vpn-connection.c` differs from 1.56.1 only in also adding VPN domains as search domains, after the lines cited here. `nm_l3cfg_add_config()` and `nm_l3cfg_commit_on_idle_schedule()` are unchanged. `_l3_commit()` gains a CLAT step but still emits `POST_COMMIT` on every commit. |

## Reproducer

This is a fake plugin with no real tunnel and no secrets. It uses an existing dummy link and documentation addresses from RFC 5737.

Requirements:
- root on a disposable test machine;
- NetworkManager has an active primary connection, that is, one with the default route. Without one, VPN activation fails with "Could not find source connection." (`nm-manager.c:6165-6172`);
- `/usr/bin/python3` with PyGObject (`python3-gobject` on Fedora, `python3-gi` on Debian and Ubuntu).

`/usr/local/libexec/nm-repro-service`:

```python
#!/usr/bin/python3
# Fake NetworkManager VPN plugin. It "connects" by sending Config, Ip4Config
# and StateChanged(STARTED) for an existing dummy link repro0. On SIGUSR1 it
# simulates a plugin-side reconnect: StateChanged(STARTING), then 5 s later
# the same Config and Ip4Config and StateChanged(STARTED) again.
import signal, socket, struct
from gi.repository import Gio, GLib

BUS = "org.freedesktop.NetworkManager.repro"
PATH = "/org/freedesktop/NetworkManager/VPN/Plugin"
IFACE = "org.freedesktop.NetworkManager.VPN.Plugin"
STARTING, STARTED, STOPPED = 3, 4, 6
XML = """<node><interface name="org.freedesktop.NetworkManager.VPN.Plugin">
 <method name="Connect"><arg type="a{sa{sv}}" direction="in"/></method>
 <method name="ConnectInteractive"><arg type="a{sa{sv}}" direction="in"/>
  <arg type="a{sv}" direction="in"/></method>
 <method name="NeedSecrets"><arg type="a{sa{sv}}" direction="in"/>
  <arg type="s" direction="out"/></method>
 <method name="Disconnect"/>
 <property name="State" type="u" access="read"/>
 <signal name="StateChanged"><arg type="u"/></signal>
 <signal name="Config"><arg type="a{sv}"/></signal>
 <signal name="Ip4Config"><arg type="a{sv}"/></signal>
 <signal name="Failure"><arg type="u"/></signal>
</interface></node>"""

loop = GLib.MainLoop()
bus = None
state = 1  # INIT


def emit(name, sig, args):
    bus.emit_signal(None, PATH, IFACE, name, GLib.Variant(sig, args))


def set_state(s):
    global state
    state = s
    print(f"repro: StateChanged({s})", flush=True)
    emit("StateChanged", "(u)", (s,))


def ip4(a):  # NM wants IPv4 addresses as uint32 in network byte order
    return struct.unpack("=I", socket.inet_aton(a))[0]


def send_config():
    emit("Config", "(a{sv})", ({"gateway": GLib.Variant("u", ip4("192.0.2.254")),
                                "tundev": GLib.Variant("s", "repro0"),
                                "has-ip4": GLib.Variant("b", True),
                                "has-ip6": GLib.Variant("b", False)},))
    emit("Ip4Config", "(a{sv})", ({"address": GLib.Variant("u", ip4("192.0.2.1")),
                                   "prefix": GLib.Variant("u", 32),
                                   "never-default": GLib.Variant("b", True)},))
    print("repro: sent Config + Ip4Config", flush=True)
    set_state(STARTED)
    return False


def on_usr1():
    if state == STARTED:
        set_state(STARTING)
        GLib.timeout_add_seconds(5, send_config)
    return True


def on_call(conn, sender, path, iface, method, params, inv):
    print(f"repro: {method}", flush=True)
    if method == "NeedSecrets":
        inv.return_value(GLib.Variant("(s)", ("",)))
        return
    inv.return_value(None)
    if method in ("Connect", "ConnectInteractive"):
        set_state(STARTING)
        GLib.timeout_add_seconds(1, send_config)
    elif method == "Disconnect":
        set_state(STOPPED)
        GLib.timeout_add_seconds(1, loop.quit)


def on_get(conn, sender, path, iface, prop):
    return GLib.Variant("u", state)


def on_bus(conn, name):
    global bus
    bus = conn
    conn.register_object(PATH, Gio.DBusNodeInfo.new_for_xml(XML).interfaces[0],
                         on_call, on_get, None)


Gio.bus_own_name(Gio.BusType.SYSTEM, BUS, Gio.BusNameOwnerFlags.NONE,
                 on_bus, None, lambda *a: loop.quit())
GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR1, on_usr1)
GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, loop.quit)
loop.run()
```

Run as root:

```sh
chmod 755 /usr/local/libexec/nm-repro-service

cat > /etc/dbus-1/system.d/nm-repro.conf <<'EOF'
<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <policy user="root">
    <allow own="org.freedesktop.NetworkManager.repro"/>
    <allow send_destination="org.freedesktop.NetworkManager.repro"/>
  </policy>
</busconfig>
EOF
busctl call org.freedesktop.DBus /org/freedesktop/DBus org.freedesktop.DBus ReloadConfig

cat > /usr/lib/NetworkManager/VPN/nm-repro.name <<'EOF'
[VPN Connection]
name=repro
service=org.freedesktop.NetworkManager.repro
program=/usr/local/libexec/nm-repro-service
supports-multiple-connections=false
EOF
# NetworkManager watches this directory. If activation fails because the
# plugin is not found, restart NetworkManager once.

ip link add repro0 type dummy && ip link set repro0 up
# Without the dummy driver, a veth whose peer sits in an empty namespace works too:
#   ip netns add repro-void
#   ip link add repro0 type veth peer name repro0p netns repro-void
#   ip -n repro-void link set repro0p up; ip link set repro0 up

# ipv4.auto-route-ext-gw needs NM >= 1.42; leave it out on older versions.
nmcli connection add type vpn con-name repro \
    vpn-type org.freedesktop.NetworkManager.repro vpn.data 'unused = 1' \
    connection.autoconnect no ipv4.auto-route-ext-gw no

# Optional: log the internal VPN state and l3cfg commits. l3cfg has no log
# domain of its own; it logs under CORE. KEEP leaves the other domains as they are.
nmcli general logging level KEEP domains VPN:TRACE,CORE:TRACE

nmcli connection up repro              # "Connection successfully activated"
pkill -USR1 -f '^/usr/bin/python3 /usr/local/libexec/nm-repro-service'
                                       # STARTING now; same config + STARTED 5 s later
for i in $(seq 24); do                 # 2 minutes, longer than the 60 s default vpn.timeout
    echo "$(date +%T) $(nmcli -g GENERAL.STATE connection show repro)"; sleep 5
done

# Cleanup
nmcli connection delete repro; ip link del repro0; ip netns del repro-void 2>/dev/null
rm /usr/lib/NetworkManager/VPN/nm-repro.name /etc/dbus-1/system.d/nm-repro.conf
```

What we ran: these steps on NetworkManager 1.46.0 (Ubuntu 24.04 package `1.46.0-1ubuntu2.8`, in a container). NetworkManager was started by hand with debug logging, and firewalld was not running. The script was unchanged apart from three details the container needed:
- the veth alternative shown above, because the container kernel has no dummy driver;
- `#!/usr/bin/python3.12` as the first line, because the container's `/usr/bin/python3` points to a Python without PyGObject;
- `^/usr/bin/python3[.0-9]* ...` as the `pkill` pattern, to match that shebang.

We have not run the reproducer on 1.56.1, on main, or with SELinux enforcing.

## Actual result

- After `SIGUSR1`, `GENERAL.STATE` becomes `activating`, and it stays `activating` after the plugin re-sends `Config`, `Ip4Config` and `STARTED`. In our reproducer run, all 24 polls from 04:36:19 to 04:38:15 printed `activating`.
- No `connect timeout exceeded` warning appears, and NetworkManager logs nothing at info level or above. In the reproducer run this held for 130 s after the config was re-sent, more than twice the 60 s default `vpn.timeout`.
- With VPN debug logging, the log shows `set state: connect (was activated)`, then `set state: ip-config-get (was connect)`, `config4: reply received` and `dbus: state changed: started (4)`. After that there is no further state change and no `pre-up`.
- From the source: the `vpn-pre-up` and `vpn-up` dispatcher events do not run again.
- The VPN leaves this state only if it is deactivated or the plugin reports `STOPPED`, or if an unrelated commit on the tunnel's l3cfg releases it. We saw `nmcli device reapply` do that (see the second issue).

## Expected result

Once the plugin has re-sent its configuration, the VPN should return to `activated`, as the 1.34 code did (source only). At the very least it should fail after `vpn.timeout`. It should not stay `activating` with no end and no log message.

## What we observed

### Reproducer on NetworkManager 1.46.0 (debug logging)

Log prefixes are shortened; the `repro:` lines are the plugin's output.

```
[...178.1484] vpn: dbus: state changed: starting (3)
[...179.7199] vpn: set state: ip-config-get (was connect)
[...179.7203] vpn: dbus: state changed: started (4)
[...179.7210] vpn: set state: pre-up (was ip-config-get)
[...179.7352] vpn: set state: activated (was pre-up)
repro: StateChanged(3)                                   <- SIGUSR1
[...179.7661] vpn: dbus: state changed: starting (3)
[...179.7663] vpn: set state: connect (was activated)
repro: sent Config + Ip4Config
repro: StateChanged(4)
[...184.7228] vpn: config: reply received (IPv4:on(auto), IPv6:on(auto))
[...184.7229] vpn: set state: ip-config-get (was connect)
[...184.7231] vpn: config4: reply received
[...184.7231] vpn: dbus: state changed: started (4)
              (no VPN state change, no warning for 129.7 s)
[...314.4344] vpn: set state: deactivating (was ip-config-get)   <- nmcli connection delete
```

### Our plugin on NetworkManager 1.46.0 (same container, debug logging), with controls

Setup:
- our plugin (nm-sshuttle, in development), which sends the same sequence plus DNS fields and `can-persist`;
- a veth device as `tundev`;
- `ipv4.auto-route-ext-gw no`;
- no firewalld.

```
[...409.0051] vpn: set state: activated (was pre-up)
  plugin: SIGUSR1: simulating a drop (mode identical), reconnecting in 5 s
[...409.0761] vpn: dbus: state changed: starting (3)
[...409.0763] vpn: set state: connect (was activated)
  plugin: Config / Ip4Config identical to the first ones, then STARTED
[...415.8453] vpn: config: reply received (IPv4:on(auto), IPv6:on(auto))
[...415.8456] vpn: set state: ip-config-get (was connect)
[...415.8459] vpn: config4: reply received
[...415.8460] vpn: dbus: state changed: started (4)
              (no VPN state change for 9.2 s)
[...425.0149] vpn: set state: deactivating (was ip-config-get)   <- test deactivated it
```

As a control, we changed only the content of the re-sent `Ip4Config`. The plugin and the timing were the same.
- Sending one extra `Ip4Config` that also contains an `nbns` entry, just before the real one: `ip-config-get` → `pre-up` → `activated` within 3 ms (444.8481 → 444.8510).
- Using a different address (192.0.0.9 instead of 192.0.0.8): `activated` within 4 ms (474.8486 → 474.8522).

### Our plugin on NetworkManager 1.56.1, Fedora 44 (default logging)

Setup:
- Fedora 44, kernel 7.2.8-200.fc44, systemd 259, SELinux enforcing, firewalld and systemd-resolved active;
- the plugin was started through D-Bus activation as a systemd service, not spawned directly by NetworkManager;
- profile with `vpn.persistent yes` and `ipv4.auto-route-ext-gw no`;
- the plugin created a dummy device `nmss0`;
- `Config` contained gateway, `tundev=nmss0`, `has-ip4`, `has-ip6=false` and `can-persist`;
- `Ip4Config` contained 192.0.0.8/32, one DNS server, one domain and `never-default`.

Plugin log. `nmcli connection up` had returned "successfully activated" before the drop.

```
22:46:34 Config: gateway=198.51.100.1 tundev=nmss0 has-ip4=True
22:46:34 Ip4Config: address=192.0.0.8/32 dns=['10.99.0.53'] domains=['corp.test'] never-default=true
22:46:34 state STARTING -> STARTED
22:46:34 SIGUSR1: simulating a drop, reconnecting in 20 s
22:46:34 state STARTED -> STARTING
22:46:55 Config: gateway=198.51.100.1 tundev=nmss0 has-ip4=True
22:46:55 Ip4Config: address=192.0.0.8/32 dns=['10.99.0.53'] domains=['corp.test'] never-default=true
22:46:55 state STARTING -> STARTED
22:47:30 D-Bus call Disconnect            <- the test deactivated the connection
```

- The test polled `nmcli -g GENERAL.STATE` every 0.5 s and recorded only changes. The only state it recorded was `activating`.
- The test deactivated the connection at 22:47:30. That was about 35 s after the config was re-sent and about 56 s after the drop.
- Traffic through the tunnel worked at 22:47:30.
- From 22:46:34.83 to 22:47:30.19, NetworkManager logged nothing at info level or above.
- The window is shorter than the 60 s default `vpn.timeout` counted from the drop, so this run alone does not rule out a timer; the 1.46.0 reproducer run does.
- The journal has no debug lines, so on this system the internal state comes from the source, not the log.

### Our plugin on NetworkManager 1.56.1, Fedora 44 (trace logging)

Same setup as above, in a second run, with `nmcli general logging level KEEP domains VPN:TRACE,CORE:TRACE,DEVICE:DEBUG,DNS:DEBUG`. Prefixes are shortened.

```
08:46:42.348116 vpn: dbus: state changed: starting (3)
08:46:42.348142 vpn: set state: connect (was activated)
08:47:03.344913 vpn: config: reply received (IPv4:on(auto), IPv6:off(auto))
08:47:03.344921 vpn: set state: ip-config-get (was connect)
08:47:03.345275 vpn: config4: reply received
08:47:03.345524 vpn: dbus: state changed: started (4)
08:47:03.348793 vpn: apply-config
                (no "l3cd[...]: set", no "add-config", no l3cfg commit and no VPN state change
                 until trace logging was switched off at 08:47:37.852848)
```

The same plugin in the same run, with one extra `Ip4Config` carrying a sentinel `nbns` entry before the real one (firewalld running):

```
09:02:26.340695 vpn: config: reply received (IPv4:on(auto), IPv6:off(auto))
09:02:26.341546 vpn: l3cd[ip-4]: set 204854c610215add      (sentinel, wins[0]: 192.0.0.10)
09:02:26.341763 vpn: l3cd[ip-4]: set 6b5bda57565c64ea      (real)
09:02:26.341874 vpn: dbus: state changed: started (4)
09:02:26.345838 vpn: apply-config
09:02:26.346025 vpn: l3cd[ip-4]: add-config 6b5bda57565c64ea
09:02:26.346405 l3cfg[...,ifindex=20]: emit signal (post-commit, l3cd-old=(null), l3cd-new=[aea96d88507d5838], l3cd-changed=0)
09:02:26.346413 vpn: set state: pre-up (was ip-config-get)
09:02:26.368245 vpn: set state: activated (was pre-up)
```

## Root cause

Line numbers are for tag 1.56.1 (b829f838fc5d). The cited lines of `nm-vpn-connection.c` are identical on main at ed1f38cd. The `nm-l3cfg.c` line numbers are for 1.56.1 only.

1. **The VPN goes back to CONNECT and no timer is armed.** `StateChanged(STARTING)` after `STARTED` sets `STATE_CONNECT` (`nm-vpn-connection.c:1800-1806`).
   - `connect_timeout_source` is armed only in `connect_success()` (`:1600-1605`).
   - `start_timeout_source` is armed only when the plugin is spawned or its bus name appears (`:2759`, `:2784`). It was cleared when the VPN first reached `ACTIVATED` (`:1053`).
2. **Config moves to IP_CONFIG_GET and the config is applied again.** `Config` moves `CONNECT` → `IP_CONFIG_GET` (`:1953-1954`).
   - `_check_complete()` continues at once, because `l3cds[L3CD_TYPE_IP_4]` from the first activation is still set (`:1438-1445`).
   - It requests the firewall zone change, and the callback calls `_apply_config()` (`:1502-1509`, `:1390-1408`).
3. **`_apply_config()` schedules no commit for equal content.** It sets `wait_for_pre_up_state = TRUE` and calls `_l3cfg_l3cd_update_all()` (`:1384-1386`).
   - With equal content, `_l3cfg_l3cd_set()` keeps the old l3cd object (`:692-693`).
   - `nm_l3cfg_add_config()` then sees the same pointer and parameters and returns FALSE (`nm-l3cfg.c:3595-3601`, `:3692-3697`).
   - `_l3cfg_l3cd_update()` therefore returns before `nm_l3cfg_commit_on_idle_schedule()` (`nm-vpn-connection.c:752-769`, `:776`).
4. **Only a POST_COMMIT leads to PRE_UP.** `_l3cfg_notify_cb()` handles a `POST_COMMIT` from `priv->l3cfg_if ?: priv->l3cfg_dev`, and it is the only place that sets `STATE_PRE_UP` (`:836-845`). From `PRE_UP`, `dispatcher_pre_up_done()` sets `ACTIVATED` (`:947-957`). No commit means no `POST_COMMIT`, so no `PRE_UP`.
5. **STARTED changes nothing.** `StateChanged(STARTED)` only records `service_state` (`:1782`).

The VPN can still leave `ip-config-get` in other ways: the plugin reporting `STOPPED` (`:1784-1799`), deactivation, or an unrelated commit (see the second issue). It does not go forward on its own.

## When it started

We checked this against the git history, using the GitHub mirror of the upstream repository.

- Commit 58287cbcc0c8 ("core: rework IP configuration in NetworkManager using layer 3 configuration"; authored 2021-08-06, committed 2021-11-18):
  - removes from `nm_vpn_connection_apply_config()`:
    ```c
    _LOGI("VPN connection: (IP Config Get) complete");
    if (priv->vpn_state < STATE_PRE_UP)
        _set_vpn_state(self, STATE_PRE_UP, NM_ACTIVE_CONNECTION_STATE_REASON_NONE, FALSE);
    ```
  - adds the `POST_COMMIT` check in `_l3cfg_notify_cb()` and the `wait_for_pre_up_state` flag;
  - also drops the clearing of `ip4_config` and `ip6_config` when `Config` arrives (1.34.0 `nm-vpn-connection.c:1417`, `:1422`). See Related.
- `git describe --contains` places it at 1.35.1-dev. It is an ancestor of 1.36.0 and not of 1.34.0.
- 1.36.0 already has the equality check in `_l3cfg_l3cd_set()` and the same early return before scheduling a commit.

The missing timer during a reconnect is older: 1.34.0 does not arm one in this branch either (1.34.0 `nm-vpn-connection.c:978-985`). Before 1.36, though, the reconnect finished once the new config had been applied (source only).

A side effect of the same commit: the info-level "(IP Config Get) complete" and "(IP Config Get) reply received." messages are gone. In 1.56.1 the only info-level message left in `nm-vpn-connection.c` is "starting ...". At the default log level, a VPN stuck in `ip-config-get` leaves no trace in the journal.

## Second issue: `wait_for_pre_up_state` is never reset

- `wait_for_pre_up_state` is assigned only in `_apply_config()` (`:1384`) and read only in `_l3cfg_notify_cb()` (`:841`). It is never cleared.
- After the first activation it stays TRUE. During a reconnect the VPN is in `CONNECT`, which is below `PRE_UP`.
- Any `POST_COMMIT` on the tunnel's l3cfg would then move the VPN to `pre-up` and `activated`, even before the plugin has re-sent its config or reported `STARTED`.
- `_l3_commit()` emits `POST_COMMIT` on every commit of type above NONE, whether or not anything changed (`nm-l3cfg.c:5436-5437`, `:5466-5471`).
- l3cfg instances are shared per ifindex (`nm_netns_l3cfg_acquire()`), so a commit by the `NMDevice` of the tunnel interface counts too.
- Clients would then show the VPN as connected while the plugin is still reconnecting.

We saw this on 1.56.1 (Fedora 44):
- **`nmcli device reapply` on the tunnel device.** The VPN was stuck in `ip-config-get` as above. `nmcli device reapply nmss0` at 08:47:43.907 released it, and our plugin saw the active connection go `activated` in the same second. Trace logging was already off in that run. A later run (2026-10-09) traced the same step: the reapply scheduled a reapply commit on the tunnel's l3cfg, and its post-commit moved the VPN to `pre-up`:

  ```
  21:17:32.126652 device (nmss0): reapply (version-id 13 (unmodified))
  21:17:32.127170 l3cfg[...,ifindex=8]: commit reapply (idle handler)
  21:17:32.129338 l3cfg[...,ifindex=8]: emit signal (post-commit, l3cd-old=(null), l3cd-new=[5ce16ca446fef800], l3cd-changed=0)
  21:17:32.129383 vpn[...,if:8,dev:2:(nmss0)]: set state: pre-up (was ip-config-get)
  21:17:32.158169 vpn[...,if:8,dev:2:(nmss0)]: set state: activated (was pre-up)
  ```

  From the source, reapply also switches an external device to managed-type FULL (`nm-device.c:14666-14667`, `:14555-14592`, `:4781-4800`).

  With the tunnel device unmanaged through keyfile `unmanaged-devices`, the reapply was refused, and the VPN stayed stuck (both 2026-10-10 runs; the first shown):

  ```
  06:07:42.528310 audit: op="device-reapply" interface="nmss0" ifindex=45 pid=43080 uid=0 result="fail" reason="Device is not activated"
  ```
- **Deleting the tunnel device during a reconnect.** NMManager sets the software device to managed-type REMOVED before it unrealizes the device (`nm-manager.c:4376-4380`). That schedules a commit on the dead ifindex (`nm-device.c:3673-3692`). The VPN's own commit type makes it a real commit, and its post-commit flips the VPN. With trace logging:

```
11:33:08.269069 l3cfg[...,ifindex=32]: commit update (auto) (idle handler)
11:33:08.269360 platform-linux: do-add-ip4-address[32: 192.0.0.8/32]: failure 19 (No such device - ipv4: Device not found)
11:33:08.269390 l3cfg[...,ifindex=32]: emit signal (post-commit, l3cd-old=(null), l3cd-new=[6305d8a26e733c8c], l3cd-changed=0)
11:33:08.269399 vpn[...,if:32,dev:2]: set state: pre-up (was connect)
11:33:08.305765 vpn[...,if:32,dev:2]: set state: activated (was pre-up)
11:33:08.384400 vpn[...,if:32,dev:2]: config: reply received (IPv4:on(auto), IPv6:off(auto))   <- the plugin's Config for the new link
```

  The first 2026-10-10 run saw the same three times: when the plugin deleted and re-created the device (pre-up 121 ms before its new `Config`), when it deleted the device to give up, and when the device went away during the reconnect. Each flip dispatched `vpn-pre-up` on the uplink. In the re-create case and the case where the device went away, NetworkManager also dispatched `vpn-up` there, before it saw any new tunnel device. The re-create trace shows the order:

  ```
  06:08:56.035627 l3cfg[...,ifindex=48]: commit type register (type "none", source "device", existing ...)
  06:08:56.035629 l3cfg[...,ifindex=48]: schedule commit on idle (auto)
  06:08:56.035633 device (nmss0): unrealize (ifindex 48)
  06:08:56.036342 vpn[...,if:48,dev:2]: set state: pre-up (was connect)
  06:08:56.157379 vpn[...,if:48,dev:2]: config: reply received (IPv4:on(auto), IPv6:off(auto))   <- the plugin's Config for the new link
  ```

- Adding and then removing an address on the tunnel device with `ip` did not release the stuck VPN. From the source, a foreign address change is not an l3cfg commit. Commits on the uplink's l3cfg did not release it either.

## Proposed fix

This applies cleanly to 1.56.1, 1.58.0 and main (ed1f38cd), and to 1.36.0 and 1.46.0 with offsets (checked with `patch --dry-run`). It has not been compiled or tested.

```diff
--- a/src/core/vpn/nm-vpn-connection.c
+++ b/src/core/vpn/nm-vpn-connection.c
@@ -1354,6 +1354,7 @@
 _apply_config(NMVpnConnection *self)
 {
     NMVpnConnectionPrivate *priv = NM_VPN_CONNECTION_GET_PRIVATE(self);
+    NML3Cfg                *l3cfg;
 
     _LOGT("apply-config");
 
@@ -1384,6 +1385,15 @@
     priv->wait_for_pre_up_state = TRUE;
 
     _l3cfg_l3cd_update_all(self);
+
+    /* _l3cfg_notify_cb() only moves to PRE_UP on a POST_COMMIT. If the
+     * configuration equals the one already applied (for example when the
+     * plugin reconnects with STARTED -> STARTING -> STARTED and sends the
+     * same Config/Ip4Config again), nothing above schedules a commit.
+     * Request one, so that we always get a POST_COMMIT. */
+    l3cfg = priv->l3cfg_if ?: priv->l3cfg_dev;
+    if (l3cfg)
+        nm_l3cfg_commit_on_idle_schedule(l3cfg, NM_L3_CFG_COMMIT_TYPE_AUTO);
 }
 
 static void
@@ -1799,7 +1809,10 @@
         }
     } else if (new_service_state == NM_VPN_SERVICE_STATE_STARTING
                && old_service_state == NM_VPN_SERVICE_STATE_STARTED) {
-        /* The VPN service got disconnected and is attempting to reconnect */
+        /* The VPN service got disconnected and is attempting to reconnect.
+         * Only the next _apply_config() may move us to PRE_UP again, not
+         * an unrelated commit on the tunnel's l3cfg. */
+        priv->wait_for_pre_up_state = FALSE;
         _set_vpn_state(self,
                        STATE_CONNECT,
                        NM_ACTIVE_CONNECTION_STATE_REASON_CONNECT_TIMEOUT,
```

**Hunk 1 (`_apply_config()`).** It always requests a commit on the same l3cfg that `_l3cfg_notify_cb()` compares against (`priv->l3cfg_if ?: priv->l3cfg_dev`).
- If a commit is already scheduled, `nm_l3cfg_commit_on_idle_schedule()` only merges the request (`nm-l3cfg.c:3408-3417`).
- The VPN registers commit type UPDATE on that l3cfg (`nm-vpn-connection.c:874-875`), so the idle commit is a real commit.
- `_l3_commit()` emits `POST_COMMIT` even when nothing changed (`nm-l3cfg.c:5436-5437`, `:5466-5471`).
- This keeps the current rule that `pre-up` follows a commit.
- Alternative: let `_l3cfg_l3cd_update_all()` report whether it scheduled anything, and enter `PRE_UP` directly when it did not.

**Hunk 2 (reconnect branch).** It fixes the second issue. From reading the source, a firewalld zone call left over from an earlier config update could still run `_apply_config()` after the reconnect has started. Adding `fw_call_cleanup(self)` to the same branch would close that gap. A side effect to weigh *(from source)*: today, a commit that flips a reconnecting VPN to `pre-up` also puts it inside the range where `vpn_connection_state_changed()` removes the VPN's DNS on failure (`nm-policy.c:2678-2683`). With hunk 2, a plugin that gives up from `CONNECT` leaves the VPN's DNS registered. That check may need to cover a VPN that was activated before.

**Optional, a policy decision for maintainers.** NetworkManager does not limit how long a plugin-driven reconnect may take, and 1.34 did not either.
- If a limit is wanted, re-arm `connect_timeout_source` in the reconnect branch.
- `connect_timeout_cb()` already handles `CONNECT` and `IP_CONFIG_GET` (`:1588`), and `_check_complete()` clears the timer (`:1447`).
- This would change behaviour for plugins whose reconnects take longer than `vpn.timeout`.

## Workaround for plugin authors until this is fixed

On reconnect, make the re-sent `Ip4Config` differ in content from the one already applied.

- **Observed working on 1.46.0 and on 1.56.1** (send one extra `Ip4Config` with a sentinel `nbns` entry right before the real one, or change the address).
- **How it works, seen with trace logging on 1.56.1** (one run on 2026-10-09, two on 2026-10-10):
  - `_l3cfg_l3cd_set()` compares only with the previous `Ip4Config` (`:692-693`). The real config after the sentinel is therefore stored as a new object, and `nm_l3cfg_add_config()` schedules a commit.
  - From the source: each `Ip4Config` cancels the pending firewalld zone call and starts a new one. Only a call that completes runs `_apply_config()` (`nm-vpn-connection.c:1390-1408`, `:1497-1509`).
  - With firewalld running, the zone call delays `_apply_config()` by a few milliseconds. Both signals had been processed by then, so only the real config was added and committed, with `l3cd-changed=0`. The sentinel never reached the kernel or D-Bus.
  - Without firewalld, the zone call completes with a "fake success" from an idle callback (`nm-firewalld-manager.c:263`), so the outcome is a race. From the source, `g_idle_add()` runs at idle priority, so the callback waits while a D-Bus signal is already queued. On 2026-10-09 NM applied the sentinel before the second signal: two commits, the WINS server exported for about 1 ms, and `pre-up` entered on the sentinel. On 2026-10-10, in four reconnects over two runs, only the real config was committed:
    - In three, the zone call for `Config` completed first and re-applied the old config. The real `Ip4Config` then cancelled the sentinel's call (the excerpt below is one of them).
    - In one, all three signals were queued before any idle callback ran. The sentinel cancelled the call for `Config`, the real `Ip4Config` cancelled the sentinel's, and only the third call completed (`08:41:55.038665` and `.038717 complete: cancel`, `.038731 complete: fake success`).

    ```
    05:57:29.133880 firewalld: [...,change*:"nmss0"]: firewall zone change nmss0:default (not running, simulate success)   <- for the sentinel
    05:57:29.133903 vpn: config4: reply received                                                                          <- the real config
    05:57:29.133984 firewalld: [...,change*:"nmss0"]: complete: cancel (Request cancelled)
    05:57:29.134247 firewalld: [...,change*:"nmss0"]: complete: fake success
    05:57:29.134343 vpn: l3cd[ip-4]: add-config 48e67264ee069222                                                          <- the real config only
    05:57:29.134502 l3cfg[...,ifindex=30]: emit signal (post-commit, l3cd-old=(null), l3cd-new=[38123bb155f2286d], l3cd-changed=0)
    ```

  - What decides it is whether the sentinel's fake success runs before NM dispatches the real `Ip4Config`. Our bus-monitor times cannot measure the plugin's spacing: in one reconnect, NM had handled an `Ip4Config` 0.18 ms before the monitor's timestamp for it.
  - So a plugin cannot rely on the sentinel being committed, or on it not being committed.
  - Each time the VPN was `activated` 19–40 ms after `Config`, most of it in the pre-up dispatcher call. In the second 2026-10-10 run, almost all of that call was the D-Bus activation of NetworkManager-dispatcher, which had exited after 10 s idle.
- **From the source:** WINS servers do not reach the kernel or resolved's server list. They are read by the D-Bus `IP4Config` object and the dispatcher environment, and they count in the DNS manager's change hash, so a WINS-only change makes NM push the same DNS again.
- **Will not help, per the source:** changes to `mtu` or `banner`, because they are not part of the l3cd.

## Not verified

- The reproducer has been run only on 1.46.0, in a container, with the three small changes described above. It has not been run on 1.56.1, on main, or with SELinux enforcing.
- "Never": the longest observation is 130 s after the config was re-sent (1.46.0). The claim that no timer exists comes from reading the source.
- 1.34.0, and the range from 1.36.0 to 1.45: source only, not run.
- The reapply flip was traced three times (2026-10-09 and both 2026-10-10 runs). The switch to managed-type FULL is from the source.
- The patch has not been compiled or tested.
- Duplicates: one earlier web search found no existing issue. We could not search gitlab.freedesktop.org from our environment, so we may have missed one. The git log of `nm-vpn-connection.c` from 1.36.0 to ed1f38cd contains no fix for this.

## Related

- In a second Fedora 44 test, our plugin deleted and re-created the tunnel device during the reconnect, to force a commit. NetworkManager 1.56.1 then crashed with SIGSEGV in `_check_complete()`, called from `_dbus_signal_ip_config_cb()`. We are reporting that crash separately.
- The same commit introduced a related behaviour (from the source):
  - 1.34.0 cleared `ip4_config` and `ip6_config` whenever `Config` arrived (1.34.0 `nm-vpn-connection.c:1417`, `:1422`), so it waited for a fresh `Ip4Config`.
  - Since 1.36, the IPv4 l3cd from the previous activation is kept. `_check_complete()` therefore proceeds on `Config` alone (`:1438-1445`).
  - A plugin that sends `Config` long before `Ip4Config` during a reconnect could have the old IPv4 config re-applied in the meantime.
  - On 1.56.1 we saw `_check_complete()` proceed on `Config` alone (its `auto-route-ext-gw` debug line follows `config: reply received` before any `config4`). Without firewalld, NM sometimes applied the first `Ip4Config` before it had processed the second (see the workaround). On 2026-10-10, in three reconnects over both runs, it also re-applied the old config 0.25–0.36 ms after `Config`, before the new `Ip4Config` arrived (`05:57:29.133465 firewalld: ... complete: fake success`, `.133470 vpn: apply-config`).
- nm-policy registers a VPN's DNS only on the transition to ACTIVATED (the `FIXME(l3cfg)` at `nm-policy.c:2675`). On 1.56.1 a VPN whose tundev was re-created got its address on the new link but never its DNS. We may report this separately.
