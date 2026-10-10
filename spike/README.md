# M1 spike

This spike answers the open risks in
[`docs/design.md` §6](../docs/design.md#6-risks-to-retire-first) on a real
Fedora 44 system with NetworkManager, systemd-resolved, SELinux and GNOME,
before the real plugin is written.

One command on a Fedora 44 host does all of it:

1. creates a throwaway Fedora 44 VM;
2. installs GNOME in it;
3. runs a bare VPN plugin through the scenarios below;
4. copies the report back.

```console
$ sudo dnf install qemu-kvm qemu-img xorriso openssh-clients curl gnupg2 virt-viewer
$ spike/vm.sh
```

The report lands in `spike-results/<timestamp>/nmss-spike-report/report.md`,
with raw command output, journals and SELinux records in `raw/` next to it.

## What it checks

| ID | Question | How |
|---|---|---|
| R0 | Can root run ssh as the session user, with the key from the user's agent? | `ssh-as-user` against a jump host in a network namespace |
| R1 | Does NM accept the plugin when spawned directly (R1a), directly with a tunnel unit named `NetworkManager-*` (R1e), and as a shim that D-Bus-activates a systemd service (R1b)? Does `vpn-type sshuttle` resolve with no editor plugin (R1c)? | `nmcli connection up` in each mode; a failure must be reported within seconds, with its reason |
| R2 | Which SELinux domains do the plugin, sshuttle and ssh run in? Are there denials? | `busctl status`, `ps -o label`, new audit records per mode |
| R3 | Does NM address a plugin-created dummy `tundev` and hand its DNS to resolved as a separate split-DNS link? Do lookups then go through the tunnel? | `resolvectl`, a DNS server reachable only through the tunnel; plain (R3a) and `~` (R3b) domains, single-label names, the firewalld zone; `dns=none` with a link (R3c) |
| R4 | After a drop (STARTED → STARTING → STARTED), does NM show "activating" and then "activated" again, without deactivating or crashing? | One run per reconnect mode, with NM trace logging and a capture of the plugin's D-Bus signals: `identical` (R4a, expected stuck, then probed for an early "activated"), `nbns` sentinel with three drops (R4b), two-phase sentinel (R4i), `nbns` without firewalld (R4g), alternating address (R4c), `invisible` (R4d, stays activated), `nbns` with `dns=none` (R4e), and `relink-wait` (R4f, last, because relinking crashed NM 1.56.1). After each recovery: NM became "activated" only after the plugin's new `Config` (-v, which catches an early flip); whether NM committed the sentinel (-k); no sentinel WINS server left (-w); exactly one VPN DNS entry, on `nmss0` (-n); a name in the split domain resolves (-q); `nmss0` is not resolved's default DNS route and the uplink keeps its servers (-e); and NM has no non-VPN DNS entry for `nmss0` (-u, risk 11). NM's DNS configuration is read after `nmcli general reload dns-rc`, because NM caches it. R4b, R4g and R4c also remove the uplink's default route for a moment and check that `nmss0` does not take resolved's default route (-o). R4b repeats -u and the -o probe after its second and third drops (-u2, -u3). After a relink, or when NM's VPN entry is not on `nmss0`, a PASS of -w, -e, -u or -o is reported as INFO ("passes only trivially"); a FAIL stays a FAIL. A later drop that did not recover reports -u and -o as not evaluated. |
| R4h | Switching the VPN off during a reconnect: does it stay off, with nothing sent after `Disconnect` and nothing left behind? Is the pending reconnect cancelled? | `nmcli connection down` in the gap; the plugin is kept alive past its reconnect timer: -p checks that its PID is alive and owns the bus name, -k reads its log |
| R4j–R4l, R4p | When the plugin gives up from "connecting", does NetworkManager drop the VPN's DNS entry? | Three orders, with NM trace logging: link removed before STOPPED (R4j, runs 1–3), STOPPED first (R4k), the unchanged `Config` re-sent first (R4l, the design's order); and the link lost halfway through the gap (R4p; the loss flips NM to "activated", so the plugin bounces it back to "connect" first, and -b checks that the give-up ran from there; -a records whether NM flipped). The plugin keeps `nmss0` 5 s after NM deactivates, so resolved is read while the link exists (-k). The trace shows the state NM failed the VPN from; NetworkManager is restarted after a leak. |
| R4m, R4n | What happens when the plugin is killed (R4m) or its service stopped (R4n) during a gap? | `kill -9` (expected: NM keeps the VPN's DNS entry); `systemctl stop`, where the plugin gives up first (design §4.4) |
| R8 | Does an unmanaged `nmss0` remove risk 11 and the reapply flip, without breaking anything? | A packaged `/usr/lib/NetworkManager/conf.d` snippet with `unmanaged-devices+=interface-name:nmss0` (applied by restarting NetworkManager), then R3a (R8a), R4b with one drop (R8b), R4g (R8g) and R4c (R8c) with -o, the two-phase sentinel (R8j, expected to commit it: -k yes), R4a's early-flip probe (R8i), and the give-ups R4l (R8l), link loss in a gap (R8p, like R4p; -a expected yes, because link deletion still flips the VPN) and a kill in a gap (R8m, like R4m; -d expected to FAIL). R8 letters do not follow R4's. |
| R5 | Does the Quick Settings toggle switch it on and off (R5a to R5c)? Does GNOME show the reconnect (R5e)? What does GNOME Settings show without (R5f) and with (R5d) the auth-dialog stub? | Manual, with prompts |
| R6 | Does gcr-ssh-agent prompt to unlock a key when ssh runs outside the session with `BatchMode=yes` (R6)? Does a reconnect behind the lock screen with the key unloaded fail fast, without hanging (R6b)? | Manual, with a passphrase-protected key. R6b locks the screen itself; leave it locked until asked. |
| R7 | Does the plugin tear down when NetworkManager restarts under an active VPN? | `systemctl restart NetworkManager`, then look for leftovers |

R5 and R6 need you. The terminal tells you what to do in the VM window (which
opens automatically if `remote-viewer` is installed) and asks for y/n
answers. `--auto` skips them.

## Commands

```
spike/vm.sh [all|up|run|viewer|ssh|status|down|destroy] [options]
```

| Command | Does |
|---|---|
| `all` (default) | `up`, then `run` |
| `up` | Downloads the Fedora 44 Cloud image, checks its signed CHECKSUM against the Fedora 44 key in `/etc/pki/rpm-gpg`, boots the VM and provisions it (system update and GNOME, about 2–3 GB of packages). The first run takes a while; later runs reuse the VM. |
| `run` | Copies the current `spike/` into the VM, runs it and fetches the report. Re-run it after changing the plugin. |
| `viewer`, `ssh`, `status` | Look at, log in to or check the VM. |
| `down`, `destroy` | Stop it, or delete it. The downloaded image is kept. |

| Option | Effect |
|---|---|
| `--headless` | No GNOME. Much faster; R5 and R6 are skipped. |
| `--auto` | Skip the manual checks even with GNOME. |
| `--sshuttle-pip 2.0.0` | Test that sshuttle release from PyPI instead of Fedora's 1.3.x package. Use it on a fresh VM (`destroy` first). |
| `--mem`, `--cpus`, `--disk` | VM size (defaults: 4096 MB, 4 CPUs, 30 GB). |
| `--ssh-port`, `--vnc-display` | Host ports (defaults: 2244 and VNC :44 = 5944). |
| `--image FILE`, `--mirror URL` | Use a local image, or another Fedora mirror. |

## What it changes

- **On the host:**
  - Uses `~/.cache/nm-sshuttle-spike` (image, VM disk, SSH key) and
    `spike-results/`.
  - Runs QEMU as your user, with user-mode networking. Needs no root, and
    makes no libvirt or network changes.
  - The VM's SSH and VNC ports listen on 127.0.0.1 only. VNC has no
    password, and the VM user `tester` has the password `spike`, so don't
    run it on a shared host.
- **In the VM:** anything; it is disposable.
  - `spike.sh` undoes its own changes at the end anyway (`--keep` leaves
    them for poking around; `spike.sh cleanup` removes them later).
  - That cleanup is what would make it bearable on a real machine, but it is
    meant for the VM.

## Files

| File | Role |
|---|---|
| `vm.sh` | Host side: image, VM, provisioning, run, report. |
| `provision.sh` | Inside the VM: packages, GNOME, autologin without screen lock. |
| `spike.sh` | Inside the VM: installs the plugin, builds the network-namespace topology, runs R0–R7, writes the report. |
| `plugin/nm-sshuttle-spike-service` | The bare VPN plugin. Python and Gio; implements `org.freedesktop.NetworkManager.VPN.Plugin` directly. |
| `plugin/nm-sshuttle-spike-activate` | The shim `program=` for D-Bus activation. |
| `plugin/nm-sshuttle-spike-auth-dialog` | `[GNOME] auth-dialog=`: answers GNOME's secrets requests with no secrets. |
| `plugin/exec-tunnel` | ExecStart of the tunnel unit. Works out the first hop as the user, then execs sshuttle. |
| `plugin/ssh-as-user` | sshuttle's `-e`: ssh as the user, with the user manager's agent and display. |
| `plugin/sweep` | Removes stale sshuttle nft tables (no listener on their port). |

## Already verified, and not yet

- **Verified in a container.** The plugin's D-Bus protocol was smoke-tested
  against a real NetworkManager 1.46 (Ubuntu 24.04), started directly by
  NM, without systemd. It covered:
  - activation with an empty `tundev` and with a `tundev`;
  - NM addressing the link;
  - traffic through the tunnel;
  - failure reporting for a rejected profile, an unreachable host (4 s) and
    a denied `systemctl start` (0.2 s, with a fake systemctl);
  - clean deactivation and idle exit;
  - the reconnect modes: `identical` stays "activating", `nbns`, `perturb`
    and `relink-wait` return to "activated", and `invisible` stays
    "activated";
  - NetworkManager killed and restarted: the plugin removes `nmss0` and its
    tables and exits.
- **Checked here, short of a real boot.** The QEMU command line, the
  cloud-init seed and `vm.sh status` and `destroy` were checked against a
  blank disk.
- **First Fedora run (2026-10-07).** The VM came up and R0 passed, with the
  gcr agent exported by default. R1 failed because the `.name` file went to
  `/etc/NetworkManager/VPN`, which Fedora does not have; it now goes to
  `/usr/lib/NetworkManager/VPN`. The SELinux check could not read the audit
  log by date; it now reads new audit records by file offset.
- **Second Fedora run (2026-10-07).** Findings, each checked against the
  logs and the upstream sources (details in
  [`docs/design.md` §2.1](../docs/design.md#21-networkmanager-and-gnome)):
  - **R1, R2:** the shim works, with plugin and tunnel in
    `unconfined_service_t` and no denials. Spawned directly, the plugin runs
    as `NetworkManager_t`, and systemd refuses to start its tunnel unit. The
    plugin then hung for 60 s instead of failing: an exception in a GLib
    callback was lost. Every entry point is now guarded.
  - **R3:** split DNS through the dummy link works, with both domain forms.
  - **R4:** an unchanged config leaves NM at "activating" (a NetworkManager
    regression since 1.36). Recreating the link crashed NetworkManager
    1.56.1 with SIGSEGV. The plugin now keeps the link, waits for NM's
    device before `Config`, and tries the modes listed in R4.
  - **R5:** the toggle works. Settings shows a spinner because there is no
    auth dialog (a GNOME Shell bug makes it hang); the stub should fix it.
    The reconnect question (R5e) was asked during the crash, so it is asked
    again.
  - **R6:** gcr asked for the passphrase of a key that was not loaded, and
    the tunnel came up.
  - The ssh label (R1b-s) was empty because of a bad `ps` call; fixed.
- **After the second run.** A review of the driver and plugin against
  NetworkManager 1.56 and the run-2 logs found that trace logging was never
  on (NetworkManager has no `L3CFG` log domain, so it rejected the whole
  request), that a direct-mode fallback would have reused the denied unit
  name, and that R6b could pass on a hang. All fixed. The changed and new
  R4 phases (R4a with its early-flip probe, R4b with three drops, R4h, R4i,
  R4j, signal counts) were run through the driver's own functions against
  NetworkManager 1.46 in the container, and all passed. There, nothing
  flipped a stuck reconnect to "activated", and the give-up removed the
  VPN's DNS. R4g needs firewalld, which the container lacks.
- **Third Fedora run (2026-10-09).** Every phase ran, with NetworkManager
  trace logging and a capture of the plugin's D-Bus signals for each
  reconnect. Findings, checked against the traces and the upstream sources
  (details in
  [`docs/design.md` §2.1](../docs/design.md#21-networkmanager-and-gnome)):
  - **R1, R2:** the shim works, with no denial records in the whole run
    except R1a's. Spawned directly, the plugin was refused even
    `systemctl is-active` on an ordinary unit name (five `USER_AVC`), and
    reported that in 0.14 s instead of hanging. With a `NetworkManager-*`
    unit name (R1e) it started and stopped the tunnel with no denials, but
    only on a minimal path: `dns=none`, no `nmss0`, under a second up. The
    user's ssh runs as `unconfined_service_t` in both modes.
  - **R3:** split DNS works as in run 2. Only a plain domain completes a
    single-label name. `nmss0` lands in firewalld zone `public`, and with
    `dns=none` the link has no DNS.
  - **R4:** the `nbns` sentinel brought NetworkManager 1.56.1 back to
    "activated" every time: three drops with firewalld (R4b), without
    firewalld (R4g), with `dns=none` (R4e) and in two phases (R4i), each
    24–30 ms after the plugin's `Config`. The alternating address works
    too (R4c), and the invisible reconnect stays "activated" (R4d). An
    unchanged config stays stuck (R4a) until `nmcli device reapply nmss0`
    flips it to "activated" (R4a-i); adding or removing an address does
    not.
  - **The traces correct the mechanism.** With firewalld running,
    NetworkManager never commits the sentinel; the real config commits
    because it is a new object. Without firewalld the sentinel is committed
    for about 1 ms. So R4b-w, R4e-w, R4c-w and R4f-w checked little.
  - **R4f's PASS is spurious.** Deleting the old `nmss0` flipped the VPN to
    "activated" 79 ms before the plugin's new `Config`, and the new link
    never got its DNS (R4f-d FAIL). NetworkManager did not crash this time.
  - **R4j's PASS depends on the plugin's order.** The plugin deleted
    `nmss0` before sending STOPPED. NetworkManager's commit on the dead
    link most likely moved the VPN out of "connect" first, and that is what
    made it drop the DNS. R4j ran without trace. Design risk 9 stays open.
  - **R4h:** switching off during a gap works. But the plugin process idled
    out before its reconnect timer was due, so cancelling that timer was
    not tested.
  - **R5:** the toggle works, and a reconnect raised no notification (R5e).
    The question asked about "connecting", which GNOME 50.5 shows only as
    an icon. Without the auth-dialog stub, Settings waited 25 s and GNOME
    Shell logged an unhandled promise rejection (R5f). With the stub the
    editor opened at once (R5d).
  - **R6:** the unloaded key was unlocked on the first try (9 s, typing
    included). Behind the lock screen the reconnect failed in 1 s through
    gcr's cancelled prompt (R6b). The spike reports that as "connect
    failed"; it has no wait-for-unlock logic.
  - **R7:** the plugin tore everything down within about 0.1 s of
    NetworkManager's exit. It removed `nmss0` only after the new
    NetworkManager had started.
  - **Also seen:** once in 29 tunnel stops, systemd's kill hit sshuttle's
    own `nft delete table`, and only the unit's `ExecStopPost` sweep
    removed the table.
  - **Not captured:** firewalld, dispatcher and dbus-broker logs; a trace
    for R4a-i and R4j; NetworkManager's DNS entries after the reconnects;
    the selinux-policy version.
- **After the third run.** The driver now restarts NetworkManager before
  each run, keeps trace logging on through the early-flip probe and the
  give-up tests, adds firewalld's, the dispatcher's and dbus-broker's logs,
  and records the selinux-policy version. The plugin removes `nmss0` first
  when NetworkManager vanishes, cancels a pending reconnect on
  `Disconnect`, and can give up in three orders. Against NetworkManager
  1.46 in the container, the new checks caught relinking's early flip
  (R4f-v) and its stale DNS entry (R4f-n). Of the give-up orders, STOPPED
  first left the VPN's DNS entry registered, while re-sending the
  unchanged `Config` first removed it.
- **Fourth run (2026-10-09).** Unattended: `spike/vm.sh run --auto` on the
  existing VM. It ran with trace logging through every reconnect, the
  early-flip probe and the give-ups, and it dumped NM's DNS entries after
  each. The findings below were checked against the traces and the
  upstream sources (details in
  [`docs/design.md` §2.1, §4.4 and §4.5](../docs/design.md#21-networkmanager-and-gnome)):
  - **R0 to R3:** same results as run 3, with no denial records besides
    R1a's five.
  - **R4:** the `nbns` sentinel recovered every time, 28–40 ms after
    `Config`; run 4 logged more at debug level. A split name resolved after
    every reconnect that kept the link (-q).
  - **The give-up order (risk 9) is decided on 1.56.1.**
    - `giveup-config` (R4l) failed the VPN from ip-config-get and removed
      its DNS entry while `nmss0` still existed, with no warnings.
    - `giveup-stopped` (R4k) failed it from "connect" and left the entry in
      NetworkManager until the driver restarted it (R4k-d FAIL).
    - `giveup` (R4j) removed the entry only because deleting the link
      flipped the VPN to pre-up first. NetworkManager then tried to re-add
      the address and got a NoSuchLink error from resolved.
  - **Risk 11 is observed (R4c-o, reported only as INFO).** After R4c, R4g
    and R4i, NetworkManager held a second, non-VPN DNS entry for `nmss0`.
    With the uplink's default routes removed:
    - resolved moved DefaultRoute to `nmss0`;
    - NetworkManager reset the uplink's DNS server list;
    - an outside name went through the tunnel to the lab's DNS server.
  - **R4f:** relinking flipped the VPN to "activated" 80 ms before the new
    `Config` and left the new link without DNS (R4f-v, R4f-d and R4f-q
    FAIL). R4f-n's PASS is spurious: `DnsManager.Configuration` is a
    snapshot from the last DNS change, and it still called the deleted link
    `nmss0`.
  - **R4a-i is traced now.** The reapply's commit released the stuck VPN.
    It also filed a non-VPN `nmss0` DNS entry.
  - **R4h:** the plugin cancelled its pending reconnect at `Disconnect`
    (R4h-p, R4h-k).
  - **R7:** the plugin removed `nmss0` first, and the restarted
    NetworkManager did not list it.
  - **Checks that do not discriminate:**
    - R4x-r runs after the plugin has deleted `nmss0`, so it passed despite
      R4k's leak.
    - R4x-n reads a stale property and ignores non-VPN entries, so R4c, R4g
      and R4i passed with two `nmss0` entries.
    - R4c-o recorded no `resolvectl dns` and made no lookup.
  - **Also seen:**
    - systemd's kill hit sshuttle's own `nft delete table` twice in 28
      stops (3 in 57 over runs 3 and 4).
    - An uplink commit inside R4e's gap did not flip the VPN.
    - The R4h plugin exited through its 60 s startup timer, not its idle
      timer.
    - relink-wait ran `ip link del nmss0` twice.
  - **Not captured:**
    - firewalld's own log;
    - units outside the journal filter, so the process that looked up
      fedoraproject.org during R4c-o is unknown;
    - a DNS dump after R4f's teardown.
- **Fifth run (2026-10-10).** Unattended: `spike/vm.sh run --auto` on the
  run-4 VM, in the same boot; the driver restarted NetworkManager first.
  New in the driver: its own NetworkManager snippet uses
  `unmanaged-devices+=`; the DNS checks read resolved per link and NM's
  configuration after `nmcli general reload dns-rc`; the no-default-route
  probe (-o) is a check with lookups. New phases: the R8 unmanaged-`nmss0`
  set, the plugin killed or stopped in a gap (R4m, R4n), and a give-up
  after the link is lost (R4p). The findings below were checked against
  the traces and the upstream sources (details in
  [`docs/design.md` §2.1, §4.1, §4.4 and §4.5](../docs/design.md#21-networkmanager-and-gnome)):
  - **R0 to R3:** same results as run 4, with no denial records besides
    R1a's five. R5 and R6 were skipped (`--auto`).
  - **An unmanaged `nmss0` retires risk 11 (R8).**
    - With the packaged `conf.d` snippet, NetworkManager still addressed
      `nmss0`, pushed the VPN's DNS and bound the firewalld zone. It
      generated no `nmss0` profile and no second active connection.
    - Every R8 check passed. No non-VPN DNS entry appeared after R8b, R8g
      or R8c (-u), and `nmss0` never took resolved's default route (-o).
      Only R8c's commit changed the content, so R8c is the one sample that
      tells the two forms apart.
    - `nmcli device reapply nmss0` was refused ("Device is not activated")
      and left the stuck R8i alone. The R8l give-up was clean.
    - The design adopts the snippet (§4.1).
  - **With a managed `nmss0`, risk 11 follows the commit.** R4i and R4c
    filed the non-VPN entry again (-u FAIL), and R4c-o moved resolved's
    default route to `nmss0` (FAIL, as expected). R4g passed -u and -o,
    because this time NetworkManager did not commit the sentinel. Without
    firewalld that is a race, and run 4 lost it. R4b's three drops never
    changed the committed content.
  - **Give-ups:**
    - `giveup-config` (R4l) behaved as in run 4.
    - STOPPED first (R4k) leaked again, now shown in a rebuilt dump.
      resolved kept the VPN's server on `nmss0` while the link existed
      (R4k-k FAIL).
    - Losing the link in a gap (R4p) flipped the VPN to "activated" for
      62 ms, with vpn-pre-up and vpn-up on the uplink. The give-up then
      made NetworkManager fail the VPN with "IP configuration invalid" and
      drop its DNS.
    - Stopping the service in a gap (R4n) gave up first and left nothing
      behind.
    - A SIGKILL in a gap (R4m) leaked the DNS entry and left `nmss0`
      behind. NetworkManager's `Disconnect` D-Bus-activated a fresh plugin
      in the same second, and that instance knew nothing of the link.
  - **R4f:** relinking flipped the VPN 121 ms before the new `Config`. It
    left the VPN's entry on the dead ifindex, also after `Disconnect`.
    R4f-n now fails, because the dump is rebuilt. No crash.
  - **R4h-p FAILED, but the plugin was alive.** It ran until its idle exit
    60 s after STOPPED and had cancelled its reconnect (R4h-k). The failing
    check was a `busctl --system status` whose output went to the terminal.
    Why it failed is unknown: `check()` keeps no output.
  - **R7:** as in run 4, with a managed `nmss0`. The R8 snippet had been
    removed before R7.
  - **Timing:** from the drop to the new `Config` took 20.46–21.27 s with a
    20 s timer; the plugin's whole-second timer and the tunnel start add
    the rest. From `Config` to "activated" took 22–29 ms, also with `nmss0`
    unmanaged. The report's "+21s" rounds up.
  - **Also seen:**
    - systemd's kill hit sshuttle's own `nft delete table` once in 41
      stops (4 in 98 over runs 3 to 5), and 7 stops logged a harmless
      `ProcessLookupError`.
    - During R4c-o, a lookup of fedoraproject.org reached the internal
      server before the driver's own lookup. It was most likely
      NetworkManager's connectivity check.
    - Every `CONNECT_FAILED` give-up ended with reason 0.
  - **Checks that do not discriminate:**
    - -u and -o run after the first drop only.
    - R4f-u, -e and -w pass trivially after a relink.
    - -z (`public`) cannot tell a binding from firewalld's default zone.
    - -o3 checks the uplink's servers, not its default route.
    - The `+N s` timings round to whole seconds.
  - **Not captured:**
    - firewalld's own log;
    - busctl's output for R4h-p;
    - sub-second timestamps in `journal-all.txt` and `plugin.log`;
    - dispatcher and firewalld debug outside the trace windows.
- **After the fifth run.**
  - The design adopts the unmanaged `nmss0` for M2 (design §4.1). M1 is
    done. What is still open goes to M2's VM tests (design §4.10).
  - R4h-p now checks that the plugin's PID is alive and owns its bus name.
    The driver also records `busctl status` once, without a pager and with
    its exit code, in `raw/reconnect-R4h.txt`.
  - The driver reads -u after every drop of R4b (-u2, -u3) and repeats
    the -o probe there.
  - After a relink, a PASS of R4f-w, -e or -u is reported as INFO,
    because it would pass trivially; a FAIL stays a FAIL.
  - -o3 also checks the uplink's IPv4 default route and its resolved
    default route.
  - -z says whether the zone is firewalld's default zone.
  - The give-ups record the firewalld zone of `nmss0`.
  - Each check's start time goes to `raw/check-times.txt`, and a failing
    check's output to `raw/check-fail.txt`.
  - `journal-all.txt` and `plugin.log` have sub-second times.
  - No systemd tool starts a pager.
- **Sixth run (2026-10-10).** Optional and unattended: `spike/vm.sh run
  --auto` on the same VM and boot as run 5, with the driver at commit
  552ec4d; the driver restarted NetworkManager first. It ran the changes
  listed after the fifth run and the three R8 phases that run 5 left out.
  Every FAIL was on the expected lists (R1a-x, R4-x, R8-x). The findings
  below were checked against the traces and the upstream sources (details
  in
  [`docs/design.md` §2.1, §4.4, §4.5 and §4.10](../docs/design.md#21-networkmanager-and-gnome)):
  - **R0 to R3:** same results as run 5, with no denial records besides
    R1a's five. R5 and R6 were skipped (`--auto`).
  - **The unmanaged `nmss0` holds in the gaps run 5 left open.**
    - **Two-phase sentinel (R8j).** NetworkManager committed the sentinel
      (-k yes) and then the real config, both with `l3cd-changed=1`. Each
      time it logged "DNS configuration did not change", so it filed no
      non-VPN entry (-u PASS), and `nmss0` kept out of resolved's default
      route (-o PASS). The same reconnect with a managed `nmss0` (R4i)
      filed the entry again.
    - **Link loss in a gap (R8p).** Deleting the link still flipped the VPN
      to "activated", for 88 ms (R4p: 69 ms), with vpn-pre-up and vpn-up on
      the uplink. The give-up's re-sent `Config` then made NetworkManager
      fail the VPN with "IP configuration invalid" and drop its DNS (-b and
      -d PASS). Unlike R4p, there was no external device to deactivate and
      no generated profile to delete.
    - **A kill in a gap (R8m)** leaked the DNS entry (R8m-d FAIL) and left
      `nmss0` behind, as R4m did. NetworkManager removed the address and
      the firewalld zone and did not assume the link. The plugin instance
      that NetworkManager's `Disconnect` started answered "already stopped"
      and left `nmss0` alone; the spike plugin has no startup cleanup.
    - Still open with `nmss0` unmanaged, for M2's VM tests (design §4.10):
      a service stop in a gap; `nmcli device set nmss0 managed yes|no` in a
      gap; a NetworkManager restart under an active VPN (R7 again ran with
      `nmss0` managed); a NetworkManager restart while a killed plugin's
      `nmss0` survives; applying the snippet with `nmcli general reload
      conf`; a user override. That the instance `Disconnect` starts removes
      `nmss0` is untested in both forms.
  - **With a managed `nmss0`, risk 11 followed the commit again.** R4i and
    R4c filed the non-VPN entry (-u FAIL), and R4c-o moved resolved's
    default route to `nmss0` (FAIL, as expected). R4g did not commit the
    sentinel (-k no) and passed -u and -o. R4b passed -u2, -u3 and the -o
    probes after drops 2 and 3.
  - **The sentinel commit, runs 4 to 6.** With firewalld running, the burst
    never committed the sentinel (0 of 14 reconnects). Without it, it did
    once in 5 (run 4's R4g). Without firewalld there were three orders:
    the sentinel's zone call won (run 4); the `Config`'s own call won and
    the sentinel's was cancelled (run 5's R4g and R8g, run 6's R8g); or
    both were cancelled and only the real config applied (run 6's R4g).
    What decides it is whether the sentinel's "fake success" idle runs
    before NetworkManager dispatches the real `Ip4Config`.
  - **R4h-p passed.** The plugin was alive and owned its bus name when its
    reconnect would have fired, and the recorded `busctl status` exited 0.
    This says nothing about run 5's FAIL, whose cause stays unknown.
  - **R4f:** relinking flipped the VPN 87 ms before the new `Config`, with
    the same failures as in run 5 (R4f-v, -d, -q, -n). No crash.
  - **R7:** as in run 5, with a managed `nmss0`. The plugin exited 0.11 s
    after systemd stopped NetworkManager, and the new NetworkManager did
    not list `nmss0`.
  - **Timing** (the two open items of run 5):
    - For the reconnects, from the drop to the new `Config` took
      20.2–21.1 s. Of that, the plugin's synchronous `systemctl stop` took
      23–37 ms, its 20 s `timeout_add_seconds` timer fired after
      19.80–20.69 s, the tunnel start took 0.27–0.37 s, and finding
      NetworkManager's device took another 102–104 ms, because the first
      poll waits a fixed 100 ms (R4f's relink: 120 ms, for a new device).
      R4l's give-up re-sent its `Config` at the timer, after 20.0 s, with no
      tunnel start. Only the timer varies much: from GLib's source, a
      whole-second timer is rounded to a fixed point within the second.
    - From `Config` to "activated" took 19–29 ms, also with `nmss0`
      unmanaged. Pre-up came 1.1–1.3 ms after `Config` without firewalld
      and 3.5–6.0 ms with it, mostly the zone call. The rest, 17.5–24.3 ms,
      is the vpn-pre-up dispatcher call with no scripts. Almost all of that
      is the D-Bus activation of NetworkManager-dispatcher: it exits after
      10 s idle, so after a 20 s gap it always starts cold.
  - **Also seen:**
    - After a link loss (R4j, R4p, R8p), firewalld still put `nmss0` in
      zone `public` although the link was gone: NetworkManager skips the
      zone removal for an interface that has no name any more. After the
      give-ups with the link present it said "no zone".
    - The vpn-up that R8p's flip dispatched on the uplink ran four real
      dispatcher scripts, and no vpn-down followed. In R4j the flip stopped
      at pre-up, so only vpn-pre-up was dispatched.
    - In R8p the plugin's `Failure` and STOPPED arrived after NetworkManager
      had already failed the VPN on the re-sent `Config`, so it never
      logged them. The outcome was the same as in R4p.
    - An uplink commit inside R8b's gap did not flip the VPN.
    - sshuttle's cleanup left a stale nft table once in 44 stops (R8m's
      drop), in a new way. The helper was already deleting the table when
      systemd's SIGTERM came, and a `ProcessLookupError` aborted
      `nft delete table`. The `ExecStopPost` sweep removed the table. No
      stop logged "returned -15".
    - During R4c-o only the driver's own lookups reached the internal
      server, so run 5's extra lookup stays unexplained.
    - NetworkManager started eight times, and every instance exited
      cleanly. Every NetworkManager warning in the journal belongs to an
      expected path.
  - **Checks that do not discriminate:**
    - -w passes trivially in modes that send no sentinel (R4c-w, R8c-w).
    - -z cannot prove NetworkManager's binding:
      `--get-zone-of-interface` already reports only bound interfaces, and
      a binding left over from a link loss reads the same.
    - -k matches NetworkManager's l3cd IDs, which it reuses. The driver's
      per-drop slices gave the right answers, but the same match over a
      whole trace does not.
    - R4b's later drops check none of -v, -d and -q.
    - R7's "0 s" counts whole seconds from before the activation.
  - **Not captured:**
    - resolved's state on `nmss0` between a kill and the link's deletion
      (R4m, R8m); the driver's `nmcli general reload dns-rc` calls in that
      window also push the leaked entry again;
    - what a failing DNS check saw: `raw/check-fail.txt` has only the
      command and its exit code, because the helpers print nothing;
    - the routes during the -o probe;
    - a trace of the first activation;
    - firewalld's own log.
- **After the sixth run.**
  - M1 stays done. The harness defects above are not fixed in the spike.
    Also: R4f-z is used twice (the count and the restart), and -z has three
    meanings; verdicts set by `result()` (R7, R4b-r, the crash checks) are
    missing from `raw/check-times.txt`; R4h-r mixes the VPN's active
    connection with `nmss0`'s; and the shim's `busctl` prints "u 1" into
    NetworkManager's journal.
  - M2's VM tests should print what a failing check saw, use unique IDs,
    time from the event with sub-second precision, classify a sentinel
    within its own reconnect, read resolved after a kill before any DNS
    reload, and take a zone binding from NetworkManager's trace.
  - Seen in the spike plugin, to avoid in M2's: the instance that a
    `Disconnect` starts never arms its idle timer and would have lingered
    until its 60 s startup timer; the first device check waits 100 ms; the
    tunnel stop blocks the main loop.
- **Sharing a run.** `report.md` has details for R1a and R2, and
  `raw/check-fail.txt` lists every failing check with its command and exit
  code. To share everything, send the `nmss-spike-report.tar.gz` that
  `vm.sh` saves next to it.
