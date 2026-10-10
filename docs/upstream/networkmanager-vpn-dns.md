# VPN: a VPN that fails while its plugin reconnects keeps its DNS entry until NetworkManager restarts (and three related DNS problems)

**Components:** `src/core/nm-policy.c`, `src/core/dns/nm-dns-manager.c`, `src/core/dns/nm-dns-systemd-resolved.c`, `src/core/vpn/nm-vpn-connection.c`

This report has one main issue (1) and three related ones (2–4). They share code and evidence. Issue 4 does not depend on the VPN state machine and could be split into its own report.

Each statement is tagged by where it comes from:
- *(F44)*: observed on Fedora 44 with `NetworkManager-1.56.1-2.fc44`, using our plugin (nm-sshuttle, an out-of-tree VPN plugin in development). This covers spike runs 4 (2026-10-09), 5 and 6 (both 2026-10-10, same VM and boot, with NetworkManager restarted before each run), all with VPN and CORE logging at trace level and DNS, DEVICE, FIREWALL and DISPATCH at debug level. Spike check IDs (R4k, …) and raw file names refer to run 4's report unless a run is named ("run 5, R4k").
- *(1.46.0)*: observed with our plugin on NetworkManager 1.46.0 (Ubuntu 24.04 package `1.46.0-1ubuntu2.8`) in a container, with debug logging.
- *(source)*: read in the source, not run.

## Summary

**1. A VPN that fails from "connect" keeps its DNS entry.** The policy removes a VPN's DNS only when the VPN fails or disconnects from one of the external states ip-config-get through activated (`nm-policy.c:2678-2683`) *(source)*. A VPN that was activated can also fail from "connect":
- A plugin that reconnects on its own sends `StateChanged(STARTING)` after `STARTED`. NetworkManager then moves the VPN back to "connect" (`nm-vpn-connection.c:1800-1806`).
- If the plugin then gives up and sends `Failure` and `StateChanged(STOPPED)`, the VPN fails from "connect".
- The policy does not remove the DNS entry, and nothing else does. The entry stays in the DNS manager until NetworkManager restarts *(source)*.

We saw this *(F44)* in check R4k:
- The trace has no `vpn_connection_update_dns` line after `set state: failed (was connect)`, and the DNS manager reported "DNS configuration did not change".
- The spike then restarted NetworkManager to clear the entry.
- The control, R4l, was identical except that the plugin re-sent its unchanged `Config` before giving up. That moved the VPN to ip-config-get; the entry was removed, and systemd-resolved was updated.
- *(1.46.0)* showed the same difference between the two orders.
- *(F44, run 5)* R4k and R4l gave the same results again.
- *(F44, run 5, R4m)* A plugin killed during its reconnect leaks the same way: `set state: disconnected (was connect)`, then no `vpn_connection_update_dns` (trace-R4m.txt:7, :43-45). NetworkManager's own `Disconnect` call then D-Bus-activates a fresh plugin instance (journal-all.txt:9404, :9445).
- *(F44, run 6)* R4k, R4l and R4m gave the same results again. R8m repeated the kill with the tundev unmanaged through keyfile `unmanaged-devices`, and the entry stayed as well (R8m-d FAIL). So the leak does not depend on the tundev's device.

What the leftover entry does *(source)*:
- It is pushed again on every later DNS update.
- If the tunnel interface still exists, systemd-resolved keeps the VPN's server and domains on it.
- With resolv.conf management, the VPN's name server stays in resolv.conf at the VPN's priority (50).
- If the tunnel interface was deleted, every update sends per-link calls for an ifindex that no longer exists.

*(F44, run 5)* While the tunnel interface still existed, systemd-resolved kept the VPN's server on it (R4k-k: `Link 36 (nmss0): 10.99.0.53`). `Configuration`, rebuilt by `nmcli general reload dns-rc`, still listed the entry, now without an `interface` key. After the plugin deleted the interface, each forced DNS update logged `SetLinkDomains@36 failed: … NoSuchLink` until NetworkManager restarted. In run 4 the plugin deleted the interface at once, and the entry was visible only in NetworkManager's own state.

**2. VPN DNS is registered only on the transition to "activated", and it is filed under the tunnel interface's ifindex.** A plugin that re-creates its tundev during a reconnect gives it a new ifindex.
- *(F44, R4f, runs 4 to 6)* The VPN got its address on the new link but never its DNS. A name lookup in the VPN's domain failed.
- *(source)* The entry under the old ifindex can no longer be removed, because removal looks up the ifindex of the VPN's current configuration. *(F44, runs 5 and 6)* It was still listed after `Disconnect`.

**3. `DnsManager.Configuration` is a snapshot.** NetworkManager builds the property at the end of each DNS update and caches it until the next one (`nm-dns-manager.c:2758-2759`, `:2010-2011`) *(source)*.
- Apart from configuration reloads and a few other events, a DNS update runs only when the DNS manager's change hash changes, and the hash includes no ifindex.
- So the property keeps naming interfaces that have been deleted *(F44, R4k, R4f)*.
- It cannot show issue 1 or 2, unless something else forces an update (`nmcli general reload dns-rc` does). *(F44, runs 5 and 6)* After that reload, the same R4k and R4f entries had no `interface` key.
- It also has no field for the default-route decision of issue 4 *(F44, R4c; in runs 5 and 6 also after a reload)*.

**4. The tunnel interface's own device entry can take systemd-resolved's default route from the uplink.** The tundev is an external `NMDevice`. A commit on its l3cfg that changes the merged content (a post-commit with `l3cd-changed=1`, `nm-device.c:4932-4939`), while the device is in [IP_CONFIG, DEACTIVATING) (`nm-policy.c:2513`), makes the policy file the device's merged configuration as a second, non-VPN DNS entry *(source)*. That configuration includes the VPN's name server and domain, at the VPN's priority (50).
- *(F44)* It followed every such commit on the tundev: the changed address (R4c, runs 4 to 6), the sentinel alone (R4i, runs 4 to 6), and a sentinel committed during a reconnect without firewalld (R4g, run 4). It did not follow a commit with `l3cd-changed=0` (R4b; R4g in runs 5 and 6) or a first activation.
- When no connection had a default route, that entry took the automatic `~` domain away from the uplink (priority 100) *(F44, R4c-o, runs 4 to 6)*.
- systemd-resolved then had no DNS server for the uplink, and queries for public names went through the tunnel to the VPN's DNS server.
- *(source)* The path that runs when a device is activated skips external devices; the path for configuration changes does not.
- *(F44, run 5, R8c)* Control: with the tundev unmanaged through keyfile `unmanaged-devices`, a content-changing commit (`l3cd-changed=1`) gave "DNS configuration did not change" (trace-R8c.txt:76-78). An unmanaged device stays below IP_CONFIG.
- *(F44, run 6, R8j)* A second control: R4i's reconnect with the tundev unmanaged. NetworkManager committed the sentinel alone and then the real config, both with `l3cd-changed=1`, and both gave "DNS configuration did not change" (trace-R8j.txt:64-66, :138-140). R4i, in the same run, filed the copy. R8c gave the same result as in run 5.

## Affected versions

| Version | Issue 1 | Issues 2–4 | Basis |
|---|---|---|---|
| 1.34.0 | affected (source) | not examined | Source only. `vpn_connection_state_changed()` has the same check (1.34.0 `nm-policy.c:2357-2364`, calling `vpn_connection_deactivated()`). The state mapping is the same, and so is the reconnect branch (1.34.0 `nm-vpn-connection.c:978-985`). The DNS manager still worked on `NMIPConfig` objects then, so issues 2–4 were not examined. |
| 1.36.0 | affected (source) | 2, 3: same code (source); 4: not examined | Source only. `nm-policy.c:2320-2328`; the DNS manager files entries by the l3cd's ifindex (`nm-dns-manager.c:1897`) and caches the property (`:2493`). |
| 1.46.0 (Ubuntu 24.04, `1.46.0-1ubuntu2.8`, in a container) | affected | same code (source) | Issue 1 observed with our plugin, debug logging, `dns=none`, no firewalld (excerpt below). Issues 2–4 from the source: `nm-policy.c:2121` and `:2268` have the same asymmetry for external devices; `nm-dns-manager.c:2046` and `:2716` are the same as the lines cited below. |
| 1.52.0 | affected (source) | same code (source) | Source only. `nm-policy.c:2597-2605`, `:2288`, `:2435`. |
| 1.56.1 (Fedora 44, `NetworkManager-1.56.1-2.fc44`) | affected | affected | Observed *(F44, runs 4 to 6)*: R4k and R4l (issue 1), R4m (issue 1, plugin killed, runs 5 and 6), R8m (the same with the tundev unmanaged, run 6), R4f (issue 2, including after `Disconnect` in runs 5 and 6), Configuration dumps in R4k, R4f and R4c (issue 3), R4c-o (issue 4), R8c and R8j (issue 4 controls, runs 5 and 6). |
| 1.58.0, and main at ed1f38cd449b (2026-10-01) | affected (source) | affected (source) | Source only. `nm-dns-manager.c` differs from 1.56.1 only in whitespace and in an assert-only fix at `:374`; all cited line numbers are the same. `nm-policy.c`: 1.58.0 has the same line numbers; on main they are +71. `nm-vpn-connection.c`: +4 after line 2137 on both. `nm-dns-systemd-resolved.c`: 1.58.0 is identical; on main +6 (+15 after line 404). |

## Steps observed, and a reproducer

### Steps observed (our plugin, spike run 4) *(F44)*

Setup:
- Fedora 44 Cloud Edition, kernel 7.2.8-200.fc44, systemd 259 (259.9-1.fc44), SELinux enforcing, firewalld running.
- NetworkManager logged `dns-mgr: init: dns=systemd-resolved rc-manager=unmanaged (auto), plugin=systemd-resolved` (journal-all.txt:19).
- Profile: `vpn.persistent yes` and `ipv4.auto-route-ext-gw no`.
- On each activation the plugin creates a dummy link `nmss0` as its tundev, then sends:
  - `Config`: gateway, `tundev=nmss0`, `has-ip4`, `has-ip6=false`, `can-persist`;
  - `Ip4Config`: 192.0.0.8/32, DNS server 10.99.0.53, domain `corp.test`, `never-default`;
  - `StateChanged(STARTED)`.
- The uplink is `enp0s2`, with DNS server 10.0.2.3.

Checks:
- **R4k (issue 1).** `StateChanged(STARTING)`, then, 20 s later, `Failure(1)` and `StateChanged(STOPPED)`. The plugin deleted `nmss0` once NetworkManager had deactivated the VPN.
- **R4l (control).** The same, except that the plugin re-sent its unchanged `Config` right before `Failure`. NetworkManager had been restarted 25 s before this give-up, to clear R4k's leftover entry.
- **R4f (issue 2).** `StateChanged(STARTING)`. 21 s later the plugin deleted `nmss0` (ifindex 20), created it again (ifindex 21) and waited for NetworkManager's new device. Then it sent `Config`, `Ip4Config` and `STARTED` again.
- **R4c (issue 4).** `StateChanged(STARTING)`, then `Config`, an `Ip4Config` with a different address (192.0.0.9), and `STARTED`. The spike then took the default route away from the uplink with `nmcli device modify enp0s2 ipv4.never-default yes ipv6.never-default yes`, and restored it with `nmcli device reapply enp0s2`.

Before and after each check, the spike read `DnsManager.Configuration` with `busctl get-property` and ran `resolvectl`.

Runs 5 and 6 repeated these checks with the same plugin and setup. Each `Configuration` read ran `nmcli general reload dns-rc` first, and the plugin kept `nmss0` for 5 s after NetworkManager deactivated the VPN. Run 5 added:
- **R4m (issue 1, plugin killed).** `StateChanged(STARTING)`, then, 3 s later, `kill -9` of the plugin.
- **R4p (workaround, tundev gone).** `StateChanged(STARTING)`. 10 s later the plugin deleted `nmss0`. NetworkManager moved the VPN to "activated" (the early activation of our other report), and the plugin sent `STARTED`, then `STARTING`, to move it back to "connect". At its 20 s timer it re-sent the unchanged `Config`, then `Failure(1)` and `StateChanged(STOPPED)`.
- **R8c (issue 4, control).** R4c with `[keyfile]` `unmanaged-devices+=interface-name:nmss0` in a `conf.d` file, applied by restarting NetworkManager.

Run 6 added, with the same `conf.d` file:
- **R8j (issue 4, second control).** R4i with the tundev unmanaged. After `StateChanged(STARTING)` and `Config`, the plugin sent its full `Ip4Config` plus a sentinel WINS server (192.0.0.10), alone, waited for "activated", and then sent the real `Ip4Config` (R4i does the same with `nmss0` managed).
- **R8m (issue 1, plugin killed).** R4m with the tundev unmanaged.
- **R8p (workaround, tundev gone).** R4p with the tundev unmanaged.

### Reproducer (written, NOT run)

**This reproducer has not been run anywhere.** The observations in this report come from our own plugin, not from this script. The expected output below is derived from the source and from those observations.

The reproducer is a fake plugin with no real tunnel and no secrets. It uses an existing dummy link and documentation addresses (RFC 5737). It extends the fake plugin of our report on reconnects that stay "activating" and uses the same file and bus names, so do not install both at once.

Requirements:
- root on a disposable test machine;
- NetworkManager has an active primary connection, that is, one with the default route;
- `/usr/bin/python3` with PyGObject (`python3-gobject` on Fedora, `python3-gi` on Debian and Ubuntu);
- for the `resolvectl` lines, NetworkManager using systemd-resolved (the Fedora default). The `busctl` check works with any `dns=` mode.

`/usr/local/libexec/nm-repro-service`:

```python
#!/usr/bin/python3
# Fake NetworkManager VPN plugin. It "connects" by sending Config, Ip4Config
# (with a DNS server and a domain) and StateChanged(STARTED) for an existing
# dummy link repro0.
#   SIGUSR1: StateChanged(STARTING), as a plugin does when its tunnel drops
#            and it starts to reconnect. NetworkManager moves the VPN back to
#            its internal state "connect".
#   SIGUSR2: give up: Failure(CONNECT_FAILED), then StateChanged(STOPPED).
#            With "giveup = config" in vpn.data it first re-sends the
#            unchanged Config (the control).
import signal, socket, struct
from gi.repository import Gio, GLib

BUS = "org.freedesktop.NetworkManager.repro"
PATH = "/org/freedesktop/NetworkManager/VPN/Plugin"
IFACE = "org.freedesktop.NetworkManager.VPN.Plugin"
STARTING, STARTED, STOPPED = 3, 4, 6  # NMVpnServiceState
CONNECT_FAILED = 1                    # NMVpnPluginFailure
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
state = 1         # INIT
mode = "stopped"  # vpn.data "giveup": "stopped" (the bug) or "config" (the control)


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
    print("repro: sent Config", flush=True)


def connected():
    send_config()
    emit("Ip4Config", "(a{sv})", ({"address": GLib.Variant("u", ip4("192.0.2.1")),
                                   "prefix": GLib.Variant("u", 32),
                                   "dns": GLib.Variant("au", [ip4("192.0.2.53")]),
                                   "domains": GLib.Variant("as", ["repro.test"]),
                                   "never-default": GLib.Variant("b", True)},))
    print("repro: sent Ip4Config", flush=True)
    set_state(STARTED)
    return False


def on_usr1():
    if state == STARTED:
        set_state(STARTING)
    return True


def on_usr2():
    if state != STARTING:
        print("repro: SIGUSR2 ignored, send SIGUSR1 first", flush=True)
        return True
    if mode == "config":
        send_config()  # unchanged; NM leaves "connect" for "ip-config-get"
    print("repro: Failure(1)", flush=True)
    emit("Failure", "(u)", (CONNECT_FAILED,))
    set_state(STOPPED)
    return True


def on_call(conn, sender, path, iface, method, params, inv):
    global mode
    print(f"repro: {method}", flush=True)
    if method == "NeedSecrets":
        inv.return_value(GLib.Variant("(s)", ("",)))
        return
    inv.return_value(None)
    if method in ("Connect", "ConnectInteractive"):
        data = params.unpack()[0].get("vpn", {}).get("data", {})
        mode = data.get("giveup", "stopped")
        print(f"repro: give-up mode: {mode}", flush=True)
        set_state(STARTING)
        GLib.timeout_add_seconds(1, connected)
    elif method == "Disconnect":
        if state != STOPPED:
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
GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR2, on_usr2)
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

# ipv4.auto-route-ext-gw needs NM >= 1.42; leave it out on older versions.
nmcli connection add type vpn con-name repro \
    vpn-type org.freedesktop.NetworkManager.repro vpn.data 'giveup = stopped' \
    connection.autoconnect no ipv4.auto-route-ext-gw no

# Optional: log the VPN states and the DNS manager's decisions.
nmcli general logging level KEEP domains VPN:TRACE,DNS:DEBUG

dns_cfg() {
    busctl get-property org.freedesktop.NetworkManager /org/freedesktop/NetworkManager/DnsManager \
        org.freedesktop.NetworkManager.DnsManager Configuration
}
show() {  # show TITLE
    echo "== $1"
    echo "-- Configuration (as NetworkManager last built it):"; dns_cfg
    nmcli general reload dns-rc   # makes NetworkManager rebuild the property
    echo "-- Configuration after 'nmcli general reload dns-rc':"; dns_cfg
    echo "-- systemd-resolved:"; resolvectl dns repro0; resolvectl domain repro0
}
plugin='^/usr/bin/python3 /usr/local/libexec/nm-repro-service'

run() {  # run MODE: "config" (control) or "stopped" (the bug)
    nmcli connection modify repro vpn.data "giveup = $1"
    nmcli connection up repro                      # "Connection successfully activated"
    show "$1: VPN activated"
    pkill -USR1 -f "$plugin"; sleep 2              # STARTING: internal VPN state "connect"
    nmcli -g GENERAL.STATE connection show repro   # activating
    pkill -USR2 -f "$plugin"; sleep 2              # [Config,] Failure, STOPPED
    nmcli -g GENERAL.STATE connection show repro   # (empty: the VPN is gone)
    show "$1: after the give-up"
}

run config    # control first: it leaves nothing behind
run stopped   # the bug: the VPN's entry stays

# Optional, the situation of our spike run: the tundev is deleted afterwards.
ip link del repro0
nmcli general reload dns-rc; dns_cfg
journalctl -u NetworkManager --since -1min | grep NoSuchLink

# Cleanup. Only a restart drops the leftover entry.
nmcli connection delete repro; ip link del repro0 2>/dev/null
rm /usr/lib/NetworkManager/VPN/nm-repro.name /etc/dbus-1/system.d/nm-repro.conf
systemctl restart NetworkManager
```

`nmcli general reload dns-rc` makes NetworkManager run its DNS update, which rebuilds the cached `Configuration` property (`nm-manager.c:1782-1783`, `nm-dns-manager.c:2689-2699`, `:2010-2011`) *(source)*. Without it, the property can show an old state (issue 3).

Expected output, derived from the source and from our plugin's runs (**not run**):

| Step | `run config` (control) | `run stopped` (the bug) |
|---|---|---|
| VPN activated | Configuration has `"nameservers" as 1 "192.0.2.53" "domains" as 1 "repro.test" "interface" s "repro0" "priority" i 50 "vpn" b true` next to the uplink's entry. `resolvectl dns repro0` shows 192.0.2.53. | Same. |
| After the give-up, before the reload | The VPN's entry is gone (removing it ran a DNS update, which rebuilt the property). | Unchanged from "VPN activated" (the snapshot). |
| After the give-up, after `reload dns-rc` | Only the uplink's entry. `resolvectl dns repro0` shows no server. | The VPN's entry is still there, on `repro0`. `resolvectl dns repro0` still shows 192.0.2.53 and `resolvectl domain repro0` still shows `repro.test`. |
| Optional step (`repro0` deleted) | n/a | The entry is still listed, now without an `interface` key (`nm-dns-manager.c:2812-2818`). The journal has at least one `send-updates …@N failed: … NoSuchLink` line. |

A third entry for `repro0` with `"vpn" b false` may also appear (issue 4). That does not change the result.

## Actual result

1. **Issue 1.**
   - *(F44, R4k)* The VPN failed from "connect". NetworkManager did not call the policy's DNS removal, and the DNS manager reported "did not change".
   - `DnsManager.Configuration` still listed the VPN entry, but that dump is only the cached snapshot (issue 3).
   - The spike restarted NetworkManager 4 s later to clear the entry. That NetworkManager had been running for about 9.5 minutes.
   - *(F44, R4l)* With the re-sent `Config`, the VPN failed from ip-config-get, the entry was removed, and systemd-resolved reset `nmss0`.
   - *(1.46.0)* The same two orders gave the same difference in the debug log.
   - *(F44, run 5, R4k)* The same again. While `nmss0` existed, systemd-resolved still had the VPN's server on it (R4k-k FAIL). The rebuilt `Configuration` still listed the entry. NetworkManager was restarted 11 s after the give-up.
   - *(F44, run 5, R4m)* After the kill, the VPN went from "connect" to "disconnected", and the entry stayed (R4m-d FAIL).
   - *(F44, run 6)* R4k, R4l and R4m gave the same results. *(F44, run 6, R8m)* With the tundev unmanaged, the kill left the entry too (R8m-d FAIL).
2. **Issue 2.** *(F44, R4f)* After the tundev was re-created, NetworkManager activated the VPN on the old ifindex 20 (the link was already gone) and then moved it to ifindex 21. No DNS update followed.
   - systemd-resolved's last update for `nmss0` was at the first activation.
   - `resolvectl dns nmss0` lacked 10.99.0.53 (R4f-d FAIL), and `git.corp.test` did not resolve (R4f-q FAIL). Traffic through the tunnel worked (R4f-t PASS).
   - *(F44, run 5)* The same, with the old ifindex 48. The entry for it was still there after `Disconnect`.
   - *(F44, run 6)* The same, with the old ifindex 79 (R4f-d, R4f-q FAIL). The entry was still there after `Disconnect` (R4f-z).
3. **Issue 3.** *(F44)* The R4k dump after the give-up and the R4f dump after the reconnect both list `"interface" s "nmss0"` for an entry whose link (ifindex 17 in R4k, 20 in R4f) no longer existed.
   - Because of that, the spike's check R4f-n ("exactly one VPN entry, on nmss0") passed.
   - In R4c the dumps before and after the default-route change are byte-identical.
   - *(F44, runs 5 and 6)* Rebuilt by `nmcli general reload dns-rc`, the R4k and R4f dumps have no `interface` key for that entry, and R4f-n FAILED. The rebuilt R4c dumps are still byte-identical.
4. **Issue 4.** *(F44, R4c, R4c-o)* After the content-changing reconnect, the DNS manager had three entries: the VPN's, a non-VPN copy for `nmss0` at priority 50, and the uplink's.
   - With no default route on the uplink, systemd-resolved set `nmss0` as default route and reset the uplink's DNS server list.
   - Queries for `fedoraproject.org` reached the internal DNS server through the tunnel.
   - *(F44, run 5)* R4c and R4c-o gave the same result, and R4i filed the copy too. R4g did not, because its only commit had `l3cd-changed=0`.
   - *(F44, run 5, R8c)* With `nmss0` unmanaged, the same reconnect left two entries, and systemd-resolved kept the uplink as default route, with its server.
   - *(F44, run 6)* R4c, R4c-o and R4i as in run 5. R4g again had a single commit on `nmss0`, with `l3cd-changed=0`, and no copy. With `nmss0` unmanaged, R8c and R8j each left two entries, and systemd-resolved kept the uplink as default route, with its server.

## Expected result

- When a VPN fails or disconnects, NetworkManager removes every DNS entry it registered for that VPN, whatever state the VPN fails from. It already does this for a VPN that fails from ip-config-get through activated.
- A VPN's DNS follows its configuration and its tunnel interface while it is activated, as its address and routes do.
- `DnsManager.Configuration` shows what the DNS manager currently holds.
- A VPN's tunnel interface does not get a second, non-VPN DNS entry that competes with other connections for the default DNS route.

## What we observed

Log prefixes are shortened; the message text is unchanged.
- Journal lines lose the date and the host name.
- NetworkManager lines also lose the process name, the log level (kept on the `<warn>` lines of systemd-resolved calls) and NetworkManager's own timestamp. In the 1.46.0 excerpts, which come from a log without journal fields, only NetworkManager's timestamp is kept.
- `vpn[0x…,<uuid>,"nmss-spike",…]` is shortened to `vpn[…,if:N,…]`, and the `l3cfg[…]` and `dns-sd-resolved[…]` instance IDs to `…`.

### Issue 1: give-up from "connect" (R4k) *(F44)*

busmon-R4k.txt: the plugin's signals after the drop were `StateChanged` 3 at 21:24:13.926319, `Failure` 1 at 21:24:34.639683 and `StateChanged` 6 at 21:24:34.639786. It sent no `Config`.

trace-R4k.txt:14-15, 18-20, 56-58, 96, 102-106:

```
21:24:13.926224 vpn[…,if:17,dev:2:(nmss0)]: dbus: state changed: starting (3)
21:24:13.926261 vpn[…,if:17,dev:2:(nmss0)]: set state: connect (was activated)
21:24:34.639997 vpn[…,if:17,dev:2:(nmss0)]: dbus: failure: connect-failed (1)
21:24:34.640047 vpn[…,if:17,dev:2:(nmss0)]: dbus: state changed: stopped (6)
21:24:34.640058 vpn[…,if:17,dev:2:(nmss0)]: set state: failed (was connect)
21:24:34.644495 dns-mgr: (device_l3cd_changed): queueing DNS updates (1)
21:24:34.644516 dns-mgr: (device_l3cd_changed): DNS configuration did not change
21:24:34.644520 dns-mgr: (device_l3cd_changed): no DNS changes to commit (0)
21:24:34.774707 device (nmss0): state change: activated -> unmanaged (reason 'unmanaged', managed-type: 'removed')
21:24:34.775415 dns-mgr: (update_routing_and_dns): queueing DNS updates (1)
21:24:34.775466 dns-mgr: (update_routing_and_dns): DNS configuration did not change
21:24:34.775470 dns-mgr: (update_routing_and_dns): no DNS changes to commit (0)
```

- trace-R4k.txt has no `vpn_connection_update_dns` line and no `committing DNS changes` line. Trace logging stayed on until 21:24:38.457.
- The DNS manager's change hash covers every entry (`nm-dns-manager.c:1245-1275`). If the VPN's entry had been removed, `update_routing_and_dns` would have reported a change, as it did in R4l (below).
- reconnect-R4k.txt:5 (after activation) and :8 (after the give-up) are the same line:

```
aa{sv} 2 5 "nameservers" as 1 "10.99.0.53" "domains" as 1 "corp.test" "interface" s "nmss0" "priority" i 50 "vpn" b true 4 "nameservers" as 1 "10.0.2.3" "interface" s "enp0s2" "priority" i 100 "vpn" b false
```

- reconnect-R4k.txt:10-13. systemd-resolved no longer had `nmss0`, because the plugin had deleted it (R4k-r PASS):

```
$ resolvectl dns
Global:
Link 2 (enp0s2): 10.0.2.3
Link 4 (h-j):
```

- R4k-d ("NetworkManager dropped the VPN's DNS entry after the give-up") FAILED.
- R4k-z: the spike restarted NetworkManager (journal-all.txt:5989 `Stopping NetworkManager.service - Network Manager...` at 21:24:38, :6005 `NetworkManager (version 1.56.1-2.fc44) is starting... (after a restart, …)`).

### Issue 1, control: Config re-sent before the give-up (R4l) *(F44)*

busmon-R4l.txt: `StateChanged` 3 at 21:24:42.813772, then `Config` (gateway, `tundev` `nmss0`, `has-ip4` true, `has-ip6` false, `can-persist` true) at 21:25:03.638840, `Failure` 1 at .639137 and `StateChanged` 6 at .639163.

trace-R4l.txt:13-14, 17-18, 21-23, 26-27, 30-35:

```
21:24:42.814001 vpn[…,if:18,dev:2:(nmss0)]: dbus: state changed: starting (3)
21:24:42.814013 vpn[…,if:18,dev:2:(nmss0)]: set state: connect (was activated)
21:25:03.639068 vpn[…,if:18,dev:2:(nmss0)]: config: reply received (IPv4:on(auto), IPv6:off(auto))
21:25:03.639091 vpn[…,if:18,dev:2:(nmss0)]: set state: ip-config-get (was connect)
21:25:03.639628 vpn[…,if:18,dev:2:(nmss0)]: dbus: failure: connect-failed (1)
21:25:03.639789 vpn[…,if:18,dev:2:(nmss0)]: dbus: state changed: stopped (6)
21:25:03.639795 vpn[…,if:18,dev:2:(nmss0)]: set state: failed (was ip-config-get)
21:25:03.639947 dns-mgr: (vpn_connection_update_dns): queueing DNS updates (1)
21:25:03.639954 dns-mgr: (update_routing_and_dns): queueing DNS updates (2)
21:25:03.640016 dns-mgr: (update_routing_and_dns): DNS configuration changed
21:25:03.640018 dns-mgr: (update_routing_and_dns): no DNS changes to commit (1)
21:25:03.640022 dns-mgr: (vpn_connection_update_dns): DNS configuration changed
21:25:03.640025 dns-mgr: (vpn_connection_update_dns): committing DNS changes (0)
21:25:03.640030 dns-mgr: update-dns: not updating resolv.conf
21:25:03.640049 dns-mgr: update-dns: updating plugin systemd-resolved
```

journal-all.txt:6255-6256:

```
21:25:03 systemd-resolved[557]: nmss0: Bus client reset search domain list.
21:25:03 systemd-resolved[557]: nmss0: Bus client reset DNS server list.
```

reconnect-R4l.txt:5 is the same as R4k's line above. reconnect-R4l.txt:8, after the give-up:

```
aa{sv} 1 4 "nameservers" as 1 "10.0.2.3" "interface" s "enp0s2" "priority" i 100 "vpn" b false
```

### Issue 1 on NetworkManager 1.46.0 *(1.46.0)*

Container setup:
- NetworkManager started by hand with debug logging, `dns=none`, `rc-manager=unmanaged`, no firewalld;
- the same plugin with a 5 s reconnect delay, and a veth as tundev.

The excerpts come from the container's NetworkManager log, which is not part of the spike report. Prefixes are shortened as above.

Give-up from "connect" (`Failure`, then `STOPPED`), followed by the DNS manager's next lines:

```
[1791552654.6684] vpn[…,if:12,dev:5:(nmss0)]: set state: connect (was activated)
[1791552660.1820] vpn[…,if:12,dev:5:(nmss0)]: dbus: failure: connect-failed (1)
[1791552660.1821] vpn[…,if:12,dev:5:(nmss0)]: dbus: state changed: stopped (6)
[1791552660.1821] vpn[…,if:12,dev:5:(nmss0)]: set state: failed (was connect)
[1791552660.1831] dns-mgr: (device_l3cd_changed): queueing DNS updates (1)
[1791552660.1831] dns-mgr: (device_l3cd_changed): DNS configuration did not change
[1791552660.1831] dns-mgr: (device_l3cd_changed): no DNS changes to commit (0)
```

The next `vpn_connection_update_dns` line in that log belongs to the control, 18 s later:

```
[1791552678.1825] vpn[…,if:13,dev:5:(nmss0)]: set state: ip-config-get (was connect)
[1791552678.1828] vpn[…,if:13,dev:5:(nmss0)]: dbus: failure: connect-failed (1)
[1791552678.1828] vpn[…,if:13,dev:5:(nmss0)]: dbus: state changed: stopped (6)
[1791552678.1828] vpn[…,if:13,dev:5:(nmss0)]: set state: failed (was ip-config-get)
[1791552678.1830] dns-mgr: (vpn_connection_update_dns): queueing DNS updates (1)
[1791552678.1832] dns-mgr: (vpn_connection_update_dns): DNS configuration changed
[1791552678.1832] dns-mgr: (vpn_connection_update_dns): committing DNS changes (0)
```

With `dns=none`, NetworkManager configured no resolver, and we took no `Configuration` dump in the container.

### Issue 1, run 5: rebuilt configuration and systemd-resolved (R4k) *(F44)*

trace-R4k.txt:6-8 and :44-46 (run 5):

```
06:02:03.627644 vpn[…,if:36,dev:2:(nmss0)]: dbus: failure: connect-failed (1)
06:02:03.627716 vpn[…,if:36,dev:2:(nmss0)]: dbus: state changed: stopped (6)
06:02:03.627726 vpn[…,if:36,dev:2:(nmss0)]: set state: failed (was connect)
06:02:03.630576 dns-mgr: (device_l3cd_changed): queueing DNS updates (1)
06:02:03.630589 dns-mgr: (device_l3cd_changed): DNS configuration did not change
06:02:03.630592 dns-mgr: (device_l3cd_changed): no DNS changes to commit (0)
```

- trace-R4k.txt has no `vpn_connection_update_dns` line. The plugin deleted `nmss0` at 06:02:08, and that pushed nothing either (trace-R4k.txt:98, `update_routing_and_dns: DNS configuration did not change`).
- reconnect-R4k.txt:7-8, while `nmss0` still existed (R4k-k FAIL):

```
$ resolvectl dns nmss0
Link 36 (nmss0): 10.99.0.53
```

- reconnect-R4k.txt:11, after `nmss0` was deleted, rebuilt by `nmcli general reload dns-rc`. The VPN's entry is still there, without an `interface` key:

```
aa{sv} 2 4 "nameservers" as 1 "10.99.0.53" "domains" as 1 "corp.test" "priority" i 50 "vpn" b true 4 "nameservers" as 1 "10.0.2.3" "interface" s "enp0s2" "priority" i 100 "vpn" b false
```

- journal-all.txt:8049, :8052, :8055. Each line followed one of the spike's `nmcli general reload dns-rc` calls (`audit: op="reload" arg="2"`). Without such a forced update, NetworkManager logged nothing about the entry.

```
06:02:11 <warn> dns-sd-resolved[…]: send-updates SetLinkDomains@36 failed: GDBus.Error:org.freedesktop.resolve1.NoSuchLink: Link 36 not known
06:02:12 <warn> dns-sd-resolved[…]: send-updates SetLinkDomains@36 failed: GDBus.Error:org.freedesktop.resolve1.NoSuchLink: Link 36 not known
06:02:13 <warn> dns-sd-resolved[…]: send-updates SetLinkDomains@36 failed: GDBus.Error:org.freedesktop.resolve1.NoSuchLink: Link 36 not known
```

- The spike restarted NetworkManager at 06:02:14. R4l then gave the same result as in run 4.

### Issue 1, plugin killed (R4m) *(F44, run 5)*

The plugin was killed 3 s after `StateChanged(STARTING)`. trace-R4m.txt:6-7 and :43-45:

```
06:03:45.624247 vpn[…,if:40,dev:2:(nmss0)]: dbus: name owner for org.freedesktop.NetworkManager.sshuttle disappeared
06:03:45.624280 vpn[…,if:40,dev:2:(nmss0)]: set state: disconnected (was connect)
06:03:45.626749 dns-mgr: (device_l3cd_changed): queueing DNS updates (1)
06:03:45.626762 dns-mgr: (device_l3cd_changed): DNS configuration did not change
06:03:45.626765 dns-mgr: (device_l3cd_changed): no DNS changes to commit (0)
```

- trace-R4m.txt has no `vpn_connection_update_dns` line. NetworkManager did remove the address and the firewalld zone.
- journal-all.txt:9404, :9445. NetworkManager's `Disconnect` call D-Bus-activated a new plugin instance in the same second:

```
06:03:45 systemd[1]: nm-sshuttle-spike.service: Main process exited, code=killed, status=9/KILL
06:03:45 systemd[1]: Starting nm-sshuttle-spike.service - nm-sshuttle spike: NetworkManager VPN plugin...
```

- reconnect-R4m.txt:5, rebuilt after the kill, still has the VPN's entry on `nmss0` (R4m-d FAIL).
- The spike deleted `nmss0` at 06:03:55 (journal-all.txt:9461). The next forced update logged `SetLinkDomains@40 failed: … NoSuchLink` (:9464, 06:03:55.7639), and the spike restarted NetworkManager at 06:03:56.

### Issue 1, plugin killed, tundev unmanaged (R8m) *(F44, run 6)*

R4m with `[keyfile]` `unmanaged-devices+=interface-name:nmss0` (R8: "NetworkManager keeps both nmss0 and h-j unmanaged"; trace-R8m.txt:53 below). The plugin was killed 3 s after `StateChanged(STARTING)`. trace-R8m.txt:6-7, :10-11, :42-45, :53:

```
08:54:23.565566 vpn[…,if:77,dev:2:(nmss0)]: dbus: name owner for org.freedesktop.NetworkManager.sshuttle disappeared
08:54:23.565582 vpn[…,if:77,dev:2:(nmss0)]: set state: disconnected (was connect)
08:54:23.565731 vpn[…,if:77,dev:2:(nmss0)]: dbus: call Disconnect on org.freedesktop.NetworkManager.sshuttle
08:54:23.565755 firewalld: […,remove:"nmss0"]: firewall zone remove nmss0:default
08:54:23.567732 l3cfg[…,ifindex=77]: emit signal (post-commit, l3cd-old=[310c07f1572167f0], l3cd-new=(null), l3cd-changed=1)
08:54:23.567737 dns-mgr: (device_l3cd_changed): queueing DNS updates (1)
08:54:23.567747 dns-mgr: (device_l3cd_changed): DNS configuration did not change
08:54:23.567750 dns-mgr: (device_l3cd_changed): no DNS changes to commit (0)
08:54:23.567933 manager: (nmss0): assume: don't assume because device is not managed
```

- trace-R8m.txt has no `vpn_connection_update_dns` line. NetworkManager removed the address (trace-R8m.txt:34-36) and the firewalld zone, as in R4m. It generated no profile for `nmss0` and did not assume it.
- reconnect-R8m.txt:5, rebuilt after the kill by `nmcli general reload dns-rc` (journal-all.txt:18293, 08:54:31.617095), still has the VPN's entry on `nmss0` (R8m-d FAIL). It is byte-identical to run 6's reconnect-R4m.txt:5:

```
aa{sv} 2 5 "nameservers" as 1 "10.99.0.53" "domains" as 1 "corp.test" "interface" s "nmss0" "priority" i 50 "vpn" b true 4 "nameservers" as 1 "10.0.2.3" "interface" s "enp0s2" "priority" i 100 "vpn" b false
```

- The spike deleted `nmss0` at about 08:54:33.6. The next forced update logged `SetLinkDomains@77 failed: … NoSuchLink` (journal-all.txt:18301, 08:54:33.667928), and the spike restarted NetworkManager at 08:54:34.679 (:18302).
- systemd-resolved logged no reset for `nmss0` after the kill. The spike captured no `resolvectl` output between the kill and the deletion, and its `reload dns-rc` calls in that window push the leaked entry again, so systemd-resolved's state then is not observed.

### Issue 2: a re-created tundev gets no DNS (R4f) *(F44)*

Halfway through the gap, systemd-resolved still had the server on the old link (reconnect-R4f.txt:4-5):

```
$ resolvectl dns nmss0
Link 20 (nmss0): 10.99.0.53
```

trace-R4f.txt:20, 75, 78, 86, 89, 93-94, 111, 122-123, 173, 180, 222-224, 312.
- Deleting the old link caused a commit on the dead ifindex. That commit moved the VPN to pre-up and activated before the plugin's new `Config` arrived (R4f-v FAIL). This is the early activation described in our report on reconnects that stay "activating".
- The policy registered the VPN's DNS again, still on ifindex 20.
- Then the `Config` for the new link moved the VPN to ifindex 21.
- The `device_l3cd_changed` call for ifindex 21 came while the new device was still unmanaged; the device became externally activated only later (trace-R4f.txt:305).

```
21:25:18.963724 vpn[…,if:20,dev:2:(nmss0)]: set state: connect (was activated)
21:25:40.227155 platform-linux: do-add-ip4-address[20: 192.0.0.8/32]: failure 19 (No such device - ipv4: Device not found)
21:25:40.227292 vpn[…,if:20,dev:2]: set state: pre-up (was connect)
21:25:40.266114 vpn[…,if:20,dev:2]: set state: activated (was pre-up)
21:25:40.266425 dns-mgr: (vpn_connection_update_dns): queueing DNS updates (1)
21:25:40.266499 dns-mgr: (vpn_connection_update_dns): DNS configuration did not change
21:25:40.266503 dns-mgr: (vpn_connection_update_dns): no DNS changes to commit (0)
21:25:40.267775 manager: (nmss0): new Dummy device (/org/freedesktop/NetworkManager/Devices/5)
21:25:40.345900 vpn[…,if:20,dev:2]: config: reply received (IPv4:on(auto), IPv6:off(auto))
21:25:40.345939 vpn[…,if:20,dev:2]: set ip-ifindex-if 21
21:25:40.346870 vpn-config: nameserver4[0]: 10.99.0.53
21:25:40.351337 vpn[…,if:21,dev:2:(nmss0)]: apply-config
21:25:40.352213 l3cfg[…,ifindex=21]: emit signal (post-commit, l3cd-old=(null), l3cd-new=[a21a7185f2c8e2d0], l3cd-changed=1)
21:25:40.352226 dns-mgr: (device_l3cd_changed): queueing DNS updates (1)
21:25:40.352253 dns-mgr: (device_l3cd_changed): DNS configuration did not change
21:25:40.358526 dns-mgr: (device_state_changed): DNS configuration did not change
```

- trace-R4f.txt contains no `committing DNS changes` line. DNS debug logging ran from 21:25:18.949 until 21:25:46.167, just before the test deactivated the VPN.
- systemd-resolved's last update for `nmss0` is journal-all.txt:6634, from the first activation:

```
21:25:18 systemd-resolved[557]: nmss0: Bus client set DNS server list to: 10.99.0.53
```

- Results: R4f-d (`nmss0` still has 10.99.0.53) FAIL, R4f-q (`git.corp.test` resolves through the tunnel) FAIL, R4f-t (traffic flows) PASS.
- R4f-n PASSED only because of the snapshot (next section).

Run 5 *(F44)* gave the same result. Deleting the old link (ifindex 48) moved the VPN to pre-up 121 ms before the plugin's new `Config` (trace-R4f.txt:82 at 06:08:56.036342, :126 at .157379), and R4f-d and R4f-q FAILED again. reconnect-R4f.txt:11 (rebuilt after the reconnect) and :14 (rebuilt after `Disconnect` at 06:09:05) are the same line. The VPN's entry has no `interface` key:

```
aa{sv} 2 4 "nameservers" as 1 "10.99.0.53" "domains" as 1 "corp.test" "priority" i 50 "vpn" b true 4 "nameservers" as 1 "10.0.2.3" "interface" s "enp0s2" "priority" i 100 "vpn" b false
```

- Each forced DNS update logged `SetLinkDomains@48 failed: … NoSuchLink`: at 06:09:02 to 06:09:04, after the reconnect, and at 06:09:16 to 06:09:18, after `Disconnect` (journal-all.txt:15219, :15260, :15292, :15347, :15352, :15355). The spike restarted NetworkManager at 06:09:19.
- R4f-n FAILED, and the spike's count of entries without an interface after `Disconnect` was 1 (R4f-z).

Run 6 *(F44)* gave the same result again. Deleting the old link (ifindex 79) moved the VPN to pre-up 112 ms before the plugin's new `Config` (trace-R4f.txt:84 at 08:55:04.933507, :128 at 05.045584). R4f-d, R4f-q and R4f-n FAILED. reconnect-R4f.txt:11 and :14 are again the line above. Forced updates logged `SetLinkDomains@79 failed: … NoSuchLink` after the reconnect and after `Disconnect` (journal-all.txt:19285, :19414), and R4f-z counted 1 entry without an interface.

### Issue 3: `DnsManager.Configuration` is a snapshot *(F44)*

- **R4k.** reconnect-R4k.txt:8 lists `"interface" s "nmss0"` for the VPN entry. The dump was taken about 4 s after `nmss0` (ifindex 17) had been deleted (trace-R4k.txt:81, link removed at 21:24:34.772516). No DNS update ran in between.
- **R4f.** reconnect-R4f.txt:11, taken after the reconnect, lists the VPN entry as `"interface" s "nmss0"`:

```
aa{sv} 2 5 "nameservers" as 1 "10.99.0.53" "domains" as 1 "corp.test" "interface" s "nmss0" "priority" i 50 "vpn" b true 4 "nameservers" as 1 "10.0.2.3" "interface" s "enp0s2" "priority" i 100 "vpn" b false
```

  From the source, that entry is filed under ifindex 20, which no longer existed. A rebuilt property would have no `interface` key for it (`nm-dns-manager.c:2812-2818`). The last DNS update before the dump was the one at the first activation (journal-all.txt:6628-6634); trace-R4f.txt has none after it. The spike's check R4f-n ("exactly one VPN entry, on nmss0") passed on this stale line.
- **R4c.** reconnect-R4c.txt:11 and :17 are byte-identical, although systemd-resolved's default route moved from `enp0s2` to `nmss0` between them (next section). A DNS update did run in between (trace-R4c.txt:218-221); the property has no field for the default-route decision.
- **Runs 5 and 6.** Each dump was taken right after `nmcli general reload dns-rc`. The R4k and R4f entries then had no `interface` key (above; run 6 reconnect-R4k.txt:11), so the stale name is a property of the cache, not of the entries. R4c's two dumps (reconnect-R4c.txt:11, :34 in both runs) were again byte-identical across the default-route change.

### Issue 4: the tundev's device entry takes the default route (R4c, R4c-o) *(F44)*

trace-R4c.txt:24, 55, 57-60, 86-87, 90-91, 97, 111, 120. The reconnect's `Ip4Config` changed the address from 192.0.0.8 to 192.0.0.9. The shared l3cfg of `nmss0` committed a merged configuration that includes the VPN's name server, domain and DNS priority. Its post-commit led to a DNS change through `device_l3cd_changed`, not through the VPN:

```
21:20:58.290052 vpn[…,if:12,dev:2:(nmss0)]: set state: ip-config-get (was connect)
21:20:58.295353 l3cfg[…,ifindex=12]: IP configuration changed (merged=>[9dded741d7a06d38], commited=>[9dded741d7a06d38])
21:20:58.295370 l3cfg[…,ifindex=12]:    address4[0]: 192.0.0.9/32 lft forever pref forever lifetime 363-0[0,0] dev 12 src vpn
21:20:58.295374 l3cfg[…,ifindex=12]:    dns-priority4: 50
21:20:58.295379 l3cfg[…,ifindex=12]:    nameserver4[0]: 10.99.0.53
21:20:58.295383 l3cfg[…,ifindex=12]:    domain4[0]: corp.test
21:20:58.296026 l3cfg[…,ifindex=12]: emit signal (post-commit, l3cd-old=[aaad78273ff9d0fe], l3cd-new=[9dded741d7a06d38], l3cd-changed=1)
21:20:58.296033 dns-mgr: (device_l3cd_changed): queueing DNS updates (1)
21:20:58.296074 dns-mgr: (device_l3cd_changed): DNS configuration changed
21:20:58.296078 dns-mgr: (device_l3cd_changed): committing DNS changes (0)
21:20:58.297338 vpn[…,if:12,dev:2:(nmss0)]: set state: pre-up (was ip-config-get)
21:20:58.325479 vpn[…,if:12,dev:2:(nmss0)]: set state: activated (was pre-up)
21:20:58.325889 dns-mgr: (vpn_connection_update_dns): DNS configuration did not change
```

reconnect-R4c.txt:10-28, after the reconnect and then after taking the uplink's default route away:

```
$ nm_dns_config
aa{sv} 3 5 "nameservers" as 1 "10.99.0.53" "domains" as 1 "corp.test" "interface" s "nmss0" "priority" i 50 "vpn" b true 5 "nameservers" as 1 "10.99.0.53" "domains" as 1 "corp.test" "interface" s "nmss0" "priority" i 50 "vpn" b false 4 "nameservers" as 1 "10.0.2.3" "interface" s "enp0s2" "priority" i 100 "vpn" b false

$ nmcli device modify enp0s2 ipv4.never-default yes ipv6.never-default yes
Connection successfully reapplied to device 'enp0s2'.

$ nm_dns_config
(the same line as above)

$ resolvectl default-route
Link 2 (enp0s2): no
Link 4 (h-j): no
Link 12 (nmss0): yes

$ resolvectl domain
Global:
Link 2 (enp0s2):
Link 4 (h-j):
Link 12 (nmss0): corp.test
```

`nm_dns_config` is the spike's wrapper for the `busctl get-property` call shown in the reproducer. The dumps taken right after a first activation (reconnect-R4k.txt:5, reconnect-R4l.txt:5) have only two entries, so the `"vpn" b false` copy was not there after a first activation.

journal-all.txt:3906, 3997, 4000, 4080-4085. `exec-tunnel` is the sshuttle tunnel and `python3[3276]` is the test's internal DNS server at 10.99.0.53, behind the tunnel:

```
21:21:04 systemd-resolved[557]: enp0s2: Bus client set default route setting: no
21:21:04 systemd-resolved[557]: enp0s2: Bus client reset DNS server list.
21:21:04 systemd-resolved[557]: nmss0: Bus client set default route setting: yes
21:21:04 exec-tunnel[12193]: c : DNS request from ('192.0.0.9', 58453): 35 bytes
21:21:04 python3[3276]: query from 10.99.0.1: fedoraproject.org type 28
21:21:04 exec-tunnel[12193]: c : DNS request from ('192.0.0.9', 34427): 35 bytes
21:21:04 exec-tunnel[12193]: c : DNS request from ('192.0.0.9', 37743): 35 bytes
21:21:04 python3[3276]: query from 10.99.0.1: fedoraproject.org type 28
21:21:04 python3[3276]: query from 10.99.0.1: fedoraproject.org type 1
```

The condition is unusual: no connection had a default route, in either address family. The spike restored the uplink with `nmcli device reapply enp0s2` right after.

**Who sent the first lookup.** In runs 4 and 5 a triple of queries for fedoraproject.org (types 28, 28, 1) reached the VPN's server in the same second as the change. In run 5 that was 3 s before the spike's own lookup, which sends 28 and 1 (run 5 journal-all.txt:6124, :6127, :6128 at 05:58:26, against :6181-6182 at 05:58:29). The pattern matches NetworkManager's connectivity check. When the uplink's link has no DNS scope, the check falls back to system-wide resolution (`nm-connectivity.c:1012-1019`, `:908-914`) *(source; the requester is not confirmed, and NetworkManager logged no connectivity state change)*.

### Issue 4, run 5: the trigger and a control *(F44)*

R4i, the sentinel alone, changed the committed content and filed the copy (trace-R4i.txt:74, :75, :78, :79; R4i-u FAIL):

```
05:56:47.143958 l3cfg[…,ifindex=29]: emit signal (post-commit, l3cd-old=[424b42fd5e7870ef], l3cd-new=[fe2df13d9f9d686e], l3cd-changed=1)
05:56:47.143966 dns-mgr: (device_l3cd_changed): queueing DNS updates (1)
05:56:47.144014 dns-mgr: (device_l3cd_changed): DNS configuration changed
05:56:47.144017 dns-mgr: (device_l3cd_changed): committing DNS changes (0)
```

- R4c did the same at 05:58:18.160505 (trace-R4c.txt:86-91), and R4c-o repeated the capture (journal-all.txt:5982-5988: `enp0s2` default route `no`, its DNS server list reset, `nmss0` default route `yes`).
- R4g, without firewalld, had a single commit on `nmss0`, with `l3cd-changed=0` (trace-R4g.txt:71), and no copy (R4g-u PASS). In run 4 the same check committed the sentinel first and got the copy.

R8c is the control: R4c's reconnect with `nmss0` unmanaged (`nmcli device`: "10 (unmanaged)", R8c-y). trace-R8c.txt:76-78:

```
06:06:13.134925 l3cfg[…,ifindex=44]: emit signal (post-commit, l3cd-old=[8e646ef3752a8d7c], l3cd-new=[6ff1bda6f2ab6245], l3cd-changed=1)
06:06:13.134934 dns-mgr: (device_l3cd_changed): queueing DNS updates (1)
06:06:13.134948 dns-mgr: (device_l3cd_changed): DNS configuration did not change
```

reconnect-R8c.txt:11 and :34 have two entries, the VPN's and the uplink's. With the uplink's default routes removed, systemd-resolved kept it as default route, with its server (reconnect-R8c.txt:16-20, :28-31):

```
$ resolvectl dns
Global:
Link 2 (enp0s2): 10.0.2.3
Link 23 (h-j):
Link 44 (nmss0): 10.99.0.53

$ resolvectl default-route
Link 2 (enp0s2): yes
Link 23 (h-j): no
Link 44 (nmss0): no
```

### Issue 4, run 6: a second control with a committed sentinel (R8j) *(F44)*

R4i's two-phase reconnect with `nmss0` unmanaged (R8j-y: "10 (unmanaged)"). Both commits on `nmss0` changed the content, and neither filed a device entry. The sentinel alone (trace-R8j.txt:37, :64-68, :70):

```
08:51:27.040581 vpn[…,if:73,dev:2:(nmss0)]: l3cd[ip-4]: add-config fe7f53822223a570
08:51:27.040838 l3cfg[…,ifindex=73]: emit signal (post-commit, l3cd-old=[ef0c73242b096a72], l3cd-new=[9b08e4e0c0642fd4], l3cd-changed=1)
08:51:27.040842 dns-mgr: (device_l3cd_changed): queueing DNS updates (1)
08:51:27.040853 dns-mgr: (device_l3cd_changed): DNS configuration did not change
08:51:27.040855 dns-mgr: (device_l3cd_changed): no DNS changes to commit (0)
08:51:27.040859 vpn[…,if:73,dev:2:(nmss0)]: set state: pre-up (was ip-config-get)
08:51:27.041095 manager: (nmss0): assume: don't assume because device is not managed
```

Then the real `Ip4Config`, after "activated" (trace-R8j.txt:114, :138-140):

```
08:51:27.143495 vpn[…,if:73,dev:2:(nmss0)]: l3cd[ip-4]: add-config 23ad54d05f623e11
08:51:27.143753 l3cfg[…,ifindex=73]: emit signal (post-commit, l3cd-old=[9b08e4e0c0642fd4], l3cd-new=[ef0c73242b096a72], l3cd-changed=1)
08:51:27.143762 dns-mgr: (device_l3cd_changed): queueing DNS updates (1)
08:51:27.143775 dns-mgr: (device_l3cd_changed): DNS configuration did not change
```

- reconnect-R8j.txt:11 (after the reconnect) and :34 (during the default-route probe) have two entries, the VPN's on `nmss0` and the uplink's (R8j-u PASS). With the uplink's default routes removed, systemd-resolved kept `enp0s2` as default route, with 10.0.2.3, and `nmss0` at `no` (reconnect-R8j.txt:16-31; R8j-o PASS).
- In the same run, R4i (managed) took the other branch: `l3cd-changed=1` on ifindex 57 at 08:41:12.072128, then `DNS configuration changed` (trace-R4i.txt:74, :78), and three entries, one of them `"interface" s "nmss0" "priority" i 50 "vpn" b false` (reconnect-R4i.txt:11; R4i-u FAIL).

### Side effect: per-link updates for a deleted tundev (R4j) *(F44)*

In R4j the plugin gave up in a third order: `Failure` (busmon-R4j.txt, 21:24:02.637253), then `ip link del nmss0` (ifindex 16), then `StateChanged(STOPPED)` (21:24:02.669290). Deleting the link caused the early activation described above (trace-R4j.txt:84, `set state: pre-up (was connect)` at 21:24:02.665344). The VPN therefore failed from pre-up (:92), and its DNS was removed (R4j-d PASS). NetworkManager then reset the dead link in systemd-resolved (trace-R4j.txt:157; the same line is journal-all.txt:5688):

```
21:24:02.712271 <warn> dns-sd-resolved[…]: send-updates SetLinkDomains@16 failed: GDBus.Error:org.freedesktop.resolve1.NoSuchLink: Link 16 not known
```

The other `SetLink*` calls for link 16 failed in the same way and were logged at debug level (journal-all.txt:5689-5694). From the source, an entry left behind under a deleted ifindex (issues 1 and 2) leads to the same calls on every later DNS update.

## Root cause

Line numbers are for tag 1.56.1 (b829f838fc5d). The shifts of the four component files for 1.58.0 and main are listed under "Affected versions". `nm_l3_config_data_hash_dns()` is at `nm-l3-config-data.c:3389-3495` on both.

### Issue 1

1. **The policy's check.** `vpn_connection_state_changed()` (`nm-policy.c:2668-2684`) handles the VPN's state changes:
   - It registers the VPN's DNS when the new state is ACTIVATED (`:2676-2677`).
   - It removes the DNS when the new state is FAILED or DISCONNECTED, but only if the old state is in [IP_CONFIG_GET, ACTIVATED] (`:2678-2683`). The comment there says "Only clean up IP/DNS if the connection ever got past IP_CONFIG".
   - The `FIXME(l3cfg)` at `:2675` says that changes to the VPN's l3cd are not tracked.
2. **The states are the external ones.** `_set_vpn_state()` emits `internal-state-changed` with `_state_to_nm_vpn_state()` values (`nm-vpn-connection.c:1012-1026`). That function maps (`:358-391`):
   - internal CONNECT to external CONNECT;
   - IP_CONFIG_GET and PRE_UP to IP_CONFIG_GET;
   - DEACTIVATING to ACTIVATED.
3. **A plugin-driven reconnect.** `StateChanged(STARTING)` after `STARTED` sets internal CONNECT (`:1800-1806`). The policy does nothing for new state CONNECT, so the entry stays registered, which is right while the plugin reconnects.
4. **Giving up from there.** `StateChanged(STOPPED)` sets FAILED from any state from WAITING to ACTIVATED (`:1784-1788`). The policy then sees old state CONNECT and skips the removal. `Failure` only records the reason (`:1753-1771`).
5. **Other paths to the same leak** *(source, not run)*:
   - If the plugin's bus name disappears, for example because the plugin crashed, `_name_owner_changed()` calls `nm_vpn_connection_disconnect()` (`:2766-2777`). That sets DISCONNECTED directly (`:2334-2341`), so the old state is again CONNECT.
   - A user deactivation during the reconnect does not leak: CONNECT → DEACTIVATING is reported as CONNECT → ACTIVATED (`:374-382`), so the policy registers the entry again and removes it at DISCONNECTED.
6. **Why the control works.** A `Config` signal moves CONNECT to IP_CONFIG_GET before anything else (`:1953-1954`). The later `STOPPED` therefore fails the VPN from IP_CONFIG_GET.
7. **Nothing else removes the entry.**
   - The policy registers the VPN's l3cd with the `NMVpnConnection` itself as source tag (`nm-policy.c:2642-2666`).
   - Only a call to `nm_dns_manager_set_ip_config()` with the same source tag and the same ifindex removes it (`nm-dns-manager.c:2080-2126`).
   - After FAILED the VPN manager drops the VPN object (comment at `nm-vpn-connection.c:1006-1009`), so the entry stays until NetworkManager restarts.
   - The removal would not be held back by `vpn_cleanup()`: that runs after the signal (`nm-vpn-connection.c:1121-1124`), so the l3cd is still available when the policy removes it.
8. **What the leftover entry does** *(source)*:
   - `compute_hash()` still includes it (`nm-dns-manager.c:1245-1275`), so its continued presence causes no DNS update.
   - Every later update hands it to the DNS plugin. The systemd-resolved plugin groups entries by ifindex and sends `SetLink*` calls for each ifindex without checking that the link exists (`nm-dns-systemd-resolved.c:829-870`).
   - A failed call is logged once at warning level, then at debug level until the next successful call (`:313`, `:347-352`).
   - The entry also keeps taking part in the per-domain priority decisions (`nm-dns-manager.c:1688-1720`).

### Issue 2

1. **When VPN DNS is registered.** It is registered only on the transition to ACTIVATED (`nm-policy.c:2676-2677`, with the `FIXME(l3cfg)` at `:2675`). Later changes to the VPN's l3cd are not passed on.
2. **The ifindex comes with the l3cd.**
   - The DNS manager files each entry under the ifindex of the l3cd it is given (`nm-dns-manager.c:2080-2085`, `:2145-2156`; asserted at `:368`).
   - `nm_vpn_connection_get_l3cd()` builds the combined l3cd on the VPN's current ip ifindex (`nm-vpn-connection.c:552-610`, ifindex from `:646-660`).
   - A `Config` that names a re-created tundev switches that ifindex (`:1832`, `:1846`; trace "set ip-ifindex-if 21").
3. **What happens next depends on the order.**
   - **Observed (R4f):** the VPN had already returned to ACTIVATED on the old ifindex (the early activation), so no further ACTIVATED transition followed and the new ifindex got no DNS.
   - *(source)* Without the early activation, the next ACTIVATED would register the l3cd under the new ifindex. `replace_all` only replaces entries under the same ifindex (`nm-dns-manager.c:2087-2126`), so the entry under the old ifindex would stay as well.
4. **The old entry cannot be removed.** At FAILED or DISCONNECTED the policy passes the current l3cd, with the new ifindex. The lookup at `nm-dns-manager.c:2080-2085` cannot find the old one.
5. **A move between links is not committed.** The change hash has no ifindex: `nm_l3_config_data_hash_dns()` covers name servers, WINS, domains, searches, options, mDNS, LLMNR, DoT, DNSSEC, type and priority (`nm-l3-config-data.c:3168-3274`). Moving the same content from one ifindex to another therefore gives "did not change", and `update_dns()` does not run (`nm-dns-manager.c:2300-2308`).

### Issue 3

1. **The getter returns a cache.** The `Configuration` getter returns a cached `GVariant` if there is one (`nm-dns-manager.c:2758-2759`). It builds the variant from the entry list and looks up each interface name at build time (`:2812-2818`). For a link that no longer exists it adds no `interface` key.
2. **The cache is rebuilt at the end of each DNS update.** It is cleared only at the end of `update_dns()` (`:2010-2011`) and in `dispose()` (`:2926`). The change notification there makes the D-Bus layer fetch the value again at once (`nm-dbus-manager.c:1201`), so a new snapshot is taken at the end of every `update_dns()`.
3. **When DNS updates run.** `update_dns()` runs:
   - when the hash changed at the end of a batch (`nm-dns-manager.c:2300-2313`);
   - on a configuration reload or signal (`:2689-2699`; `nmcli general reload dns-rc` sets `NM_CONFIG_CHANGE_CAUSE_DNS_RC`, `nm-manager.c:1782-1783`);
   - on a few other occasions (`:2209`, `:2265`).
4. **So the property misses changes that do not alter the hash:**
   - an entry left behind (issue 1);
   - an entry moved to another ifindex (issue 2);
   - a link that disappears.
5. **The default-route decision is not exposed.** The `~` and default-route decisions are made during `update_dns()` (`_mgr_configs_data_construct()`, `:1547-1772`). The property has no field for them.

### Issue 4

1. **The configuration-change path.** `device_l3cd_changed()` registers a device's l3cd whenever the device state is in [IP_CONFIG, DEACTIVATING) (`nm-policy.c:2491-2524`), external devices included.
   - It runs only after a commit that changed the content: the device emits `L3CD_CHANGED` only for a post-commit with `l3cd_changed` set (`nm-device.c:4932-4939`). `l3cd_changed` compares with the previous commit on the same l3cfg (`nm-l3cfg.c:4280`).
   - Outside that state range it removes the device's entry instead (`nm-policy.c:2532-2538`). An unmanaged device is always outside it, which is why R8c and R8j filed nothing.
   - The type is VPN only if `nm_device_is_vpn()`, which is true only for WireGuard (`nm-device.c:3552-3565`). Otherwise it is DEFAULT.
   - The `FIXME(l3cfg)` at `nm-policy.c:2506-2511` notes that this function is not always called when a device becomes ACTIVATED, and that the earlier ACTIVATED code special-cased pseudo-VPNs.
2. **The activation path.** The ACTIVATED branch of `device_state_changed()` skips external devices (`nm-policy.c:2362-2374`).
3. **What the device's l3cd contains.** l3cfg instances are shared per ifindex. For the tundev, the device's l3cd is the merged configuration of that l3cfg, which includes the VPN's l3cds (name server, domain, DNS priority 50; trace-R4c.txt:55-60). The tundev thus gets a second entry with the VPN's DNS, as DEFAULT, at priority 50.
   - In our first activations the copy did not appear (reconnect-R4k.txt:5, reconnect-R4l.txt:5); from the source, the external device reached IP_CONFIG only after the VPN's commit. In R4f the new link's device was likewise still unmanaged when the VPN committed (trace-R4f.txt:222-228).
   - It appeared after every later commit that changed the merged content while the device was activated (R4c and R4i in runs 4 to 6, R4g in run 4). A commit with `l3cd-changed=0` filed nothing (R4b; R4g in runs 5 and 6).
4. **Who gets the `~` domain.** If no entry has a default route, the automatic `~` goes to every non-VPN entry with name servers (`nm-dns-manager.c:1570-1600`, `:1632-1646`). Entries are processed in priority order, and a domain already taken by a lower priority value is dropped for the others (`:1688-1720`). The copy at 50 takes `~` from the uplink at 100.
5. **The uplink loses its servers.** The systemd-resolved plugin sends no servers for an entry that has neither search domains nor a default route (`nm-dns-systemd-resolved.c:390-396`). Hence "enp0s2: Bus client reset DNS server list."

## Proposed fix

Directions; only (a) comes with a patch.

**(a) Remove a VPN's DNS whenever it fails or disconnects, whatever the old state.** Removing an entry that was never added changes nothing: the lookup at `nm-dns-manager.c:2080-2085` finds nothing for this source tag, and the function returns FALSE.
- The patch is not compiled and not tested. `git apply --check` accepts it on 1.56.1 and on main (ed1f38cd; offset +71 lines).
- `old_state` becomes unused; NetworkManager builds with `-Wno-unused-parameter`.

```diff
--- a/src/core/nm-policy.c
+++ b/src/core/nm-policy.c
@@ -2676,10 +2676,11 @@
     if (new_state == NM_VPN_CONNECTION_STATE_ACTIVATED)
         vpn_connection_update_dns(self, vpn, FALSE);
     else if (new_state >= NM_VPN_CONNECTION_STATE_FAILED) {
-        /* Only clean up IP/DNS if the connection ever got past IP_CONFIG */
-        if (old_state >= NM_VPN_CONNECTION_STATE_IP_CONFIG_GET
-            && old_state <= NM_VPN_CONNECTION_STATE_ACTIVATED)
-            vpn_connection_update_dns(self, vpn, TRUE);
+        /* Clean up whatever state we come from. A VPN that was activated
+         * and then fails during a plugin-driven reconnect (STARTED ->
+         * STARTING) fails from CONNECT, and its DNS is still registered.
+         * Removing a configuration that was never added does nothing. */
+        vpn_connection_update_dns(self, vpn, TRUE);
     }
 }
 
```

- A side effect: `update_routing_and_dns()` (`nm-policy.c:2663`) would now also run for VPNs that fail before they got any configuration.
- If that is unwanted, call it only when `nm_dns_manager_set_ip_config()` returned TRUE, or keep a per-VPN flag that is set when the DNS is registered and checked at FAILED/DISCONNECTED.

**(b) Remove by source tag, not only under the current ifindex.** For `NM_DNS_IP_CONFIG_TYPE_REMOVED`, `nm_dns_manager_set_ip_config()` could drop every entry with that source tag, under any ifindex. Alternatively, the policy could remember the l3cd it registered and pass that one on removal. This makes the removal in (a) work after a relink (issue 2).

**(c) Follow the VPN's l3cd after activation.** This is the `FIXME(l3cfg)` at `nm-policy.c:2675`. While the VPN is activated, register its DNS again when `nm_vpn_connection_get_l3cd()` or the VPN's ip ifindex changes, together with (b) so that the old ifindex's entry goes. A signal from `_l3cfg_l3cd_set()` / `_set_ip_ifindex()` in `nm-vpn-connection.c` would be one way to learn about it.

**(d) Keep `DnsManager.Configuration` current.**
- Clear the cached variant and notify whenever the entry list changes, and when a link that an entry refers to goes away or is renamed. Or build the variant on each read.
- Separately, include the ifindex in the change hash, so that moving the same content to another link causes an update.
- Optionally, expose the default-route decision per entry.

**(e) Do not file a VPN's tundev as a separate DNS entry.** In `device_l3cd_changed()`, skip external devices as the ACTIVATED path does (`nm-policy.c:2366`). Or skip a device whose ifindex is a VPN's ip ifindex, since the VPN already registers that configuration. Maintainers will know better whether external devices need this path for other cases.

## Workaround for plugin authors

Until this is fixed, give up from a reconnect in this order:

1. Re-send the unchanged `Config`, naming the same tunnel interface. It must still exist with the same ifindex.
2. Send `Failure`.
3. Send `StateChanged(STOPPED)`.

- **Observed working:** *(F44, R4l)* and *(1.46.0)*.
- **How it works:** the `Config` moves NetworkManager from "connect" to "ip-config-get" (`nm-vpn-connection.c:1953-1954`). The failure then comes from a state that the policy cleans up.
- **Side effects observed:** NetworkManager starts a firewalld zone change for the tundev and cancels it at the failure (trace-R4l.txt:20, :41).
- **Delete the tunnel interface only afterwards.** Do it once NetworkManager has deactivated the VPN, as our plugin did in R4k and R4l.
- **If the tunnel interface is already gone** *(F44, run 5, R4p; run 6, R4p and R8p, the latter with the tundev unmanaged)*: re-sending `Config` still moves the VPN to ip-config-get first. NetworkManager then fails the VPN itself, because the tundev lookup fails (`:1832-1839`, `:1956-1958`, `:1449-1455`). That failure also removes the DNS. Observed from "connect", after the plugin had moved NetworkManager back from that early activation (trace-R4p.txt:81, :105, :109-112; "DNS configuration changed" and "committing DNS changes" follow at :121-124):

  ```
  06:03:13.631858 vpn[…,if:38,dev:2]: set state: ip-config-get (was connect)
  06:03:13.632116 vpn[…,if:38,dev:2]: config: failed to look up VPN interface index for "nmss0"
  06:03:13.632121 vpn[…,if:38,dev:2]: did not receive valid IP config information
  06:03:13.632126 vpn[…,if:38,dev:2]: set state: failed (was ip-config-get)
  ```

  - Clients see reason 5, "IP configuration invalid", not the reason of the plugin's `Failure`.
  - NetworkManager did not ask firewalld to remove the tundev from its zone. `vpn_cleanup()` skips that when the interface has no name any more (`nm-vpn-connection.c:916-920`) *(source)*. *(F44, run 6)* After R4p and R8p, `firewall-cmd --get-zone-of-interface=nmss0` still answered `public` although the link was gone (reconnect-R4p.txt:20-21, reconnect-R8p.txt:20-21). After R4l and R8l, where NetworkManager removed the zone while the link existed, it answered `no zone`.
  - From pre-up or "activated" this is source only.
- **If the tunnel interface was re-created with a new ifindex,** do not send `Config`. Doing so can crash NetworkManager 1.56.1; see our separate report on the SIGSEGV in `_check_complete()`. The leftover entry then cannot be avoided by the plugin. Better: do not re-create the tundev during a reconnect at all (issue 2).
- **A plugin that crashes during a reconnect cannot use this workaround** (the bus-name path above). *(F44, run 5, R4m)* Observed with `kill -9`. NetworkManager's `Disconnect` started a new plugin instance, which left the tundev alone. When the spike deleted the tundev 10 s later, systemd-resolved dropped the link (the next forced update got `NoSuchLink`), but NetworkManager kept the entry. *(F44, run 6, R4m and R8m)* The same, also with the tundev unmanaged.

For issue 4, mark the tundev unmanaged with a `conf.d` snippet:

```ini
[keyfile]
unmanaged-devices+=interface-name:TUNDEV
```

- *(F44, run 5, R8c)* After a content-changing commit, no device entry and no default-route capture (R8b and R8g did not change the content and would have filed nothing either way). In R8b, R8g and R8c the VPN still addressed the link and registered its DNS. With firewalld running (R8b, R8c), it also set the firewalld zone.
- *(F44, run 6, R8c and R8j)* The same again, and after the two content-changing commits of a two-phase sentinel (R8j).
- A later plain `unmanaged-devices=` (without `+`) in another file replaces the list *(source)*.

## Not verified

- **The reproducer has not been run** on any version. Its expected output is derived from the source and from our plugin's runs.
- **Repetitions:** each give-up order ran three times on 1.56.1 (runs 4 to 6), with the same result. In each run R4l ran on a NetworkManager restarted 25 s before its give-up (R4k-z), and R4k on one that had been up for about 10 minutes.
- **How long the entry stays:** on 1.56.1 we saw it only until the spike restarted NetworkManager, 4 s (run 4) and about 11 s (runs 5 and 6) after the give-up or the kill. "Until NetworkManager restarts" is from the source. Run 4's R4k `Configuration` dump cannot show it (issue 3); run 5's rebuilt dump does.
- **1.46.0:** only the missing removal call and the unchanged hash were seen, with `dns=none`. There was no resolver and no `Configuration` dump.
- **Consequences:**
  - observed in runs 5 and 6: systemd-resolved keeping the VPN's server on a tundev that outlives the VPN (R4k-k), and repeated `NoSuchLink` warnings for a deleted ifindex, but only on forced updates (`nmcli general reload dns-rc`);
  - not captured after a kill (R4m, R8m): systemd-resolved's state on the tundev before the spike deleted it;
  - source only: the same with resolv.conf management (`rc-manager=file` or `symlink`), and warnings on updates with other causes.
- **Other failure paths:** a plugin kill during a reconnect was observed (R4m in runs 5 and 6; R8m in run 6, with the tundev unmanaged). A crash or a bus-name loss from other causes is source only; it takes the same path (DISCONNECTED from CONNECT).
- **Issue 2:** the order without the early activation (two entries) is source only.
- **Issue 4:**
  - The removal of the device entry when the VPN gives up from a three-entry state is untested.
  - Other priority combinations, for example a tie with the uplink at 100, are untested.
  - Dual stack is untested. From the source, an IPv6 default route on the uplink would put its IPv6 entry into the wildcard set, and the device copy would not get `~`; the spike removed both default routes.
  - Whether a first activation can create the copy, when the external device reaches IP_CONFIG before the VPN commits, is untested.
  - The unmanaged controls are three reconnects: R8c in runs 5 and 6, and R8j in run 6 (two content-changing commits). Other ways to commit on an unmanaged tundev are untested.
- **Versions:** 1.34.0, 1.36.0, 1.52.0, 1.58.0 and main are source only. On 1.46.0 only issue 1 was observed.
- **The patch** has not been compiled or tested.
- **Duplicates:** we have not searched the NetworkManager tracker yet.

## Related

- **Our report on reconnects that stay "activating"** (same plugin, same runs):
  - `wait_for_pre_up_state` is never cleared. A commit on the tundev's l3cfg can therefore move a reconnecting VPN to pre-up and activated early.
  - In R4j and R4f that early activation is why the VPN failed from pre-up (R4j: DNS removed) or was activated on the dead ifindex (R4f: no DNS for the new link).
  - That report's proposed hunk 2 resets the flag. With it, more give-ups would fail from "connect" and hit issue 1, so fix (a) should go in with it.
- **Our report on the SIGSEGV in `_check_complete()`** when a plugin re-creates its tundev: R4f waited for NetworkManager's new device before sending `Config` and did not crash, but hit issue 2.
- **`vpn-down` is not dispatched for a VPN that was activated and then fails during a reconnect.** `_set_vpn_state()` dispatches it only if the old internal state is ACTIVATED or DEACTIVATING (`nm-vpn-connection.c:1097-1119`) *(source)*. None of trace-R4k.txt (failed from connect), trace-R4l.txt (from ip-config-get) and trace-R4j.txt (from pre-up) contains a `vpn-down` dispatch *(F44)*, and neither do run 5's traces for R4n, R4p and R8l, or run 6's for R4p and R8p. Scripts that undo work done at `vpn-up` therefore do not run in these cases.
- **An early activation dispatches `vpn-pre-up` and `vpn-up` for a dead tunnel.** In run 5's R4p, deleting the tundev during the reconnect moved the VPN to pre-up and activated, and NetworkManager dispatched both on the uplink (trace-R4p.txt:73-74, :81, :92). Nothing undid them *(F44)*. Run 6 repeated this in R4p and in R8p, with the tundev unmanaged. In R8p the `vpn-up` on the uplink ran four dispatcher scripts (journal-all.txt:17945-17953).
