# vpn: SIGSEGV in `_check_complete()` (NULL NMDevice) when a plugin re-sends Config + Ip4Config for a re-created tundev

## Summary

A VPN plugin can crash NetworkManager with SIGSEGV. It does this by deleting and re-creating its tundev while the VPN is up, so the link gets a new ifindex, and then emitting `Config` and `Ip4Config` back to back. If both signals are dispatched before NMManager has created the `NMDevice` for the new link, `_check_complete()` calls `nm_device_create_l3_config_data_from_connection()` with a NULL device. That function returns NULL, and the NULL is then dereferenced.

The NULL device comes from 306f9c490b2a ("vpn: Use nm_device_create_l3_config_data_from_connection if possible"). The guard added to fix that, 574411b8a56e ("vpn: wait for device to become available before creating l3cd", RHEL-125796, MR !2347), waits only while no idle check is pending. It treats "an idle check is already pending" as if it meant "we already waited once". So a second `_check_complete()` call made before the idle runs falls through with `device == NULL`. The idle callback has the same hole, because it calls `_check_complete()` before it clears its own source.

This was found while developing an out-of-tree VPN plugin (nm-sshuttle). A test mode of that plugin re-created its tundev, a dummy link, on reconnect.

## Affected versions

| Version | Status | Basis |
|---|---|---|
| 1.54.x and older | Not affected by this path | 306f9c490b2a is first tagged in 1.56-rc1. The 1.52.0, 1.52.1, 1.54.0 and 1.54.3 copies of `nm-vpn-connection.c` do not call `nm_device_create_l3_config_data_from_connection()`. |
| 1.56.0 | Affected (from source) | Contains the guard as cherry-pick 4c5478744c0f. `nm-vpn-connection.c` is identical to 1.56.1. Not built or run. |
| 1.56.1 (tag `1.56.1`, b829f838fc5d) | Affected | Crash observed with Fedora's NetworkManager-1.56.1-2.fc44. Also reproduced with 1.56.1 built from the tag (see below). |
| 1.58.0 | Affected (from source) | Contains 306f9c490b2a and 574411b8a56e. `nm-vpn-connection.c` matches 1.56.1 except 4 added lines at 2138+. Not built or run. |
| main at ed1f38cd449b (2026-10-01) | Affected | Reproduced with main built from source. Lines 1412-1489 of `nm-vpn-connection.c` are identical to 1.56.1. |

Commits checked in a full clone of the upstream history:
- **306f9c490b2a** "vpn: Use nm_device_create_l3_config_data_from_connection if possible". First tag: 1.56-rc1.
- **574411b8a56e** "vpn: wait for device to become available before creating l3cd". It carries `Fixes: 306f9c490b2a`, https://issues.redhat.com/browse/RHEL-125796 and https://gitlab.freedesktop.org/NetworkManager/NetworkManager/-/merge_requests/2347. First tags: 1.57.2-dev, 1.58-rc1, 1.58.0.
- **4c5478744c0f** is the same change, "cherry picked from commit 574411b8a56e". It is in 1.56.0.

## Environment where it was first observed

- Fedora 44, kernel 7.2.8-200.fc44.x86_64.
- NetworkManager-1.56.1-2.fc44.x86_64, glib2-2.88.3-1.fc44, SELinux enforcing. NetworkManager was logging at info level.
- An out-of-tree Python VPN plugin. Its tundev is a dummy link, `nmss0`.
- The test ("reconnect with relink") did this:
  1. Emit `StateChanged(STARTING)` and stop the plugin's tunnel.
  2. About 20 s later, once the tunnel was back: `ip link del nmss0`, `ip link add nmss0 type dummy`, `ip link set nmss0 up`.
  3. Emit `Config`, `Ip4Config` and `StateChanged(STARTED)` back to back. The `Config` had `tundev`, `gateway`, `has-ip4=true`, `has-ip6=false` and `can-persist=true`. The `Ip4Config` had `address`, `prefix=32`, `dns`, `domains` and `never-default=true`.
- The crash happened on the only attempt of this test. Six first activations of a freshly created `nmss0` in the same run did not crash.

## Steps to reproduce

This is a race. NetworkManager crashes only if the plugin's `Config` and `Ip4Config` are dispatched before NMManager's idle handler has created the `NMDevice` for the new link. On an otherwise idle system, NetworkManager usually wins the race.

The steps below hold NetworkManager with SIGSTOP while the plugin re-creates the link and sends its signals. When NetworkManager resumes, the netlink events and the D-Bus signals are all ready together, and the D-Bus signals win.

**Tested only in a sandbox,** not on a packaged distribution build:
- unshared mount, network and PID namespaces, a private `dbus-daemon` (not dbus-broker), nmcli 1.46;
- NetworkManager built from tag 1.56.1 and from main ed1f38cd with gcc 13.3, `debugoptimized`, `more_asserts=0`, no LTO;
- GLib 2.80, kernel 6.18.

**Results:**
- **With the hold:** 1.56.1 crashed in the first round in all 9 runs, and main in all 3 runs. The same two criticals were logged (1.56.1: `nm-device.c:5324`/`3635`; main: `5391`/`3697`), then the process exited with status 139. The `SIGUSR2` variant (no `STARTING`) crashed in the same way.
- **Without the hold:** 1.56.1 survived 16 rounds.

Prerequisites: a disposable VM, root, NetworkManager 1.56.0 or later, an active connection with a default route, and python3 with PyGObject.

`nm-repro-vpn-service`:

```python
#!/usr/bin/python3
# Fake NetworkManager VPN plugin: no tunnel, a persistent tun link "repro0" as tundev.
#   Connect: create repro0, wait 1 s, send Config + Ip4Config + STARTED.
#   SIGUSR1: StateChanged(STARTING); 0.5 s later, in one callback, delete and
#            re-create repro0 (new ifindex), then Config + Ip4Config + STARTED.
#   SIGUSR2: same re-create + Config + Ip4Config, without STARTING.
import os, signal, socket, struct, subprocess, sys, warnings
from gi.repository import Gio, GLib

warnings.filterwarnings("ignore", category=DeprecationWarning)
BUS_NAME = "org.freedesktop.NetworkManager.repro"
OBJ_PATH = "/org/freedesktop/NetworkManager/VPN/Plugin"
IFACE = "org.freedesktop.NetworkManager.VPN.Plugin"
LINK = "repro0"
STARTING, STARTED, STOPPED = 3, 4, 6  # NMVpnServiceState
XML = """<node><interface name="org.freedesktop.NetworkManager.VPN.Plugin">
 <method name="Connect"><arg name="connection" type="a{sa{sv}}" direction="in"/></method>
 <method name="ConnectInteractive"><arg name="connection" type="a{sa{sv}}" direction="in"/>
  <arg name="details" type="a{sv}" direction="in"/></method>
 <method name="NeedSecrets"><arg name="settings" type="a{sa{sv}}" direction="in"/>
  <arg name="setting_name" type="s" direction="out"/></method>
 <method name="NewSecrets"><arg name="connection" type="a{sa{sv}}" direction="in"/></method>
 <method name="Disconnect"/>
 <property name="State" type="u" access="read"/>
</interface></node>"""

loop = GLib.MainLoop()
conn = None
state = 0

def log(msg):
    print(f"nm-repro-vpn[{os.getpid()}]: {msg}", file=sys.stderr, flush=True)

def ip4(addr):  # NM wants IPv4 addresses as uint32 in network byte order
    return struct.unpack("=I", socket.inet_aton(addr))[0]

def emit(name, sig, args):
    conn.emit_signal(None, OBJ_PATH, IFACE, name, GLib.Variant(sig, args))

def set_state(s):
    global state
    state = s
    emit("StateChanged", "(u)", (s,))

def ip(*args):
    subprocess.run(["ip", *args], stderr=subprocess.DEVNULL)

def recreate_link():
    ip("link", "del", LINK)
    ip("tuntap", "add", LINK, "mode", "tun")  # the Fedora run used: ip link add LINK type dummy
    ip("link", "set", LINK, "up")
    log(f"{LINK} is now ifindex {socket.if_nametoindex(LINK)}")

def send_config():
    emit("Config", "(a{sv})", ({
        "tundev": GLib.Variant("s", LINK),
        "gateway": GLib.Variant("u", ip4("192.0.2.10")),
        "has-ip4": GLib.Variant("b", True),
        "has-ip6": GLib.Variant("b", False),
    },))
    emit("Ip4Config", "(a{sv})", ({
        "address": GLib.Variant("u", ip4("10.250.0.2")),
        "prefix": GLib.Variant("u", 32),
        "never-default": GLib.Variant("b", True),
    },))
    log("sent Config + Ip4Config")

def connect():
    set_state(STARTING)
    recreate_link()
    def finish():
        send_config()
        set_state(STARTED)
        return GLib.SOURCE_REMOVE
    GLib.timeout_add(1000, finish)  # NM has its NMDevice for repro0 by then
    return GLib.SOURCE_REMOVE

def reconnect(with_starting):
    log(f"reconnect (with STARTING: {with_starting})")
    if with_starting:
        set_state(STARTING)
    def relink():
        recreate_link()
        send_config()  # right after the new link, before NM has an NMDevice for it
        set_state(STARTED)
        return GLib.SOURCE_REMOVE
    GLib.timeout_add(500, relink)
    return GLib.SOURCE_CONTINUE

def on_call(_conn, _sender, _path, _iface, method, _params, inv):
    log(f"D-Bus call {method}")
    if method == "NeedSecrets":
        inv.return_value(GLib.Variant("(s)", ("",)))
        return
    inv.return_value(None)
    if method in ("Connect", "ConnectInteractive"):
        GLib.idle_add(connect)
    elif method == "Disconnect":
        ip("link", "del", LINK)
        set_state(STOPPED)
        GLib.timeout_add(200, loop.quit)

def on_bus_acquired(c, _name):
    global conn
    conn = c
    node = Gio.DBusNodeInfo.new_for_xml(XML)
    conn.register_object(OBJ_PATH, node.interfaces[0], on_call,
                         lambda *a: GLib.Variant("u", state), None)

Gio.bus_own_name(Gio.BusType.SYSTEM, BUS_NAME, Gio.BusNameOwnerFlags.NONE,
                 on_bus_acquired, lambda *a: log("bus name acquired"),
                 lambda *a: loop.quit())
GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR1, reconnect, True)
GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR2, reconnect, False)
GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, loop.quit)
loop.run()
ip("link", "del", LINK)
```

Setup and trigger:

```sh
install -D -m 755 nm-repro-vpn-service /usr/local/libexec/nm-repro-vpn-service

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

cat > /usr/lib/NetworkManager/VPN/nm-repro-service.name <<'EOF'
[VPN Connection]
name=repro
service=org.freedesktop.NetworkManager.repro
program=/usr/local/libexec/nm-repro-vpn-service
supports-multiple-connections=false
EOF
sleep 2   # NetworkManager picks up new files in this directory

nmcli connection add type vpn con-name repro vpn-type repro \
    ipv4.never-default yes ipv4.auto-route-ext-gw no ipv6.method ignore
nmcli connection up repro                # VPN activated, repro0 exists

NM_PID=$(pidof NetworkManager)
kill -STOP "$NM_PID"                     # hold NetworkManager while the plugin relinks
pkill -USR1 -f nm-repro-vpn-service      # STARTING; 0.5 s later: re-create repro0, Config, Ip4Config, STARTED
sleep 1.5
kill -CONT "$NM_PID"
sleep 2
kill -0 "$NM_PID" 2>/dev/null && echo "NetworkManager $NM_PID still running" \
                              || echo "NetworkManager $NM_PID is gone"
journalctl -b --since -2min | grep -E 'nm-device.c|dumped core|SEGV'
coredumpctl list NetworkManager
```

Variant: use `pkill -USR2` instead (no `STARTING`, so the VPN stays activated). It crashed the same way in the sandbox.

Cleanup: `nmcli connection delete repro`, `pkill -f nm-repro-vpn-service`, `ip link del repro0`, then remove the three files.

## Actual result

NetworkManager logs two criticals and then dies with SIGSEGV. On Fedora, systemd restarted it and the VPN was gone. The plugin process and its tunnel were left orphaned until the test harness stopped them about 63 s later.

Journal excerpt from the Fedora run:
- The host name is trimmed, and on plugin lines so is the syslog identifier `nm-sshuttle-spike-service[8178]:`.
- Journal timestamps have 1-second resolution, so only the order of lines from the same process is exact.

```
22:52:08 nm-sshuttle-spike[8178]: SIGUSR1: simulating a drop, reconnecting in 20 s
22:52:08 nm-sshuttle-spike[8178]: state STARTED -> STARTING
...
22:52:29 nm-sshuttle-spike[8178]: run: ip link del nmss0
22:52:29 NetworkManager[730]: <info>  [1791413549.3576] device (nmss0): state change: activated -> unmanaged (reason 'unmanaged', managed-type: 'removed')
22:52:29 NetworkManager[730]: <warn>  [1791413549.3624] platform-linux: do-add-ip4-address[10: 192.0.0.8/32]: failure 19 (No such device - ipv4: Device not found)
22:52:29 nm-sshuttle-spike[8178]: run: ip link del nmss0
22:52:29 nm-sshuttle-spike[8178]:   -> exit 1: Cannot find device "nmss0"
22:52:29 nm-sshuttle-spike[8178]: run: ip link add nmss0 type dummy
22:52:29 nm-sshuttle-spike[8178]: run: ip link set nmss0 up
22:52:29 nm-sshuttle-spike[8178]: created nmss0 (dummy)
22:52:29 nm-sshuttle-spike[8178]: Config: gateway=198.51.100.1 tundev=nmss0 has-ip4=True
22:52:29 nm-sshuttle-spike[8178]: Ip4Config: address=192.0.0.8/32 dns=['10.99.0.53'] domains=['corp.test'] never-default=true
22:52:29 nm-sshuttle-spike[8178]: state STARTING -> STARTED
22:52:29 NetworkManager[730]: ((../src/core/devices/nm-device.c:5324)): assertion '<dropped>' failed
22:52:29 NetworkManager[730]: file ../src/core/devices/nm-device.c: line 3635 (<dropped>): should not be reached
22:52:29 systemd-coredump[9231]: Process 730 (NetworkManager) of user 0 dumped core.
22:52:29 systemd[1]: NetworkManager.service: Main process exited, code=dumped, status=11/SEGV
```

The Fedora log also shows:
- NetworkManager 730 never logged `manager: (nmss0): new Dummy device` for the re-created link before the crash. It did log that line each time `nmss0` was created earlier in the run, and the restarted NetworkManager (pid 9285) logged it within about 0.1 s of starting.
- No `config: failed to look up VPN interface index` warning was logged, so the `Config` handler did resolve `nmss0` to an ifindex.

Coredump stack of the crashing thread. The module NetworkManager is from the rpm NetworkManager-1.56.1-2.fc44.x86_64. The other three threads were idle in `ppoll` or `g_cond_wait`. No debuginfo was available.

```
#0  0x00005609eaa68a72 _check_complete.lto_priv.0 (NetworkManager + 0x11fa72)
#1  0x00005609eaa6c905 _dbus_signal_ip_config_cb (NetworkManager + 0x123905)
#2  0x00005609eaa6f48d _dbus_dispatch_cb (NetworkManager + 0x12648d)
#3  0x00007fccfef152e0 emit_signal_instance_in_idle_cb (libgio-2.0.so.0 + 0xde2e0)
#4  0x00007fccfecb9524 g_idle_dispatch (libglib-2.0.so.0 + 0x45524)
#5  0x00007fccfecb7f24 g_main_context_dispatch_unlocked.lto_priv.0 (libglib-2.0.so.0 + 0x43f24)
#6  0x00007fccfecbc038 g_main_context_iterate_unlocked.isra.0 (libglib-2.0.so.0 + 0x48038)
#7  0x00007fccfecbc2e7 g_main_loop_run (libglib-2.0.so.0 + 0x482e7)
#8  0x00005609ea963450 main (NetworkManager + 0x1a450)
#9  0x00007fccfe40a681 __libc_start_call_main (libc.so.6 + 0x3681)
#10 0x00007fccfe40a798 __libc_start_main@@GLIBC_2.34 (libc.so.6 + 0x3798)
#11 0x00005609ea964235 _start (NetworkManager + 0x1b235)
```

## Expected result

NetworkManager does not crash. It either applies the new configuration to the new interface once its `NMDevice` exists, or fails the VPN cleanly.

## Root cause

Line numbers are against tag `1.56.1` (b829f838fc5d). On main (ed1f38cd), `nm-vpn-connection.c` has the same line numbers up to line 2137; after that they are shifted by +4.

Each step is marked with its evidence:
- *(Fedora)*: the Fedora journal or core dump.
- *(sandbox log)*: a debug-level log of a sandbox run of the stub against 1.56.1 built from source.
- *(source)*: from reading the code.

1. The plugin emits `StateChanged(STARTING)` *(Fedora)*. The VPN goes from ACTIVATED to `STATE_CONNECT` (`src/core/vpn/nm-vpn-connection.c:1800-1806`) *(sandbox log: "set state: connect (was activated)")*. The state from the first connection stays in place *(source)*:
   - `generic_config_received` is only ever set to TRUE (`:1904`) and is never reset.
   - `l3cds[L3CD_TYPE_IP_4]` still holds the old IPv4 config. It is cleared only by `_l3cfg_l3cd_clear_all()`, which is called from `vpn_cleanup()` (`:925`, on FAILED/DISCONNECTED, `:1097-1124`) and from `finalize()` (`:3113`).
2. The plugin deletes and re-creates the tundev, then emits `Config`, `Ip4Config` and `StateChanged(STARTED)` back to back *(Fedora)*. The new ifindex was not logged on Fedora; in the sandbox it went from 4 to 5.
3. `_dbus_signal_config_cb()` (`:1914`) does the following:
   - It moves CONNECT to IP_CONFIG_GET (`:1953-1954`) and resolves `tundev` to the new ifindex (`:1832-1835`) *(sandbox log: "set state: ip-config-get", "set ip-ifindex-if 5")*.
   - It calls `_check_complete(self, TRUE)` (`:1961`). The "need more config" gate (`:1438-1445`) passes because `l3cds[IP_4]` still holds the previous IPv4 config *(source)*.
   - No `NMDevice` exists yet for the new ifindex *(Fedora and sandbox log: no "new … device" line before the crash)*. NMManager creates it from `_platform_link_cb_idle()`, which `platform_link_cb()` queues with `g_idle_add()`, so at `G_PRIORITY_DEFAULT_IDLE` (`src/core/nm-manager.c:4356`, `:4423`).
   - So the guard at `:1466-1470` schedules `check_device_added_idle_source` and returns. The source is created with `nm_g_idle_add_source()`, also `G_PRIORITY_DEFAULT_IDLE` (`src/libnm-glib-aux/nm-shared-utils.h:1566-1572`). This step is inferred: that code logs nothing, but no other `_check_complete()` caller runs in this window.
4. The `Ip4Config` signal is dispatched next, from GDBus's per-signal idle source *(Fedora stack: frames #1-#3; sandbox log: "config4: reply received")*.
   - GDBus attaches that source at `G_PRIORITY_DEFAULT` (`gio/gdbusconnection.c:4410-4411`). This was checked in GLib main at 9235759daf6e, not in 2.88.3. So the source runs before both DEFAULT_IDLE sources.
   - `_dbus_signal_ip_config_cb()` stores the new IPv4 l3cd (`:2326`) and calls `_check_complete(self, TRUE)` (`:2330`; `:2334` on main).
   - The gate passes and `device` is still NULL. But `check_device_added_idle_source` is now set, so the guard is false and the code falls through *(source)*.
5. At `:1477-1479`, `nm_assert(device)` is compiled out, and so is `nm_assert(NM_IS_DEVICE(self))` at `nm-device.c:3627`. For Fedora this is inferred, because a segfault followed rather than an abort; the sandbox build had `more_asserts=0`. Then `nm_device_create_l3_config_data_from_connection(NULL, connection)` runs:
   - `nm_device_get_ip_ifindex(NULL)` hits `g_return_val_if_fail(self != NULL, 0)` at `src/core/devices/nm-device.c:5324`.
   - Then `g_return_val_if_reached(NULL)` at `nm-device.c:3635` fires and the function returns NULL *(Fedora and sandbox: both criticals)*.
6. `:1488` calls `nm_l3_config_data_set_allow_routes_without_address(NULL, AF_INET, TRUE)`, which writes through NULL at `src/core/nm-l3-config-data.c:2006` *(source)*.
   - On Fedora the faulting frame is `_check_complete.lto_priv.0`, which suggests this function was inlined by LTO. That is inferred, as there was no debuginfo.
   - The sandbox build had no LTO and also died with SIGSEGV (exit status 139).
   - On main the corresponding lines are `nm-device.c:5391`/`3697` (the sandbox main build logged these) and `nm-l3-config-data.c:2202`.

**The bug:** the guard at `:1466` lets `_check_complete()` wait for the device only once, and it treats "the idle is pending" as if it meant "we already waited". There are two ways to reach the NULL dereference:

- **A (observed):** any `_check_complete(TRUE)` call that passes the gate while the idle is pending and the device is still missing. Here that call was a second D-Bus config signal (`Ip4Config` after `Config`).
- **B (from source, not observed):** `_check_device_added_idle_cb()` calls `_check_complete()` (`:1417`) before it clears its own source (`:1418`). If there is still no device when the idle runs, the guard is false again and the same NULL dereference follows. That can happen if NMManager created no device for that ifindex, or if the link disappeared before NMManager's idle ran.
  - Separately, the callback returns `G_SOURCE_CONTINUE` (`:1420`) after destroying its own source. That is harmless, but `G_SOURCE_REMOVE` is what is meant.

**Exposure:**
- *(source)* `Config` is accepted from `STATE_NEED_AUTH` on (`:1926`), and `Ip4Config`/`Ip6Config` from NEED_AUTH up to ACTIVATED (`:1983-1993`).
- *(source)* The gate stays open after the first connection, so any plugin that re-sends `Config` + `IpXConfig` naming a link NetworkManager has no device for yet is exposed. This applies with or without `STARTING`; both variants crashed in the sandbox.
- A first activation that sends exactly one `Config` and one `IpXConfig` per address family should be safe. Only the last signal passes the gate, and NMManager's link idle is queued before the VPN's idle, at the same priority *(source)*. One sandbox run of a first activation with NetworkManager held did not crash.
- *(source, not tested)* Sending a second `IpXConfig` before the device exists would hit path A even on a first activation.
- Other plugins that re-create their tun device on reconnect may be affected. This has not been examined.

## Proposed fix

The fix does three things:
- While the check idle is pending, wait for it instead of falling through.
- Clear the source before re-checking from the idle.
- If there is still no device after that one idle, fall back to the device-less l3cd, which was the behaviour before 306f9c490b2a. This keeps the wait bounded, which matters because `connect_timeout_source` has already been cleared at `:1447` by the time the device check runs.

It also stops dereferencing a NULL returned by `nm_device_create_l3_config_data_from_connection()` for any other reason.

Status of the patch:
- It applies cleanly to 1.56.1 and to main ed1f38cd (`git apply --check`, `patch --dry-run`).
- It compiles without warnings (gcc 13.3, `debugoptimized`).
- In the sandbox, the patched 1.56.1 and main survived every held relink round: 1.56.1 for 9 rounds and main for 7, across `SIGUSR1` and `SIGUSR2`. Several of those rounds hit the race window (the `Ip4Config` arrived before the new `NMDevice`). In each of those, the debug log shows the VPN waiting, then reaching pre-up and activated on the new ifindex.
- Path B (the fallback) was never exercised.

```diff
vpn: don't create the l3cd with a NULL device while waiting for it

_check_complete() schedules check_device_added_idle_source when the VPN's
ifindex has no NMDevice yet, but treats "the idle is pending" like "we
already waited". Any other _check_complete() call before the idle runs
(for example the Ip4Config signal right after Config) falls through and
calls nm_device_create_l3_config_data_from_connection(NULL, ...), which
returns NULL, and the result is dereferenced. The idle callback has the
same problem, because it calls _check_complete() before clearing its
source.

Wait while the idle is pending, clear the source before re-checking, and
if there is still no device after that one idle, fall back to the
device-less l3cd as before 306f9c490b2a.

Fixes: 574411b8a56e ('vpn: wait for device to become available before creating l3cd')

diff --git a/src/core/vpn/nm-vpn-connection.c b/src/core/vpn/nm-vpn-connection.c
--- a/src/core/vpn/nm-vpn-connection.c
+++ b/src/core/vpn/nm-vpn-connection.c
@@ -230,7 +230,10 @@
 static void
 _l3cfg_notify_cb(NML3Cfg *l3cfg, const NML3ConfigNotifyData *notify_data, NMVpnConnection *self);
 
-static void _check_complete(NMVpnConnection *self, gboolean success);
+static void
+_check_complete_full(NMVpnConnection *self, gboolean success, gboolean device_wait_done);
+
+#define _check_complete(self, success) _check_complete_full((self), (success), FALSE)
 
 /*****************************************************************************/
 
@@ -1414,14 +1417,14 @@
     NMVpnConnection        *self = user_data;
     NMVpnConnectionPrivate *priv = NM_VPN_CONNECTION_GET_PRIVATE(self);
 
-    _check_complete(self, TRUE);
     nm_clear_g_source_inst(&priv->check_device_added_idle_source);
+    _check_complete_full(self, TRUE, TRUE);
 
-    return G_SOURCE_CONTINUE;
+    return G_SOURCE_REMOVE;
 }
 
 static void
-_check_complete(NMVpnConnection *self, gboolean success)
+_check_complete_full(NMVpnConnection *self, gboolean success, gboolean device_wait_done)
 {
     NMVpnConnectionPrivate                 *priv = NM_VPN_CONNECTION_GET_PRIVATE(self);
     nm_auto_unref_l3cd_init NML3ConfigData *l3cd = NULL;
@@ -1460,12 +1463,17 @@
     device     = nm_manager_get_device_by_ifindex(NM_MANAGER_GET, ifindex);
 
     /* We have a defined interface index, but the device is not processed yet.
-     * The processing of the new kernel link could be queued in an idle handler,
-     * so schedule an idle handler once to check if the device has been processed.
+     * NMManager creates the NMDevice for a new kernel link from an idle handler
+     * (_platform_link_cb_idle()), which normally runs before ours. Wait for our
+     * idle handler, also if it is already pending: never continue with a NULL
+     * device before it ran. If there is still no device when it runs, fall back
+     * to the device-less l3cd below, so that we wait at most once.
      */
-    if (ifindex > 0 && !device && !priv->check_device_added_idle_source) {
-        priv->check_device_added_idle_source =
-            nm_g_idle_add_source(_check_device_added_idle_cb, self);
+    if (ifindex > 0 && !device && !device_wait_done) {
+        if (!priv->check_device_added_idle_source) {
+            priv->check_device_added_idle_source =
+                nm_g_idle_add_source(_check_device_added_idle_cb, self);
+        }
         return;
     }
 
@@ -1474,15 +1482,15 @@
      * If this vpn connection does not have its own device resort to nm_l3_config_data_new_from_connection
      * since we can't properly apply these properties anyway
      */
-    if (ifindex > 0) {
-        nm_assert(device);
+    if (device)
         l3cd = nm_device_create_l3_config_data_from_connection(device, connection);
-    } else {
+    if (!l3cd) {
         l3cd = nm_l3_config_data_new_from_connection(nm_netns_get_multi_idx(priv->netns),
                                                      nm_vpn_connection_get_ip_ifindex(self, TRUE),
                                                      connection);
-        _LOGD("VPN connection does not have its own device. Some connection properties won't be "
-              "supported.");
+        _LOGD("VPN connection does not have its own device%s. Some connection properties won't be "
+              "supported.",
+              ifindex > 0 ? " yet" : "");
     }
 
     nm_l3_config_data_set_allow_routes_without_address(l3cd, AF_INET, TRUE);
```

Behaviour after the patch:
- **Device present:** unchanged.
- **No tundev (`ifindex <= 0`):** unchanged.
- **Path A:** the second call returns. The idle then runs after NMManager's link idle and finds the device. This was seen in the sandbox.
- **Path B (from source):** the idle falls back to the device-less l3cd. That loses only the six properties `nm_device_create_l3_config_data_from_connection()` adds: mdns, llmnr, dns-over-tls, dnssec, ip6-privacy and mptcp-flags. On main it also skips the unreachable-gateway warning.

Two things are left unchanged:
- A redundant idle run when a later call finds the device first. That was already possible before.
- The stale-config gate from step 1. On a reconnect, `Config` alone completes the check using the previous connection's IPv4 config. That is a separate behaviour question and is not needed to fix the crash.

## Workaround for plugin authors

- Keep the same tundev (same ifindex) across reconnects. In the Fedora run, an earlier reconnect test re-sent `Config` + `Ip4Config` for the unchanged `nmss0` without crashing.
- If a new link is unavoidable, emit `Config` only after NetworkManager has a device for it. *(Observed once on Fedora 44 with 1.56.1: no crash.)* The plugin polled `GetDeviceByIpIface()` until it returned a device path other than the old one (0.10 s), then sent `Config` and `Ip4Config`. But deleting the old link had already caused a commit on the dead ifindex. That commit moved the VPN to `pre-up` and `activated` 79 ms before the `Config`, and the VPN's DNS was never registered for the new link. Keeping the tundev is the only clean option.
  - For example, subscribe to `org.freedesktop.NetworkManager.DeviceAdded` before creating the link, and wait for a device whose `Interface` is the tundev.
  - Polling `GetDeviceByIpIface()` is not enough: it can still return the old device if NetworkManager has not yet processed the removal of the old link.
- On a first activation, emit exactly one `Config` and one `IpXConfig` per address family. This does not protect a reconnect with a new ifindex.

## Not verified

- **Reproducer:** it was run only in the sandbox described above, never against a distribution package. In particular it was not run on Fedora's NetworkManager-1.56.1-2.fc44, with dbus-broker, or with a dummy link. The SIGSTOP hold is a test aid that widens the race window.
- **The Fedora crash:** it was seen once, on the only relink attempt. Why NetworkManager lost the race there without any hold could not be determined from the info-level logs. A later Fedora run re-created the link once more, waiting for NM's device before `Config`, and NetworkManager did not crash.
- **The faulting instruction on Fedora:** no debuginfo was available. That it is the store at `nm-l3-config-data.c:2006` is inferred from the frame #0 symbol, the two criticals logged just before, and the source.
- **Untested variants:** path B, and the variant that sends a second `IpXConfig` on a first activation, come from source only.
- **1.56.0 and 1.58.0:** only `nm-vpn-connection.c` was compared; neither was built or run.
- **Fedora downstream patches** for 1.56.1-2.fc44 were not checked. The logged line numbers (5324, 3635) match upstream 1.56.1.
- **GLib dispatch priorities** were checked in GLib main (9235759daf6e), not in 2.88.3 (Fedora) or 2.80 (sandbox).
- **Existing reports:** the upstream GitLab tracker could not be reached for a duplicate search. A general web search found no matching report. The GitHub mirror shows no change to `nm-vpn-connection.c` on main after ed1f38cd as of 2026-10-08.