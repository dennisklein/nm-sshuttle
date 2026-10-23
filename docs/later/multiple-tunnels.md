# Several tunnels at once

Status: implemented. One of the "Later" items of the
[design](../design.md) (§7); the index is in [README.md](README.md).
"What was built" at the end lists the results and where the code differs
from this design.

v1 allows many profiles but one active at a time (§1). The `.name` file
says `supports-multiple-connections=false`, and every name the plugin uses
is fixed: the bus name, `nmss0`, the guard table, the tunnel unit,
`/run/nm-sshuttle/tunnel.json`. This design lets several nm-sshuttle VPNs
be active together, for example a corporate network and a lab behind a
different jump host.

## Goals and non-goals

Goals:

- Up to 8 nm-sshuttle VPNs active at once, each with its own toggle,
  reconnects, guard, DNS link and tunnel unit.
- One tunnel's failure, give-up, kill or reconnect does not touch the
  others.
- Profiles that would fight over the same addresses or DNS are refused at
  `Connect`, with a message that names the other profile.
- A single tunnel behaves exactly as in v1, apart from unit and table
  names.

Not in this step:

- **Nested subnets that follow each other at run time** (a lab /16 inside
  a corporate /8, each through its own jump host, with the /16 going to
  the lab tunnel only while it is up). sshuttle cannot change its rules
  while it runs, so this would restart the outer tunnel whenever the
  inner one comes or goes. The static form works: put the inner network
  in the outer profile's `exclude` (below).
- **More than 8 slots,** and any change to how one tunnel reconnects.

## What the research found

Sources: NetworkManager `main` (1.59.2-dev, ef234f8), gnome-shell `main`
(eafd268), sshuttle 1.3.2 and 2.0.0, and this repository.

- **[src] NM refuses a second activation of a single-connection plugin.**
  With `supports-multiple-connections=false`, NM fails it with "The
  'sshuttle' plugin only supports a single active connection"
  (`nm-vpn-manager.c:99-107`).
- **[src] With multiple connections, every activation gets its own bus
  name.** NM names it `<service>.Connection_<N>`, where `N` is the last
  part of the active connection's D-Bus path (`nm-vpn-connection.c:2877-2887`).
  If that name has no owner, NM spawns `program=` with
  `--bus-name <name>` and waits 5 s for the name to appear
  (`:2633-2636`, `:2750-2763`). When the name's owner goes away, NM
  disconnects that VPN (`:2770-2781`). Active connection numbers are not
  reused within one NM process; a restarted NM counts from the start
  again (inferred).
- **[src] One bus connection cannot serve two activations.** NM
  subscribes to the plugin's signals by sender, the bus name it waits
  for (`nm-vpn-connection.c:2919-2925`). GDBus matches a well-known
  sender name through its unique owner, so two names owned by one bus
  connection would each receive the other's `StateChanged`, `Config` and
  `Ip4Config` (inferred from GDBus's documented behaviour).
- **[spike] [src] The shim cannot start a unit itself.** `program=` runs
  as `NetworkManager_t` in NM's cgroup, and systemd refused that domain
  `start`, `stop` and `status` on an ordinary unit (R1a, §2.1). D-Bus
  activation is the only way out that worked (R1b), and it starts the
  one service its activation file names, for one fixed bus name.
- **[src] NM's device match takes globs.** `interface-name:nmss*`
  matches every `nmssN` (`nm-core-utils.c:1356-1373`, NetworkManager.conf
  "interface-name:IFNAME ... Simple globbing is supported").
- **[src] GNOME lists each VPN profile with its own switch** and shows
  every active one (`js/ui/status/network.js:1576-1586`).
- **[src] sshuttle supports several copies** as long as their subnets do
  not overlap: "only the most-recently-started one will win"
  (`methods/nft.py:9-14`). Each copy names its tables after its own
  redirect port, the first free one counting down from 12300, found by
  binding (`client.py:1042-1084`), so two copies cannot pick the same
  port.
- **[repo]** The D-Bus policy already allows root to own every name under
  `org.freedesktop.NetworkManager.sshuttle` (`own_prefix`). The sweep
  already treats any sshuttle table without a listener as stale and
  leaves live ones alone, so one tunnel's sweep is safe for another's
  tables (lab T7c). The health check is not: it asks whether *any*
  sshuttle table has a listener (`tunnel.nft_health`).
- **[spike] NM ignores edits to a `.name` file it has loaded** (§2.1).
  Whether a package update, which replaces the file, counts as an edit
  is untested.

## Options

**A. One plugin process per activation (proposed).** A small
*dispatcher* is D-Bus-activated by the shim. It gives the activation a
slot and starts a templated plugin unit for it, which owns the bus name
NM waits for. Each plugin instance is today's plugin with its names taken
from the slot.

**B. One process with one bus connection per activation.** The plugin
process opens a new system bus connection for each activation and owns
that activation's name on it, with one `Supervisor` per connection.

**C. Several names on one bus connection.** Rejected: NM's signal
subscriptions would cross (above).

A wins because the `Supervisor` and every path M2 tested stay as they are:
one activation per process, its own exit, its own `ExecStopPost` cleanup,
its own `systemctl status` and journal. B saves one process start per
activation, but splits `service.py` into shared and per-activation parts,
turns every "exit" in the supervisor (idle exit, NM gone, SIGTERM in a
gap, a lost name) into "end this activation", and lets one crash end all
tunnels. A's cost is the dispatcher, about 120 lines, and a second process
start inside NM's 5 s window (to be measured, below).

## Design

### Slots and names

A slot is a number from 0 to 7. Everything an activation creates is named
after it:

| | v1 | Slot N |
|---|---|---|
| plugin unit | `nm-sshuttle.service` | `nm-sshuttle@N.service` |
| tunnel unit | `nm-sshuttle-tunnel.service` | `nm-sshuttle-tunnel@N.service` |
| DNS link | `nmss0` | `nmssN` |
| guard table | `inet nm-sshuttle-guard` | `inet nm-sshuttle-guard-N` |
| state | `/run/nm-sshuttle/tunnel.json` | `/run/nm-sshuttle/N/tunnel.json`, `bus-name`, `claim.json` |
| `tproxy` mark, table, rule | | see the [tproxy design](tproxy.md) |

`/run/nm-sshuttle/invisible-reconnect` stays global: it records NM
versions, not activations. Instances append to it with `O_APPEND`.

Every link keeps 192.0.0.8/32. Linux allows the same address on several
links and keeps its local route while any link still has it (inferred);
resolved needs only some non-link-local address on each (§2.3). This is
checked in the VM before anything else; if it fails, slot N uses a second
address from a documented range, at the price of a new address
convention.

### Activation path

```
NetworkManager ──spawns──► nm-sshuttle-activate --bus-name …sshuttle.Connection_37
                                   │ busctl call (auto-starts the dispatcher)
                                   ▼
       nm-sshuttle-dispatcher.service  (Type=dbus, …sshuttle.Dispatcher)
          picks slot 2, writes /run/nm-sshuttle/2/bus-name, StartUnit
                                   ▼
       nm-sshuttle@2.service  (Type=notify)  owns …sshuttle.Connection_37
          the v1 plugin with slot 2's names; NM calls Connect on it
                                   │ StartUnit / StopUnit
                                   ▼
       nm-sshuttle-tunnel@2.service  (reads /run/nm-sshuttle/2/tunnel.json)
```

1. **The `.name` file** sets `supports-multiple-connections=true`. NM then
   passes `--bus-name`.
2. **The shim** makes one call:
   `busctl call org.freedesktop.NetworkManager.sshuttle.Dispatcher
   /org/freedesktop/NetworkManager/sshuttle
   org.freedesktop.NetworkManager.sshuttle.Dispatcher Start s NAME`.
   The bus auto-starts the dispatcher, as `StartServiceByName` does today.
   Without `--bus-name` (an NM that has not reloaded the `.name` file,
   below) it passes the fixed name `org.freedesktop.NetworkManager.sshuttle`.
   NM's own domain already calls methods on the plugin (`Connect`), so the
   shim's call crosses the same SELinux boundary (inferred; the VM checks
   for denials).
3. **The dispatcher** accepts calls from uid 0 only and a name that is
   either the fixed name or the fixed name plus `.Connection_` and digits.
   It picks the lowest slot whose plugin unit is inactive or failed and
   that it has not handed out in the last few seconds, writes the name to
   the slot's `bus-name` file (mode 0600), starts `nm-sshuttle@N.service`,
   and returns the slot once the start job is queued. With all 8 slots in
   use it returns an error; NM then times out and fails the activation.
   It exits after 60 s idle. It holds no state that matters across its
   own restarts: the units' states are the truth.
4. **The plugin instance** reads its slot from `%i` and its bus name from
   the file, owns the name, then reports `READY=1` (a few lines writing
   to `$NOTIFY_SOCKET`; no new dependency). From there it is the v1
   plugin. It serves exactly one activation: once that has ended (STOPPED
   sent, teardown done, NM's `Disconnect` seen or 1 s passed) it exits,
   and when no `Connect` arrives within 60 s it exits too. Under the fixed
   name it keeps v1's idle window instead, because NM reuses a running
   plugin under that name (R5c).

The D-Bus activation file now names the dispatcher. No activation file
names a plugin bus name any more, so after a kill NM's `Disconnect` no
longer starts a fresh plugin (R4m); the instance's `ExecStopPost` has
already cleaned up, which in M2-09 it did first anyway.

### Units

```ini
# nm-sshuttle-dispatcher.service
[Service]
Type=dbus
BusName=org.freedesktop.NetworkManager.sshuttle.Dispatcher
ExecStart=/usr/libexec/nm-sshuttle/nm-sshuttle-dispatcher
```

```ini
# nm-sshuttle@.service
[Service]
Type=notify
NotifyAccess=main
ExecStart=/usr/libexec/nm-sshuttle/nm-sshuttle-service --slot %i
ExecStopPost=/usr/libexec/nm-sshuttle/nm-sshuttle cleanup --slot %i
Restart=no
OOMScoreAdjust=-500
TimeoutStopSec=20
```

`nm-sshuttle-tunnel@.service` is today's tunnel unit with
`exec-tunnel %i`. All of §4.7's reasons (no `BindsTo=`, `Restart=no`,
`KillMode=control-group`, the `ExecStopPost` sweep) hold per instance.

The dispatcher gets its own unit name rather than taking over
`nm-sshuttle.service`. During an upgrade a v1 plugin may still be running
as `nm-sshuttle.service`; a dispatcher with that name could not start
until it exits, and every activation in between would time out.

### Conflicts between active profiles

Two active profiles must not claim the same traffic or the same DNS. At
`Connect`, once the first hop is known and before the link or the guard
exist, the instance takes `/run/nm-sshuttle/claims.lock` (`flock`), reads
the other slots' `claim.json` files, checks, writes its own and releases
the lock. A claim counts while the PID in it is a running
`nm-sshuttle-service` with the start time the claim recorded
(design §4.8). The instance removes its claim when it sends
STOPPED, and `cleanup --slot N` removes it after a kill.

A claim holds the profile's id and UUID, its *effective networks* (the
subnets and the DNS servers' /32s, minus `exclude`, per family), its DNS
mode and domains, and the first hop's addresses. The new activation is
refused when:

1. its effective networks overlap another's, in either family;
2. both use `dns=all`;
3. both list the same DNS domain (ignoring a leading `~`); a domain and a
   subdomain of it are fine, because resolved picks the longest match;
4. it would close a loop of first hops: profile X *runs over* Y when X's
   first hop lies in Y's effective networks, and no chain may lead back
   to where it started.

The failure message names the other profile ("subnets overlap with the
active profile corp: 10.50.0.0/16"). GNOME shows only "Connection failed"
(§2.1), so `nm-sshuttle check` also runs these checks against the
profiles active at that moment.

Rule 4 is checked again when a roam changes the first hop. A loop found
then fails the activation whose first hop changed.

**Nesting, the static way.** A corporate profile with `10.0.0.0/8` and a
lab profile with `10.50.0.0/16` overlap. With `exclude=10.50.0.0/16` in
the corporate profile they do not: the corporate tunnel never carries the
lab network, and the lab profile's guard protects it while the lab VPN is
on. While it is off, traffic to the lab network is not guarded and goes
out on the uplink, as for any address no active profile covers.

**One tunnel over another.** When the lab's jump host lies inside the
corporate network, the lab tunnel's ssh goes through the corporate
tunnel (rule 4 allows it). Its first hop is excluded only in its own
sshuttle and guard, so the corporate redirect carries it, and the
corporate guard accepts it because it is redirected. When the corporate
tunnel has a gap, the lab tunnel's ssh dies too and the lab VPN
reconnects. So that it does not sit in a long backoff after the corporate
tunnel is back, an instance in a gap treats *another VPN connection
becoming activated* like a network change and tries at once (`watch.py`
already reads every active connection; this adds one callback).

### What each instance does differently

- **The health check** must look at its own sshuttle table, not at any
  table with a listener. The instance reads the tunnel unit's `MainPID`
  (sshuttle, which `exec-tunnel` became), finds the TCP sockets that
  process listens on (`/proc/PID/fd` against `/proc/net/tcp` and
  `tcp6`), and checks the tables (or, for `tproxy`, the chains) named
  after that port.
- **The startup cleanup and `cleanup --slot N`** remove only slot N's
  link, guard, routing, tunnel unit and files, then run the global sweep,
  which is safe across tunnels (above). `cleanup --all` removes every
  slot and the v1 names (`nm-sshuttle-guard`,
  `nm-sshuttle-tunnel.service`, `/run/nm-sshuttle/tunnel.json`); package
  removal and the first start after an upgrade run it while no instance
  runs.
- **NetworkManager going away:** the instance releases its bus name
  first, then removes its link and tears down as today. A restarted NM
  counts active connections from the start again, so its first
  activation may want a name an exiting instance still holds.
- **Sleep:** each instance holds its own delay inhibitor; logind waits
  for all of them. Nothing else changes.
- **The unmanaged check** (§4.1) reads `nmssN`'s state, as it reads
  `nmss0`'s today.

### NetworkManager configuration

`90-nm-sshuttle.conf` becomes `unmanaged-devices+=interface-name:nmss*`.
`post-install` reloads it as today, only when no nm-sshuttle VPN is
active.

### Upgrading from v1

- A v1 plugin that is running keeps its VPN until it ends; nothing
  restarts it (§4.4).
- Until NM reads the new `.name` file, it spawns the shim without
  `--bus-name`. The shim then asks for the fixed name, the dispatcher
  starts an instance under it, and everything works one tunnel at a time,
  as in v1. A second activation is refused by NM itself (above). Whether
  NM rereads the file at the update or only at its next start is a VM
  test.
- The first instance or dispatcher start after the update runs
  `cleanup --all` if no instance is running, to remove leftovers under the
  v1 names.

## What changes in the code

- `const.py`: names become functions of the slot (`link(slot)`,
  `guard_table(slot)`, `tunnel_unit(slot)`, `state_dir(slot)`).
- `supervisor.py`: takes the names from its `Effects` instead of
  importing `LINK`; the claim at `Connect`; rule 4 after a first-hop
  refresh; the kick when another VPN is activated.
- `service.py`: `--slot`, the bus name from the slot's file, `READY=1`,
  exit after the one activation, the name released first when NM goes,
  the health check's own port.
- New `dispatcher.py` (about 120 lines) and its unit, activation file and
  libexec wrapper.
- `guard.py`, `tunnel.py`: the slot in every name; `exec-tunnel SLOT`;
  the listener lookup by PID.
- `cli.py`: `cleanup --slot N` and `--all`; `check` runs the conflict
  rules against active profiles; `list` marks active profiles with their
  slot.
- `data/`: the `.name` file, the shim, the units, the activation file,
  the conf.d glob.
- Packaging: the new and renamed files; restart the dispatcher on update,
  never a plugin instance.

About 400 lines of Python, the dispatcher included, plus tests.

## Tests

- **Unit:** slot choice (lowest free, recently handed out, all in use);
  bus-name validation; each conflict rule, including effective networks
  with `exclude`, both families, nested domains and first-hop loops of
  two and three profiles; names per slot; the listener lookup by PID on
  fake `/proc` files.
- **D-Bus smoke test:** the dispatcher and two instances on the test bus
  with the fake NM: both connect, one gives up, the other stays up; a
  third with overlapping subnets is refused.
- **VM:**
  - two profiles up at once, each with split DNS through its own link,
    both with 192.0.0.8; reconnects of one leave the other untouched
    (M2-03 for each);
  - an overlapping third profile is refused with the other's name in the
    journal; `check` reports it;
  - `kill -9` of one instance: its `ExecStopPost` cleans only its slot;
  - an NM restart with two tunnels up: both tear down;
  - the lab tunnel over the corporate one: a corporate gap takes the lab
    tunnel down, and it comes back at once after the corporate one;
  - suspend with two tunnels (opt-in, as M2-28);
  - an upgrade from v1 under an active VPN: the old plugin keeps it, a
    second activation is refused until NM reloads the `.name` file, and
    the next activation after the old one ended uses slot 0;
  - the time from NM's spawn of the shim to the instance owning its
    name, cold and warm; it must stay well inside NM's 5 s;
  - no SELinux denials, in particular for the shim's call to the
    dispatcher.

## What was built

The design as above, with these results and details:

- **The same 192.0.0.8/32 on two links works:** the kernel keeps the
  `local` route while either link has the address, and each link keeps
  its own DNS in resolved (VM MT-01).
- **The pinned ssh config is per slot:** the first-hop pin of design §4.3
  arrived after this design; each tunnel writes its own
  `/run/nm-sshuttle-ssh/ssh_config-N`, so two tunnels behind different jump
  hosts do not overwrite each other's.
- **The health check finds the tunnel's own listener through its unit's
  cgroup** (`cgroup.procs` of `nm-sshuttle-tunnel@N.service`), not
  through `MainPID`, so it does not depend on which process holds the
  socket. It finds the cgroup under cgroup v2, a hybrid host's unified
  hierarchy or systemd's v1 hierarchy. Where none holds systemd's units,
  the plugin gives up at the first health check; it never falls back to
  every listener (design §4.8).
- **`list` marks active profiles without their slot:** the claims under
  `/run/nm-sshuttle/` are readable by root only.
- **An error while reading or writing the claims fails the activation**,
  as a conflict does, with the error as the reason.
- **An instance started by hand** (`systemctl start nm-sshuttle@0`), with
  no bus-name file, takes NM's fixed name.
- **The dispatcher** treats a slot as taken for 10 s after it handed it
  out, since systemd may not show the new unit as activating yet.
- **NetworkManager passes `--bus-name`** with a per-activation name
  (`….Connection_N`) to the shim (VM M2-02k).
- **The D-Bus policy names the dispatcher** rather than allowing
  `send_destination_prefix`: on Fedora 44, dbus-broker denied the shim's
  call to the dispatcher under the prefix rule. NetworkManager reaches
  the instances' per-activation names through the
  `org.freedesktop.NetworkManager.VPN.Plugin` interface, which the policy
  allows for root.
- **Start-up is far inside NM's 5 s:** in the VM, 257 ms from the
  dispatcher's start to the instance's `READY=1` when the dispatcher was
  not running, and 124 ms from `StartUnit` when it was (MT-03). With a
  corp gap, the lab over corp was back 643 ms after corp (MT-02).
- **sshuttle can outlive its ssh:** in about one MT-02 run in three,
  killing corp's sshuttle ended the lab's ssh, whose connection ran
  through it, but not the lab's sshuttle, which listened on with nothing
  behind it. The health check now wants an `ssh` process in the tunnel
  unit and reconnects without one, within 10 s.
- **Tests:** unit tests for the slot names, the conflict rules, the
  claims files, the slot choice, the listener lookup on a fake cgroup and
  `/proc`, the supervisor's claim, exit after one activation and the
  kick, and `cleanup --slot` and `--all`; a D-Bus test of the dispatcher
  against a fake systemd. The single-tunnel VM scenarios follow the slot
  of the newest instance; M2-09, M2-10 and M2-11 now expect one instance
  per activation. MT-01 covers two tunnels with their own DNS, a
  reconnect of one, a refused overlap, a kill and an NM restart; MT-02 the
  lab over corp; MT-03 the start-up time. Not automated: the upgrade from
  v1 and suspend with two tunnels.

## Open questions

- Whether NM rereads a `.name` file that a package update replaces.
- How long the cold path takes on slow hardware: two Python starts (the
  dispatcher and the instance) inside NM's 5 s, 257 ms in the VM. If it
  is tight, the dispatcher can stay running while any instance runs,
  which leaves one start on the path.
