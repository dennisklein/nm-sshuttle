# nm-sshuttle: design proposal

Status: proposal, updated 2026-10-10 after the sixth Fedora 44 spike run.
Nothing is built yet. The code so far is the research lab in
[`lab/`](../lab), which checks the sshuttle and kernel behaviour this design
relies on, and the spike in [`spike/`](../spike), which checks
NetworkManager, SELinux and GNOME on Fedora 44.

## 1. Recommendation in brief

Build a **small NetworkManager VPN service plugin in Python, without a GUI
editor**; a ten-line auth-dialog stub is its only GNOME part. It runs as a D-Bus-activated systemd service and supervises
sshuttle, which runs in a second systemd unit.

- **The toggle.** GNOME Quick Settings shows a VPN toggle for every
  NetworkManager connection of type `vpn` or `wireguard`, and nothing else.
  A VPN plugin is the only way to get the WireGuard-like toggle without a
  Shell extension. The toggle works without an editor plugin; profiles are
  created with a small CLI that wraps `nmcli`.
- **The privilege split.** sshuttle and its firewall helper run as root. Only
  `ssh` runs as the desktop user, through a small bridge. The user's
  `~/.ssh/config` (aliases, ProxyJump), `known_hosts` and agent work
  unchanged, and the agent unlocks keys inside the session as usual. No
  sudoers rule is needed, and root never reads `~/.ssh`.
- **Lifecycle.** The plugin tells NetworkManager it is *persistent*. Once the
  toggle is on, it stays on across suspend, roaming and drops while the
  plugin reconnects by itself. While the tunnel is down, an nftables guard
  table makes traffic to the tunnelled networks **fail closed** instead of
  leaking to the local network. A NetworkManager bug means a reconnect
  shows as "connected" again only through a workaround; it works on
  Fedora's 1.56.1 (§2.1, §4.4).
- **DNS.** Split DNS goes through a per-tunnel dummy link. NetworkManager
  hands that link's DNS servers and routing domains to systemd-resolved,
  and sshuttle captures the queries sent to those servers.

The four open questions from the brief, answered:

| Undecided | Proposal |
|---|---|
| Full VPN plugin or lighter integration | A **thin VPN service plugin**: no editor, and an auth dialog that is a stub. A GTK4 editor is optional later work. |
| One tunnel or several profiles | **Many profiles** (ordinary NetworkManager connections), **one active at a time** in v1. |
| Target distributions and GNOME | NetworkManager ≥ 1.42, systemd-resolved, nftables, sshuttle ≥ 1.3.1 (2.0.0 preferred). Test on Fedora 43/44, Ubuntu 26.04 LTS and Debian 13. That covers GNOME 48 to 50. |
| Implementation language | **Python ≥ 3.10** with PyGObject (Gio D-Bus), the same runtime sshuttle needs. No compiled code in v1. |

## 2. What the research found

Sources: NetworkManager `main` (1.59.2-dev) and 1.56.1, gnome-shell `main`
(51.0) and 50.5, gnome-control-center 50.4, gcr, Fedora's selinux-policy
(f44), systemd `main` and v259, NetworkManager-ssh `master`, sshuttle 2.0.0
(PyPI sdist), web research, the lab in [`lab/run.sh`](../lab/run.sh), a
smoke test of the [spike plugin](../spike) against NetworkManager, and six
spike runs on Fedora 44 (2026-10-07 to 2026-10-10). The third to sixth
had NetworkManager trace logging during every reconnect. The fourth to
sixth also had it during the give-ups and dumped NM's DNS entries after
each; the fifth and sixth rebuilt them first (`nmcli general reload
dns-rc`). Each item is marked:

- **[src]**: read in the source code.
- **[lab]**: reproduced in the lab; the test ID follows.
- **[smoke]**: observed with the spike plugin against a real
  NetworkManager 1.46 (Ubuntu 24.04 container, no systemd).
- **[spike]**: observed by [`spike/`](../spike) on Fedora 44 (NetworkManager
  1.56.1, GNOME 50.5, SELinux enforcing).
- **[reported]**: from documentation, issue trackers or search results, not
  checked.

### 2.1 NetworkManager and GNOME

- **[src] NM spawns the plugin itself.** It runs `program=` from the `.name`
  file directly as root (`g_spawn_async`), in NetworkManager's own cgroup.
  It then waits 5 s for the plugin's D-Bus name to appear. With
  `supports-multiple-connections=true` it passes `--bus-name`.
  - **[spike] It also stays in NetworkManager's SELinux domain and
    cgroup.** On Fedora 44 a plugin spawned this way runs as
    `NetworkManager_t`, in `NetworkManager.service`'s cgroup, with that
    unit's eleven capabilities (R1e-c). systemd refused its `systemctl`
    calls on a tunnel unit labelled `systemd_unit_file_t`: five enforcing
    `USER_AVC` records, for `{ status }` (is-active, show), `{ start }` and
    `{ stop }` (R1a). systemctl reports that only as "Access denied". The
    failure reached NM 0.14 s after the start.
  - **[spike] A unit named `NetworkManager-*` gets past it** (R1e). In
    `/etc/systemd/system` such a unit file gets the
    `NetworkManager_unit_file_t` label through `subs_dist`, and the plugin
    started, queried and stopped it with no denial records. The run was
    minimal: `dns=none`, no `nmss0`, no `ip` or `nft`, no journal read,
    and under a second up.
  - **[src]** From `NetworkManager_t`, `journalctl` and transient units are
    denied, and `ss` cannot open its sock_diag socket. ss then falls back
    to `/proc/net/tcp`, but each call logs an AVC.
  - **[spike] D-Bus activation sidesteps it.** With a `program=` shim that
    calls `StartServiceByName` and exits, the plugin, the tunnel and the
    user's `ssh` ran as `unconfined_service_t` (R1b, R1b-s). Run 3 logged
    no AVC, `USER_AVC` or `SELINUX_ERR` records besides R1a's five.
    Denials hidden by `dontaudit` rules were not looked for.
- **[src] A VPN does not need its own interface.** `tundev` may be empty, and
  NM then uses the parent device. What NM does require is the external
  `gateway` address; without it the config is rejected ("no VPN gateway
  address received"). This corrects the brief: a device is needed only for
  per-link DNS in resolved (§4.5), not for NM.
  - **[src]** Without one, though, a reconnecting VPN waits for a commit
    on the uplink's IP configuration, and routine uplink commits would flip
    it to "activated" early (see the caveat below). **[spike]** In run 3
    one such commit, an IPv6 RA refresh on the uplink, came 3.5 s after R4e
    recovered. In run 4 one landed inside R4e's gap, 1.75 s before the
    plugin's `Config`, and in run 6 one inside R8b's gap, with `nmss0`
    unmanaged. Neither flipped the VPN, because NM waits on `nmss0`
    **[src]**. This design keeps `nmss0` for that reason too.
- **[src] The IP config is optional.** `has-ip4` and `has-ip6` may both be
  false, and within an IPv4 config the address is optional too. DNS servers
  and domains can be pushed on their own. Since NM 1.42,
  `ipv4.auto-route-ext-gw` can suppress the host route NM adds towards the
  gateway.
- **[src] NM does not tell the plugin who activated it.** `vpn.user-name` is
  filled in only from the secret agent that answered a secrets request. A
  plugin that needs no secrets cannot learn the activating user from NM, so
  the profile must name the local user (§4.2).
- **[src] VPN profiles cannot autoconnect.** "Autoconnect is not implemented
  for VPN profiles"; the documented alternative is `connection.secondaries`.
  But a failing secondary fails its base connection
  (`SECONDARY_CONNECTION_FAILED`). With secondaries, an unreachable jump host
  would take the Wi-Fi connection down, so this design does not use them.
- **[src] Persistence is built in.** If the plugin reports `can-persist` and
  the profile has `vpn.persistent=yes`, NM keeps the VPN while the
  underlying device goes away (including at sleep). It then re-attaches the
  VPN to the new default device.
  - A plugin that goes from STARTED back to STARTING puts the VPN into
    "connecting" without tearing it down.
  - NM arms its connect timeout only on the first activation, so a
    reconnect can last as long as the plugin wants.
  - Without `can-persist`, `vpn.persistent=yes` only makes NM re-activate
    straight away after a failure that follows a successful connect. There
    is no backoff, and NM does not wait for connectivity.
- **[smoke] [spike] A reconnect with an unchanged config never completes.**
  After STARTED → STARTING, the plugin re-sends `Config`, `Ip4Config` and
  STARTED with the same content. NM then stays at "activating" while
  traffic flows: indefinitely on 1.46 in the container, and on Fedora's
  1.56.1 for 41 s after the re-sent config, until the driver's `nmcli
  device reapply` released it (R4a, run 3). NM's trace shows connect,
  ip-config-get and "apply-config", then no l3cfg commit at all.
  - **[src] Why:** NM moves from ip-config-get to pre-up only on a
    post-commit notification of the tundev's IP configuration
    (`_l3cfg_notify_cb`). Identical content schedules no commit, and no
    timer is armed after the first activation. This is a regression since
    1.36; the code is the same on `main`. A report is drafted (§6).
  - **[spike] What works without touching the link:** an `Ip4Config` whose
    content differs from the `Ip4Config` NM received just before it. The
    plugin sends `Config`, one `Ip4Config` with a sentinel `nbns` (WINS)
    server, then the real one. On 1.56.1 that recovered every time: three
    drops in a row (R4b), without firewalld (R4g), with `dns=none` (R4e)
    and in two phases (R4i). NM was "activated" 24–30 ms after the
    `Config` in run 3, 28–40 ms in run 4, which logged more at debug
    level, 22–29 ms in run 5 and 19–29 ms in run 6, also with `nmss0`
    unmanaged (R8b, R8g, R8j).
    - Most of that is the pre-up dispatcher call, which ran no scripts.
      NetworkManager-dispatcher exits after 10 s idle, so after a gap D-Bus
      starts it cold: 17–24 ms in run 6. With firewalld, NM's zone call
      adds 1.7–4.4 ms.
    - **[smoke]** It also worked on 1.46.
  - **[spike]** Alternating the address between 192.0.0.8 and 192.0.0.9
    also works (R4c, R8c). Each reconnect then swaps the kernel address and
    local route. With a managed `nmss0` it also adds a DNS entry for the
    device (below) and rewrites NM's generated `nmss0` profile.
  - **[spike] [src] Why the sentinel works:** `_l3cfg_l3cd_set` compares a
    new `Ip4Config` only with the one before it, so the real config after
    the sentinel is stored as a new object. l3cfg commits a new object even
    when the merged content is unchanged (`l3cd-changed=0` in R4b and R4e),
    and that commit releases pre-up. Whether the sentinel itself is ever
    committed depends on NM's firewalld zone call:
    - **[src]** Each `Ip4Config` cancels NM's pending firewalld zone call
      and starts a new one. Only a call that completes applies a config
      (`nm-vpn-connection.c:1390-1406`, `:1497-1509`). The `Config` alone
      starts one too, because NM still holds the `Ip4Config` from before
      the drop.
    - With firewalld running (R4b, R4e, R8b; runs 3 to 6), a zone call
      took 2.6–5 ms (1.7 ms at the fastest, in R4i). NM's trace shows the
      real `Ip4Config` 0.2–0.9 ms after the sentinel, so NM committed only
      the real one, in 14 of 14 bursts over runs 4 to 6. The sentinel
      reaches neither the kernel nor D-Bus nor DNS. The margin is about
      1 ms (fastest zone call 1.7 ms against a gap of up to 0.9 ms).
    - Without firewalld, the zone call is a "fake success" on an idle
      callback (`g_idle_add`, `nm-firewalld-manager.c:263`). It runs only
      when no plugin signal is queued in NM **[src]**. The outcome is a
      race: the sentinel is committed only if its idle runs before NM
      takes the real `Ip4Config`. That happened in 1 of 5 bursts. Three
      orders were seen:
      - The sentinel cancelled the `Config`'s call, and its own call
        completed. NM committed the sentinel for about 1 ms, WINS
        192.0.0.10 included; from the source the pre-up dispatcher saw it
        (run 4's R4g).
      - The `Config`'s own call completed first, and NM applied the
        `Ip4Config` from before the drop, with no commit. The real
        `Ip4Config` then cancelled the sentinel's call (run 5's R4g and
        R8g, run 6's R8g).
      - The sentinel cancelled the `Config`'s call, and the real
        `Ip4Config` cancelled the sentinel's. Only the real one was
        applied (run 6's R4g).
      - The bus monitor's timestamps are receipt times, so the plugin's
        own spacing between the two was not measured.
    - Both outcomes are normal. Nothing may rely on WINS never showing,
      or on the sentinel not being committed: the 10 s escalation always
      commits it (R4i, R8j). Nothing may delay the real `Ip4Config` either:
      on a managed `nmss0` a committed sentinel files the entry of risk 11.
  - **[spike] [src] Caveat, an early "activated":** NM never clears its
    "waiting for pre-up" flag. Any l3cfg commit on `nmss0` during a gap
    therefore flips the VPN to "activated" while the tunnel is down.
    - Seen: `nmcli device reapply nmss0` released the stuck R4a. Run 4
      traced it: the reapply scheduled a reapply commit, and its
      post-commit moved the VPN to pre-up and then activated (R4a-i).
      Deleting the link flipped R4f 79–80 ms before the plugin's new
      `Config` in runs 3 and 4, 121 ms before it in run 5 and 87 ms
      before it in run 6. It also flipped R4p, and R8p with `nmss0`
      unmanaged (run 6), and moved R4j to pre-up before its STOPPED.
      **[src]** NM sets the device's managed type to "removed" before it
      unrealizes the device (`nm-manager.c:4378-4380`). That schedules a
      commit, also from an unmanaged device's default type, "external".
      The VPN's own commit type makes the commit real: NM tries to re-add
      the address to the dead link (`failure 19`), and the post-commit
      flips the VPN. **[spike]** R4p and R8p traced the same steps.
    - Not a trigger: adding or removing an address with `ip`, or a commit
      on the uplink (R4a, R4e, R8i, R8b). **[src]** Neither is a commit on
      `nmss0`'s l3cfg.
    - **[src]** Other triggers: anything NM itself commits on `nmss0`, such
      as a change of its device's managed type. `nmcli device set nmss0
      managed yes|no` changes the type and schedules a commit, even when
      the device stays unmanaged. ACD hardly matters, because NM gives up
      ACD on the NOARP `nmss0` (R4a trace).
    - With `nmss0` unmanaged (§4.1), `nmcli device reapply` is refused
      ("Device is not activated") and flips nothing (R8i). Link deletion
      still flips the VPN (R8p), and the managed setter remains a trigger
      **[src]**.
  - The first fix found, recreating the link, crashes NetworkManager 1.56
    (next item), and it loses split DNS even when NM survives (below).
- **[spike] [src] NetworkManager 1.56 crashes on a recreated `tundev`.** In
  the second run's R4b, the plugin deleted and recreated `nmss0` during a
  reconnect, then sent `Config` and `Ip4Config` at once. NM 1.56.1 died
  with SIGSEGV: the `Ip4Config` handler ran `_check_complete` before NM had
  a device for the new interface, and dereferenced NULL. systemd restarted
  NM, and the VPN was gone.
  - The bug is an incomplete fix (574411b8, RHEL-125796) for a regression
    from 1.56-rc1 (306f9c490b2a). It is in 1.56.0, 1.58.0 and `main`. A
    report with a patch is drafted (§6).
  - Rules for the plugin: keep `nmss0`, and so its ifindex, for the whole
    activation. Before any `Config`, wait until NM has a device for the
    link (`GetDeviceByIpIface`). Send one `Config` and one `Ip4Config` per
    (re)configuration, except for the sentinel above, which is sent only
    once the device exists. With these rules even the first activation is
    safe on affected versions.
- **[src] [spike] NM registers a VPN's DNS once, when it becomes
  "activated", under the tundev's ifindex.** `nm-policy.c` registers it
  only on the transition to activated ("FIXME(l3cfg): we need to track
  changes"). It removes it only when the VPN fails or disconnects from
  ip-config-get, pre-up or activated. NM's DNS manager files entries by
  ifindex.
  - An `Ip4Config` applied while activated still reaches the kernel and
    D-Bus, but not the VPN's DNS entry (R4i). It reaches resolved only
    through a side path: a managed `nmss0`'s own external device registers
    its merged IP configuration, VPN DNS included, as a second, non-VPN
    entry. **[src]** That happens on any commit whose merged content
    differs from the previous commit on that link (`l3cd-changed=1`) while
    the device is between ip-config and deactivating, not only on
    reconnects. The entry carries the VPN's priority, 50. **[spike]** In
    runs 4 to 6 it followed every such commit on a managed `nmss0`: the
    sentinel alone (R4i), the alternating address (R4c), and a sentinel
    committed by the `nbns` burst (run 4's R4g). It never followed an
    `l3cd-changed=0` commit (R4b's drops, R4e, R4g in runs 5 and 6) or a
    first activation. At the first activation NM assumed `nmss0` just
    after the VPN's first commit (21:25:18.849), so the first activation
    avoids the entry only by timing (inferred). After `nmcli device
    reapply nmss0` the entry has the device's priority, 100, instead
    (R4a-i). An unmanaged `nmss0` never enters that state range: below
    it, `nm-policy.c` hands the device's configuration to the DNS manager
    as removed, and there is no device entry to remove **[src]**.
    **[spike]** R8c's content-changing commit left NM's DNS unchanged, and
    so did both of R8j's (the sentinel alone, then the real config; run
    6): "DNS configuration did not change". §4.5 covers what the entry
    does.
  - So the sentinel carries the real config's `dns` and `domains`. In R4i
    NM registered the sentinel version, which was right only because the
    content matched. DNS servers and domains can change only with a new
    activation.
  - **[spike] Relinking loses split DNS.** In R4f the plugin deleted
    `nmss0`, waited for NM's new device and re-sent its config. NM had
    already flipped the VPN to "activated" on the old ifindex (above). The
    new link got its address but never its DNS, in runs 3 to 6 (R4f-d,
    R4f-q FAIL). Neither path above fired, because the new device was
    still unmanaged at the commit. In run 4 R4f-n passed only because
    `DnsManager.Configuration` is a stale snapshot (next item). Runs 5
    and 6 rebuilt it first, and R4f-n failed. Where the failed lookup
    went was not captured. Dispatcher scripts got a vpn-up on the uplink with no VPN
    interface or DNS. **[spike] [src]** The entry under the old ifindex is
    never removed, not even at `Disconnect`, because NM looks it up under
    the VPN's current ifindex. In runs 5 and 6 it was still there, without an
    `interface`, after `Disconnect`, until the driver restarted NM. NM
    logged NoSuchLink for it whenever a DNS update was forced.
  - **[spike] [src]** Switching off during a reconnect always removes the
    VPN's DNS (R4h), because NM publishes its deactivating state as
    "activated" first. **[spike]** A plugin failure from "connect" does
    not remove it (R4k, runs 4 to 6), unless the plugin first re-sends
    its `Config` (R4l; §4.4). A plugin killed in a gap does not remove it
    either, managed (R4m) or unmanaged (R8m). In runs 5 and 6 the rebuilt
    configuration still held R4k's entry, and resolved kept the server on
    `nmss0` until the link went (R4k-k FAIL).
- **[src] [spike] NM's `DnsManager.Configuration` is a snapshot, not live
  state.** NM rebuilds it only when it pushes DNS. It pushes only when the
  DNS content changes, and the ifindex is not part of that comparison. The
  `interface` field is the link's name at build time. In run 4, after R4f's
  relink and R4k's give-up, the property therefore still showed a VPN
  entry "on `nmss0`" for a deleted ifindex. In R4c-o it was byte-identical
  before and after resolved's default route moved: the property never
  shows that decision. `nmcli general reload dns-rc` forces a rebuild, and
  an entry for a dead link then has no `interface` **[src]**. **[spike]**
  Runs 5 and 6 showed this for R4k and R4f. NM logs `NoSuchLink` for such
  an entry only when something forces a DNS update, so its journal does
  not reveal a leak on its own.
- **[src] [smoke] [spike] A NetworkManager restart orphans the plugin.** NM
  does not tell the plugin, and the restarted NM does not reattach the VPN.
  The plugin must watch NM's bus name and tear down when it disappears. On
  Fedora the spike plugin did so within about 0.1 s of NM's exit (R7). NM
  sent no `Disconnect`. In runs 4 to 6 the plugin deleted `nmss0` first, then
  stopped the tunnel (sshuttle removed its table) and exited. The restarted
  NM listed only `lo`, the uplink and the lab's veth.
- **[src] [spike] Failure reasons are coarse.** NM maps the plugin's
  `Failure` codes to "login failed" and "IP configuration invalid". Every
  other code, including `CONNECT_FAILED`, becomes "unknown reason" (R1a,
  R6b), the same as sending no `Failure` at all **[src]**. Every give-up
  in run 5 sent `CONNECT_FAILED` and ended with reason 0, except R4p:
  there NM rejected the `Config` and failed the VPN itself first, with 5,
  "IP configuration invalid". R4p and R8p did the same in run 6. The
  plugin's own message reaches only the journal.
  - **[src]** GNOME Shell shows the same notification for every reason
    except "no secrets" and "user disconnected": "Connection failed",
    "Activation of network connection failed". There is no VPN-specific
    text.
  - **[spike] [src]** Every reconnect shows as "activating" with reason 6,
    "connect timeout" (`nm-vpn-connection.c:1800-1806`), although no
    timeout runs.
- **[spike] NM handles a plugin-created dummy `tundev` as hoped** (R3a,
  R3b). NM put 192.0.0.8/32 on `nmss0` and handed resolved its DNS server
  and domains with `DefaultRoute=no`. `git.corp.test` resolved through the
  tunnel, and other names stayed on the uplink. Details:
  - a domain without `~` also becomes a search domain; with `~` it is
    routing-only;
  - `DefaultRoute=no` follows from the domains, because a VPN entry with a
    domain never gets the automatic `~` **[src]**. The `Ip4Config` key
    `never-default` only stops NM from adding a default route. The DNS rule
    reads the profile's `ipv4.never-default`, which is `no` by default. A
    VPN entry with servers but no domains would therefore get
    `DefaultRoute=yes` and every lookup. So `split` requires a domain, and
    the CLI sets the profile's `never-default`;
  - A managed `nmss0` shows as "connected (externally)", with a volatile
    connection NM generates and an active connection of its own. That one
    ends with reason 3, "device disconnected", whenever the link is removed
    (R4h-r). An unmanaged `nmss0` (§4.1) has neither (R8a).

  Each check was a one-second snapshot of one name. Run 3 added three
  results. A single-label name (`git`) is completed only with the plain
  domain (R3a-s), not with `~corp.test` (R3b-s). NM's VPN code binds
  `nmss0` to the profile's firewalld zone, by default firewalld's default
  zone `public`. It unbinds it at deactivation, managed or unmanaged
  (R3a-z, R8b, R8l) **[src]**. It skips the unbinding when the link is
  already gone, because NM reads the name from a link that no longer
  exists **[src]**.
  **[spike]** After R4j, R4p and R8p firewalld still listed `nmss0` in
  `public` (run 6), until a later teardown with the link present, or a
  firewalld restart, removed it. The next `nmss0` starts in that zone.
  That is harmless while every profile uses the same zone. With `dns=none`
  the link carries no DNS server (R3c).
- **[src] Which connections get the GNOME toggle.** In gnome-shell
  (`js/ui/status/network.js`, `_shouldHandleConnection`) the VPN toggle
  lists connections of type `vpn` and `wireguard`, regardless of editor
  plugins. A `generic` connection type, including NM 1.46's
  `generic.device-handler`, would not get a toggle.
  - **[spike]** On Fedora 44 the toggle showed the spike profile, and
    switching it on and off went through the plugin (R5a to R5c). During a
    reconnect it stayed on and no notification appeared, with and without
    firewalld (R5e). **[src]** GNOME 50.5 shows a reconnect only through
    the "acquiring" icon. The tester was asked about "connecting" text,
    which GNOME does not show, and not about the icon.
- **[spike] [src] GNOME Settings stalls 25 s without an auth dialog.**
  Settings lists the profile under VPN with a switch and a gear button. The
  editor behind the gear first asks for the profile's `vpn` secrets, and
  GNOME Shell's agent hands every `vpn` secrets request to the plugin's
  auth dialog. With no `[GNOME] auth-dialog=` in the `.name` file,
  `_findAuthBinary` (gnome-shell `networkAgent.js`) throws outside its
  `try`, and the request is never answered.
  - **[spike]** With the profile inactive (R5f), GNOME Shell logged
    `Unhandled promise rejection` from `_vpnRequest`, with no exception
    text. Settings logged `Failed to get secrets: Timeout was reached`
    24.98 s later, which is libnm's 25 s D-Bus timeout, and then built the
    editor. In run 2, with the profile active, the tester saw a spinner.
  - **[spike]** With the stub (R5d) the editor opened at once, with Details,
    Identity ("unable to load VPN connection editor"), IPv4 and IPv6, as
    the source predicted. The stub does not answer the request; it makes
    it fail fast. GNOME Shell turns the stub's empty answer into "no
    secrets", NM finds no other agent, and Settings logs `Failed to get
    secrets: No agents were available for this request.` every time.
    GNOME Shell needed no restart after the `.name` file changed.
  - **[src]** The Details tab's "Make available to other users" clears
    `connection.permissions`, and the IPv4 tab can change the method.
    Settings can save both without an editor plugin, perhaps after an
    admin prompt. The plugin then rejects the profile (§4.2).
  - The missing *editor* plugin only explains the error label and
    sshuttle's absence from "Add VPN".
- **[src] NetworkManager-ssh's approach to keys and devices.**
  - Keys: its auth dialog passes `SSH_AUTH_SOCK` from the session to the
    root plugin as a "secret", and ssh then runs as root.
  - SELinux: it ships its own module (`selinux/NetworkManager-ssh.te`) so
    that root ssh can read `~/.ssh` and connect to the user's agent.
  - No-tunnel mode: it reports a pre-created `dummy0` (from
    `modprobe dummy`) with dummy addresses as `tundev`.
- **[reported] GNOME's SSH agent.** GNOME 46–49 use `gcr-ssh-agent`. It is a
  systemd user socket at `$XDG_RUNTIME_DIR/gcr/ssh` and exports
  `SSH_AUTH_SOCK` into the user manager's environment. It loads keys from
  `~/.ssh` on first use and asks for the passphrase with the GNOME system
  prompt. It is on by default on Ubuntu 25.10 and later.
  - **[spike]** On Fedora 44 (GNOME 50.5) it is on by default too: the user
    manager exports `SSH_AUTH_SOCK=/run/user/UID/gcr/ssh`.
  - **[spike]** A root process can run ssh as the user through `setpriv`
    with that socket, with SELinux enforcing (R0).
  - **[spike] It also unlocks keys for an ssh outside the session.** In R6,
    sshuttle inside a system unit started ssh through
    `setpriv --reset-env`, with `BatchMode=yes` and
    `SSH_AUTH_SOCK=/run/user/UID/gcr/ssh`. The key was
    passphrase-protected and had never been added to the agent. gcr asked
    for the passphrase in the session, and the tunnel came up 20 s later
    (9 s in run 3, typing included). `ssh-add -L` listed the key before it
    was loaded, so it cannot show what gcr has loaded.
  - **[src] Conditions,** from gcr and gnome-shell:
    - gcr loads keys on demand only for key pairs directly in `~/.ssh` with
      the `.pub` file next to them;
    - while the screen is locked, GNOME Shell cancels every gcr prompt at
      once, so signing fails at once instead of hanging;
    - a prompt on screen never times out, and a wrong passphrase brings it
      back, so only the caller can bound the wait;
    - keys already loaded stay loaded across lock and suspend.
  - **[spike] Behind the lock screen** (R6b; screen locked with
    `loginctl lock-session`, key not loaded), gcr's `ssh-add` failed, ssh
    logged "agent refused operation" and "Permission denied (publickey)",
    and the reconnect failed 1 s after it started. Those markers are only
    in the tunnel unit's journal; `systemctl start` reports just a failed
    control process. The test VM has auto-lock off, so locking at resume
    was not exercised.
- **[spike] Where plugins go.** NetworkManager creates
  `/usr/lib/NetworkManager/VPN` but not the deprecated `/etc/NetworkManager/VPN`,
  which does not exist on Fedora 44. If nmcli finds no plugin for a short
  `vpn-type`, it silently stores the name as the service type, and
  activation then fails with "was not installed".
  - **[src]** NM picks up a new `.name` file at once, but ignores edits to
    one it already loaded; the file must be removed and written again.
    GNOME Shell and libnm read the file afresh on every lookup.

### 2.2 sshuttle 2.0.0

- **[src] Firewall helper and cleanup.**
  - The client starts the firewall helper (`sshuttle --firewall`) through
    sudo or doas, or directly when it is already root.
  - The helper removes its rules when its stdin closes. On SIGTERM it
    relays SIGINT to the client and waits.
  - **[lab T5]** `systemctl stop`-style SIGTERM therefore cleans up.
  - **[spike]** Under systemd, not always. In 4 of 98 stops over runs 3 to
    5, systemd's SIGTERM to the unit's cgroup also killed the helper's own
    `nft delete table` child ("returned -15"). Run 6 had none in 44 stops,
    but a third case: in R8m's drop the helper was already deleting its
    table when the SIGTERM came. Relaying it to the exited client raised a
    `ProcessLookupError` inside the `nft delete table` call. Each time the
    table stayed until the unit's `ExecStopPost` sweep removed it (5 of 142
    stops).
  - **[spike]** In 14 of 98 stops (runs 3 to 5) and 2 of 44 (run 6) the
    helper logged a `ProcessLookupError` traceback after its rules were
    gone, because the client had already exited. This is harmless, but
    resolved's cache flush was skipped.
    sshuttle flushes resolved's cache on every start and stop.
  - **[lab T7]** SIGKILL, as at the end of a stop timeout, leaves the rules
    in place. They then refuse every connection to the tunnelled networks.
- **[src] `READY=1` means rules installed and server connected, not that the
  tunnel is alive.** The client sends it after the firewall rules are in
  place.
- **[src] Dead tunnels are noticed only through ssh.** sshuttle has no
  keepalive of its own; only ssh's `ServerAlive*` notices a dead path.
  - **[lab T10a/b]** Without keepalives, after the local address changes,
    sshuttle keeps running for 30 s and more. A unit would still be
    "active" while traffic through the tunnel is dead.
  - **[lab T10c]** With `ServerAliveInterval=5` and `ServerAliveCountMax=2`,
    it exits about 17 s after the change and cleans up its rules.
  - **[lab T10e]** When sshuttle is stopped while the path is dead, its
    `ssh` child keeps running. Only killing the whole cgroup, as a systemd
    unit does on stop, removes it reliably.
  - On the remote side the old `sshuttle` server sessions also linger.
    Server-side `ClientAliveInterval` would clean them up, but that setting
    is outside this project's control.
- **[src] Rule tables are named after the redirect port**
  (`inet sshuttle-ipv4-<port>` for nft). The port is the first free one
  counting down from 12300. The listener does not set `SO_REUSEADDR`, so
  after a restart:
  - **[lab T6]** the old port is still in TIME_WAIT and the new instance
    picks a different port, so the table names change between runs;
  - **[lab T8]** a fixed `--listen` port fails with `EADDRINUSE` for about
    60 s.

  Cleanup therefore cannot assume a port. A table is stale exactly when
  nothing listens on its port, and the lab's sweeper uses that test
  **[lab T7c]**.
- **[src] `--dns` already covers systemd-resolved, but only once.** Since
  1.0.5 it reads `/etc/resolv.conf` and `/run/systemd/resolve/resolv.conf`,
  only at start. After a network change the capture points at the old
  servers. This partly corrects the brief. Split DNS through resolved
  remains impossible without an interface: upstream #688 **[reported]**.
- **[src] 2.0.0's auto-exclusion runs as the wrong user here.** 2.0.0
  auto-excludes the remote host, or the first ProxyJump hop, by running
  `ssh -G` as the user running the sshuttle client.
  - **[lab T2]** With a root client, that reads root's ssh config and fails
    for the user's aliases.
  - nm-sshuttle must therefore work out the first hop as the user and pass
    `-x` itself.
- **[lab T9] A bad remote nameserver kills the tunnel.** If the remote
  host's resolver list contains an unreachable server, one forwarded DNS
  query kills the whole tunnel. In `server.py` `try_send()`, `connect()` sits
  outside the `try`. `--to-ns` avoids it. This is worth an upstream bug
  report.
- **[src] `--method auto` prefers `nat` (iptables) over `nft`.**
- **[reported] Related upstream issues:**
  - #285: detect a dead tunnel.
  - #532, #632, #901: forwarding silently stops while the process lives.
  - #831: a second instance overwrites the first one's rules.
  - #629: rules survive an unclean reboot.

### 2.3 Kernel and systemd-resolved

- **[src] resolved's rule for using a link's DNS servers**
  (`resolved-link.c`, `link_relevant`): the link must be up with carrier
  and have an address better than link-local. A dummy link for DNS
  therefore needs a global-scope address.
- **[lab T3b] Pinned queries are still captured.** A query sent with
  `IP_UNICAST_IF` pinned to the tunnel link, as resolved does for per-link
  servers, is still caught by sshuttle's nft output redirect. It is answered
  by the internal server through the jump host.
- **[lab T4] An nft guard table makes traffic fail closed.**
  - The guard accepts packets that conntrack marks as DNAT'ed (sshuttle's
    redirect) and rejects the rest for the tunnelled networks.
  - Tunnel down: TCP is refused within about 10 ms, and DNS to the internal
    server fails locally with `EPERM`.
  - Tunnel up: everything passes.
  - The excluded jump host stays reachable.

### 2.4 Prior art

- **[reported] sshuttle-tray (KDE).**
  - It notes that systemd can report `active` while traffic bypasses a dead
    tunnel, and checks real reachability with a public-IP comparison.
  - Its unit uses `Restart=no` on purpose, because systemd restart-looped
    sshuttle while the machine was offline.
- **[reported] sshuttle-ui (Tauri).** A network watcher triggers an
  immediate reconnect with backoff. Its kill switch is only an overlay in
  the UI.
- **[reported] Python VPN plugins** exist (openvpn3, openconnect-sso,
  GlobalProtect and others). Most implement the D-Bus interface directly.
  One warns that libnm's `NMVpnServicePlugin` GIR annotations break
  subclassing from Python.
- **[reported]** No project integrating sshuttle with NetworkManager was
  found. "nm-sshuttle" was free on GitHub and PyPI when checked.

### 2.5 Target distributions

| Distribution | GNOME | NetworkManager | systemd | sshuttle package |
|---|---|---|---|---|
| Fedora 43 | 49 | 1.54 | 258 | 1.3.1 |
| Fedora 44 | 50 | 1.56.1 | 259 | 1.3.2 |
| Ubuntu 26.04 LTS | 50.1 | 1.54.3 | 259.5 | 1.3.2 |
| Debian 13 | 48 | 1.52.1 | 257 | 1.3.1 |

Ubuntu was verified on packages.ubuntu.com. The other rows come from search
results **[reported]**. No distribution packages 2.0.0 yet, except possibly
Arch, so the design must work with 1.3.x, which has the same firewall and
DNS behaviour.

## 3. Options considered

| | A. Full VPN plugin | **B. Thin VPN plugin (proposed)** | C. Unit + Shell extension | D. `ssh -D` + tun2socks |
|---|---|---|---|---|
| GNOME toggle | yes | yes | extension only | yes (needs plugin) |
| Edit in GNOME Settings | yes | no (CLI) in v1 | no | depends |
| NM events (sleep, roam, connectivity) | yes | yes | must watch NM itself | yes |
| Code | Python + C/GTK4 editor + auth dialog | about 1,200 lines Python | small, plus JS per GNOME release | Go binary + glue |
| Keeps sshuttle's DNS and server-side Python | yes | yes | yes | no: OpenSSH SOCKS has no UDP |

- **A** buys native editing in GNOME Settings at the cost of a C/GTK4
  editor library (gnome-control-center `dlopen`s it) and libnma. It can be
  added on top of B later without changing anything else.
- **C** is the smallest: a systemd unit plus a Quick Settings extension
  such as *Custom Command Toggle* (GNOME 45–51). But the toggle sits outside
  NM's VPN list, extensions break on GNOME upgrades, and the lifecycle work
  is the same as in B.
- **D** gives NM a real interface. But OpenSSH's `-D` cannot carry UDP, so
  DNS needs separate handling. tun2socks' built-in ssh client has no agent
  support and ignores host keys. It also drops what sshuttle already does
  well.
- **Rejected outright:** auto-starting through `connection.secondaries`
  (fails the base connection, §2.1) and a `generic` device-handler profile
  (no toggle).

## 4. Proposed architecture

```
 GNOME Quick Settings toggle / nmcli
              │ activate, deactivate
              ▼
      NetworkManager ──spawns──► nm-sshuttle-activate   (shim: one D-Bus call, exits)
              │                              │ StartServiceByName
              │ org.freedesktop.NetworkManager.VPN.Plugin
              ▼                              ▼
  ┌─ nm-sshuttle.service ─────────── root, Type=dbus ───────────────────────┐
  │  plugin + supervisor                                                    │
  │   • validates the profile, resolves the first hop as the user          │
  │   • nft table inet nm-sshuttle-guard  (fail closed while toggle is on) │
  │   • link nmss0, dummy, 192.0.0.8/32   (DNS; kept while activated)      │
  │   • watches NM (bus name, connectivity, primary connection), logind,  │
  │     the tunnel unit, and health probes                                 │
  └───────────────┬─────────────────────────────────────────────────────────┘
                  │ StartUnit / StopUnit
  ┌─ nm-sshuttle-tunnel.service ──── root, Type=notify, Restart=no ─────────┐
  │  ExecStartPre / ExecStopPost: nm-sshuttle sweep                         │
  │  ExecStart: sshuttle --method nft -r REMOTE -x FIRST_HOP                │
  │                 --ns-hosts DNS --to-ns DNS SUBNETS                      │
  │                 -e "nm-sshuttle ssh-as-user USER"                       │
  │    ├─ sshuttle --firewall        (root; nft table sshuttle-ipv4-PORT)  │
  │    └─ ssh, as USER via setpriv   (~/.ssh/config, ProxyJump, agent)     │
  └─────────────────────────────────────────────────────────────────────────┘
```

### 4.1 NetworkManager integration

Files installed:

```ini
# /usr/lib/NetworkManager/VPN/nm-sshuttle-service.name
[VPN Connection]
name=sshuttle
service=org.freedesktop.NetworkManager.sshuttle
program=/usr/libexec/nm-sshuttle/nm-sshuttle-activate
supports-multiple-connections=false
supports-safe-private-file-access=true

[GNOME]
auth-dialog=/usr/libexec/nm-sshuttle/nm-sshuttle-auth-dialog
```

The other installed files:

- a D-Bus policy in `/usr/share/dbus-1/system.d/`: only root may own the
  name or send to it;
- a D-Bus activation file with `SystemdService=nm-sshuttle.service`;
- the two systemd units (§4.7);
- the auth-dialog stub;
- a NetworkManager `conf.d` snippet that keeps `nmss0` unmanaged (below).

**[src]** NetworkManager 1.58 and later refuse to activate a profile with
`connection.permissions` (which every nm-sshuttle profile has, §4.2) unless
the `.name` file sets `supports-safe-private-file-access=true`. The key
promises that the plugin reads no files on the user's behalf, or checks
the user's permissions when it does. nm-sshuttle reads no files from the
profile; ssh reads `~/.ssh` as the user. 1.56 ignores the key.

**Why a shim and D-Bus activation.** NM spawns `program=` inside its own
cgroup and SELinux domain and only waits for the bus name. The shim makes
one `StartServiceByName` call and exits. The real plugin then runs as an
ordinary systemd unit, with its own cgroup, journal, sandboxing and
`systemctl status`. It no longer lives inside NetworkManager's process
tree.

**[spike]** On Fedora 44 the shim is the only setup that ran the whole
path with the stock SELinux policy (§2.1): link, split DNS, every
reconnect mode, GNOME and an NM restart (R1b to R7). Spawned directly, the
plugin runs as `NetworkManager_t`, and systemd refuses even to query a
tunnel unit with an ordinary name (R1a).

**Fallbacks:**

- **Direct mode, tested only on a minimal path.** `program=` is the plugin
  itself, and the tunnel unit is named
  `NetworkManager-sshuttle-tunnel.service`, so that it gets the
  `NetworkManager_unit_file_t` label. **[spike]** Start, stop and status
  of the unit worked with `dns=none` and no `nmss0` (R1e).
  - **[src]** Denied by policy: `journalctl`, so the failure detail would
    have to come from unit properties or a reason file; transient units;
    and `ss`'s sock_diag socket (ss falls back to `/proc/net/tcp` and
    logs an AVC each time).
  - **[src]** Allowed by policy, untested: `ip` and `nft`, which run as
    `ifconfig_t` and `iptables_t`. Neither can read the plugin's files in
    `/run`, so a ruleset goes on stdin.
  - Untested: reconnects, giving up, logind and systemd watches, and an NM
    restart. **[src]** For the restart, NM spawns the plugin in its own
    cgroup under `KillMode=process` and never stops it. Also untested:
    denials hidden by `dontaudit`. The label of the state directory in
    `/run` depends on which process creates it first.
  - The plugin would run inside NetworkManager's cgroup and sandbox. It is
    not worth more work unless the shim breaks.
- A small SELinux policy module, as NetworkManager-ssh ships. Untested.

**The plugin speaks the VPN D-Bus interface directly** through Gio, rather
than subclassing libnm. The interface is small: `Connect`, `NeedSecrets`,
`Disconnect`, `StateChanged`, `Config`, `Ip4Config`, `Failure`, and a few
others.

**Behaviour:**

- `NeedSecrets` returns `""`. Keys live in the user's agent, so there are
  no NM secrets.
- **The auth dialog is a stub.** GNOME asks it whenever anything requests
  the profile's `vpn` secrets, for example the Settings editor. Without
  one, Settings waits 25 s before it shows the editor (§2.1). The stub
  speaks the old auth-dialog protocol: it reads stdin up to `DONE`, prints
  two empty lines, waits for `QUIT` and exits 0. That is ten lines of
  shell. **[spike]** With it the editor opens at once (R5d). The request
  then fails fast instead of being answered, and Settings logs "Failed to
  get secrets: No agents were available for this request." That warning
  is expected.
- **`nmss0` exists for the whole activation**, also for `dns=none`. The
  plugin creates it before starting the tunnel and removes it only after
  `Disconnect` or failure (§4.4). It never recreates the link while NM
  holds the VPN. That can crash NM 1.56, and even without a crash the
  VPN's DNS stays on the old ifindex (§2.1, R4f). Without a link of its
  own, a reconnecting VPN would also be flipped to "activated" by commits
  on the uplink (§2.1).
- **`Config` waits for NM's device.** Before the first `Config`, the plugin
  asks `GetDeviceByIpIface("nmss0")` at once, then every 100 ms, for up to
  10 s, until NM has a device for the link. The spike waited 100 ms before
  its first poll, which added about 0.1 s to every reconnect (run 6). Each
  new `nmss0` gets a new device path (R8), so the plugin never keeps the
  path across activations. **[src]** Right after a link is replaced, this
  call can still return the old link's device, and NM's Device interface
  has no ifindex to tell them apart. That is one more reason never
  to replace `nmss0` during an activation; at `Connect` a leftover link is
  replaced before NM has seen the profile, and the stale device path, if
  any, is not accepted.
- `Config` carries:
  - `gateway`: the first hop's address, resolved as the user;
  - `tundev`: `nmss0`;
  - `can-persist`: true;
  - `has-ip4`: true, so that NM addresses the link.
- `Ip4Config` carries `address` 192.0.0.8/32 and `never-default`, plus
  `dns` and `domains` for `split` and `all`, and **no routes**. sshuttle
  intercepts traffic, so the routing table stays as it is. Fail-closed comes
  from the guard table, not from routes.
- **The plugin watches NM's bus name** and tears everything down when NM
  goes away (§2.1).
- **The plugin follows only its own active connection.** For a managed
  `nmss0` NM creates a second one, and that one's state changes are noise
  (reason 3 at every teardown, R4h-r). An unmanaged `nmss0` has none (R8),
  but a user override brings it back. So the plugin still finds its own by
  the profile's UUID and filters `StateChanged` by that path.
- **`nmss0` is unmanaged.** The package installs
  `/usr/lib/NetworkManager/conf.d/90-nm-sshuttle.conf`:

  ```ini
  [keyfile]
  unmanaged-devices+=interface-name:nmss0
  ```

  **[spike]** R8 ran with this content (as `90-nm-sshuttle-spike.conf`,
  applied by restarting NM). In runs 5 and 6 every check passed (R8a to
  R8l). Run 6 added the two-phase escalation (R8j), link loss in a gap
  (R8p) and a kill in a gap (R8m). Each behaved as with a managed `nmss0`,
  except that R8j filed no non-VPN entry (R4i did); R8m leaked the DNS
  like R4m.
  - Still there: NM addresses `nmss0`, registers the VPN's DNS, binds the
    firewalld zone, and has a device for the link (`GetDeviceByIpIface`
    after 0.10 s). The VPN finds its tundev by ifindex, whatever its
    managed state, so the rule that `Config` waits for NM's device
    (above) still guards against the 1.56 crash **[src]**.
  - Gone: the generated `nmss0` profile and its second active connection,
    also after a kill (NM does not assume the leftover link, R8m);
    the non-VPN DNS entry behind risk 11 (R8c-u, R8c-o, R8j-u; §4.5); `nmcli
    device reapply` as an early-flip trigger (refused, R8i), and
    `disconnect` **[src]**; and device-level dispatcher events for
    `nmss0`. VPN events (vpn-pre-up, vpn-up) stay.
  - Unchanged: the flip when the link is deleted (R8p), and a flip from
    `nmcli device set nmss0 managed …` (the device stays unmanaged, because
    the keyfile setting cannot be overruled through the API), **[src]**
    only. Also the give-up order (R8l, R8p) and the DNS leak after a kill
    (R8m).
  - **Installing:** the scriptlet runs `nmcli general reload conf` only
    when no nm-sshuttle VPN is active. Otherwise the file takes effect at
    NM's next start. Untested: the reload itself (R8 restarted NM), and
    the setting arriving while a VPN is active, for example when NM
    restarts after an update without the scriptlet's reload **[src]**. No udev rule and no `[device]`
    `managed=0`: both rank below the keyfile setting.
  - **Overrides:** a file of the same name in `/etc/NetworkManager/conf.d`
    or `/run` masks ours. A plain `unmanaged-devices=` in
    `NetworkManager.conf` or `/etc` replaces the list **[src]**; the
    spike's own file did that until run 5, and the smoke container's file
    still does. `nmss0` is then managed, and risk 11 returns. A managed
    `nmss0` is also "unmanaged" (state 10) until NM assumes it after the
    first commit (run 5), so the state before the first `Config` proves
    nothing. The plugin checks after the first "activated": state 10 means
    unmanaged, and any other state means managed (R8a). In R3a the
    plugin saw "activated" while the managed `nmss0` was still in
    ip-check; it reached 100 ("connected (externally)") just after. On NM ≥ 1.48, `StateReason` 76 ("unmanaged by settings")
    should say so earlier **[src]**, unobserved. If `nmss0` is managed,
    the plugin logs "nmss0 is managed by NetworkManager; DNS may take the
    default route (risk 11)" and carries on.
- **A `Connect` can reach a plugin that has just stopped.** NM reuses a
  running plugin whose bus name is still owned. In R5c that happened 5 s
  after the `Disconnect`. A `Connect` after STOPPED is a fresh activation:
  the plugin resets the reconnect state and cancels pending timers. If the
  service exits when idle, every VPN.Plugin call cancels every exit timer,
  a startup timer included **[src]**.
- **Every entry point is guarded.** An exception in a D-Bus handler or a
  GLib callback becomes a `Failure` and STOPPED, never a silent hang that
  NM would wait out for 60 s, as spike R1a did in the second run.
  **[spike]** In run 3 R1a's failure reached NM in 0.14 s. An error from
  systemd, such as "Access denied", is a failure, never "inactive": the
  spike once read a denied `is-active` as a stopped tunnel (R1a).

192.0.0.8/32 is the RFC 7600 "IPv4 dummy address". It sits outside the
464XLAT range (192.0.0.0/29), and the lab used it. NM sets the address
itself; otherwise it might remove an address it did not configure.

The CLI sets `ipv4.auto-route-ext-gw=no`. sshuttle captures traffic with
nft rules, not routes, and `nmss0` carries no routes, so the first hop goes
over the uplink anyway (spike R1b-r). NM's extra host route to the gateway
would only get in the way: after a roam on the same interface it still
points at the old network's router **[src]**.

The CLI also sets `ipv4.never-default=yes` and `ipv6.never-default=yes`, so
that the VPN's DNS entry can never take resolved's default route by itself
**[src]**. `all` sets `~.` explicitly.

### 4.2 Profiles

A profile is an NM connection of type `vpn` with
`vpn.service-type=org.freedesktop.NetworkManager.sshuttle`. Its `vpn.data`
keys:

| Key | Example | Notes |
|---|---|---|
| `remote` | `corp` | An ssh destination as the user would type it; usually an alias from `~/.ssh/config`. |
| `local-user` | `dennis` | Whose ssh identity and agent to use. Required. |
| `subnets` | `10.0.0.0/8,172.20.0.0/16` | CIDRs to tunnel. |
| `exclude` | `10.0.5.0/24` | Optional. |
| `dns` | `split` | `none`, `split` or `all`. |
| `dns-servers` | `10.1.0.53` | Required for `split` and `all`. |
| `dns-domains` | `corp.example,lab.example` | Routing domains for `split`. |
| `method` | `nft` | `nft` (default) or `nat`. `tproxy` comes later. |
| `gateway` | `203.0.113.7` | Override the first hop. Needed with `ProxyCommand`. |
| `probe` | `10.1.0.10:22` | Optional reachability check through the tunnel. |
| `fail-closed` | `yes` | Install the guard table (default `yes`). |

The CLI also sets `connection.permissions=user:<local-user>`,
`vpn.persistent=yes` and `connection.autoconnect=no`. Example:

```console
$ nm-sshuttle add corp --remote corp --subnets 10.0.0.0/8 \
      --dns split --dns-servers 10.1.0.53 --dns-domains corp.example
$ nm-sshuttle check corp     # as the user: ssh -G, host key, test login
```

**The profile is untrusted input to a root process.** The plugin enforces:

- `remote` must match `^[A-Za-z0-9._@:%\[\]-]+$` and must not start with
  `-`;
- every address and network must parse with `ipaddress`;
- `local-user` must exist and be a regular user;
- `connection.permissions` must be exactly `user:<local-user>`;
- `vpn.persistent` must be `yes`, so that NM keeps the VPN across drops
  (§2.1), and `ipv4.method` must be `auto`, because otherwise NM ignores the
  plugin's `Ip4Config` **[src]**.

The last rule stops another local user from activating a profile that
would use someone else's agent, and NM also hides the profile from other
users. There are **no free-form sshuttle or ssh
options**: ssh options belong in the user's `~/.ssh/config`, which only
ever reaches the unprivileged ssh. NetworkManager-ssh removed arbitrary ssh
options for the same reason.

**[src]** GNOME Settings can break the permissions and method rules without
an editor plugin. The Details tab's "Make available to other users" clears
`connection.permissions`, possibly after an admin prompt, and the IPv4 tab
changes the method (R5d-x shows both controls). The plugin's failure
message names the field, and `nm-sshuttle check` flags it.

### 4.3 Privilege split and key unlock

| Runs as root | Runs as the user |
|---|---|
| plugin and supervisor, sshuttle client, sshuttle firewall helper | `ssh` only |

The bridge, `nm-sshuttle ssh-as-user USER`, is sshuttle's `-e` command:

1. It reads the user's systemd manager environment
   (`systemctl --user -M USER@ show-environment`) and keeps
   `SSH_AUTH_SOCK`, `WAYLAND_DISPLAY`, `DISPLAY` and `XAUTHORITY`. If
   `SSH_AUTH_SOCK` is missing there, it falls back to the well-known
   sockets under `/run/user/UID` (`gcr/ssh`, `keyring/ssh`).
2. It runs ssh as the user:
   `setpriv --reuid --regid --init-groups --reset-env … ssh -o BatchMode=yes
   -o ControlMaster=no -o ControlPath=none -o ConnectTimeout=15
   -o ServerAliveInterval=10 -o ServerAliveCountMax=3 REMOTE -- <sshuttle
   bootstrap>`.

**[lab T1]** In the lab, a root sshuttle 2.0.0 tunnelled traffic with ssh
running as the user, a user-only alias, and a passphrase-protected key held
in the user's agent.

The `-o` options are given on the command line, so they override the
user's config for this connection only:

- `ControlMaster=no` and `ControlPath=none` stop a shared master connection
  from hiding the tunnel's liveness.
- `BatchMode=yes` means ssh itself never prompts.

**How a key gets unlocked.** Unlocking happens in the agent: gcr-ssh-agent
prompts in the session when a key is first used. With plain `ssh-agent`,
the user runs `ssh-add` once, or the system relies on `AddKeysToAgent` from
an earlier login. The first login to a new host goes through
`nm-sshuttle check`, so the host key is accepted interactively once.

**Authentication failures stop the tunnel.** An `ssh` exit with "Permission
denied" or "Host key verification failed" ends it. The plugin reports
`LOGIN_FAILED`, GNOME shows its generic "Connection failed" notification
(§2.1), and the toggle goes off. There is no retry loop that could keep
raising prompts. The plugin reads these markers, and "agent refused
operation", from the tunnel unit's journal, on the first connect and on
reconnects. `systemctl` itself reports only a failed process (R6b).
During a *reconnect*, for example right after resume behind the lock
screen, the plugin waits until logind's `LockedHint` clears and then
retries once. **[spike]** R6b showed the fast failure behind the lock
screen. The wait and the retry are untested.

**Alternatives that were rejected:**

- *Upstream's model* runs the sshuttle client as the user and starts
  `sudo sshuttle --firewall`. It needs a NOPASSWD sudoers rule for a helper
  that accepts arbitrary netfilter rules on stdin, and the client would have
  to live in the user session.
- *NetworkManager-ssh's model* runs ssh as root with the user's
  `SSH_AUTH_SOCK`. Root then reads `~/.ssh`, `known_hosts` handling gets
  awkward, and SELinux needs a custom module.

**The cost of this design.** The sshuttle *client* runs as root, while
upstream runs only the firewall helper as root. That is a larger root
surface; §4.8 covers the mitigation.

### 4.4 Lifecycle

| Supervisor state | NM / GNOME show | sshuttle unit | Guard | `nmss0` |
|---|---|---|---|---|
| connecting (first time) | activating | starting | on | up |
| up | activated | running | on | up |
| reconnecting | activating (toggle on) | stopped or backing off | **on** | up |
| failed | deactivated + notification | stopped | removed | removed |
| off | deactivated | stopped | removed | removed |

**How a reconnect shows** was decided by comparing five approaches
against NetworkManager's source: the `nbns` sentinel, an invisible
reconnect, an alternating address, failing and re-activating, and a hybrid.
The sentinel is the only one that shows an outage in GNOME's own terms (the
"acquiring" icon, no notification) and keeps DNS failing closed.
**[spike]** Run 3 confirmed it on Fedora's 1.56.1 in every variant tried
(§2.1). The invisible reconnect is the simplest and the most robust across
versions, so it stays as the fallback.

Events and what the supervisor does:

- **Toggle on (`Connect`):**
  1. Resolve the first hop as the user (`ssh -G` through the bridge).
  2. Sweep stale tables.
  3. Create `nmss0` and install the guard.
  4. Start the tunnel unit and wait for `READY=1` (timeout 30 s).
  5. Wait until NM has a device for `nmss0` (§4.1).
  6. Send one `Config` and one `Ip4Config`, then `STARTED`.

  A failure during this first connect is reported straight away, without
  retries; the user has to act anyway.
- **The tunnel drops while up** (the unit stops, two probes fail, the
  network changes, connectivity goes NONE, or the machine suspends):
  - authentication or host-key error while the screen is unlocked →
    **failed**;
  - anything else → **reconnecting**. The plugin stops the tunnel unit,
    keeps `nmss0` and the guard, and sends `StateChanged STARTING` once.
    NM shows "connecting" with reason 6, "connect timeout", which the
    plugin ignores. GNOME keeps the toggle on with the "acquiring" icon
    and shows no notification (R5e). **[src] [spike]** NM keeps the link's
    address and DNS in this state and arms no timer, so the plugin's limit
    is the only one. resolved kept `nmss0`'s DNS server through every gap
    in run 3.
  - STARTING is sent only while NM shows the connection as activated, and
    never once NM has started deactivating: it would undo the user's "off"
    **[src]**. While a reconfiguration is still in flight, the plugin
    first waits for "activated" (at most 2 s).
  - Attempts: at once after a network change or resume, otherwise after 1,
    2, 4 … 60 s. None while offline, asleep or waiting for an unlock; at
    most one a minute behind a captive portal.
  - The plugin gives up (reports failure) after `reconnect-timeout`,
    default 10 min of attempt time. Offline, asleep and locked time do not
    count.
- **Coming back:** NM shows "connected" again only if the re-sent IP
  config differs from the one NM received just before it (§2.1). The
  plugin sends, back to back from one callback, `Config`, one `Ip4Config`
  with a sentinel `nbns` server (192.0.0.10), the real `Ip4Config`, then
  STARTED. The sentinel is the real config plus `nbns`, with the same
  address, `dns` and `domains`. Before sending, it checks that `nmss0` has
  the same ifindex, that NM has a device for it, that NM is the same
  instance, and that the connection is still activating. **[spike]** On
  1.56.1 NM was "activated" 19–40 ms after the `Config` (runs 3 to 6).
  **[smoke]** It also worked on 1.46; **[src]** the code involved is the
  same from 1.52.1 to `main`.
  - STARTED must follow every (re)configuration; otherwise NM ignores the
    next STARTING **[src]**. Its place in the burst does not matter.
  - Not "activated" 10 s later: the plugin sends the sentinel config alone,
    waits for "activated", then the real one. **[spike]** That worked too
    (R4i, R8j): "activated" 21–30 ms after `Config`. NM then keeps the
    sentinel version as the VPN's DNS (§2.1), which is why the two may
    differ only in `nbns`. This also works if NM starts comparing configs
    by content. With a managed `nmss0`, this step always files the non-VPN
    entry of risk 11 (R4i, runs 4 to 6). With `nmss0` unmanaged NM
    committed both configs and filed nothing (R8j, run 6). The spike sends
    the sentinel alone straight after `Config` (`nbns2`), so it tests this
    step but not the 10 s timer before it; the timer is for M2's unit
    tests.
  - Still not "activated" 60 s after the first try: the plugin reports
    failure and records the NM version in `/run`. Later activations on that
    version use the invisible reconnect.
- **"Activated" is not proof.** NM never clears its "waiting for pre-up"
  flag, so any l3cfg commit on `nmss0` during a gap flips the VPN to
  "activated" while the tunnel is down. **[spike]** Traced triggers:
  deleting the link, managed or unmanaged (R4f, R4p, R8p; R4j reached
  pre-up), and, with a managed `nmss0`, `nmcli device reapply nmss0`
  (R4a-i). With `nmss0` unmanaged the reapply is refused (R8i). Adding or
  removing an address with `ip`, and uplink commits, did not flip the VPN
  (R4a, R4e, R8i, R8b). **[src]** `nmcli device set nmss0 managed …` is a
  further trigger in both forms. The plugin's own health check decides. If
  NM shows "activated" while the plugin is reconnecting and `nmss0` still
  has its ifindex, the plugin sends STARTED then STARTING to put it back, at
  most 3 times per gap. If the link is gone, it does not bounce; it gives up
  at once (branch 4). With the same ifindex, NM's DNS stays right through
  such a flip **[src]**, but with a managed `nmss0` a reapply also files a
  non-VPN `nmss0` DNS entry (§4.5). A flip also dispatches vpn-pre-up and
  vpn-up, on the uplink when the link is gone (R4p, R8p), and nothing undoes
  them. In R8p that vpn-up ran four real dispatcher scripts on the uplink,
  and no vpn-down followed. The flip lasts until the plugin's bounce: 69 ms
  (R4p) and 88 ms (R8p) in run 6.
- **Giving up during a reconnect, or any failure while NM shows
  "connecting":** NM removes the VPN's DNS only when the VPN fails from
  ip-config-get, pre-up or activated, and a reconnecting VPN sits in
  "connect" **[src]**. A first connect has registered no DNS yet.
  **[spike]** Runs 4 to 6 compared the three orders on 1.56.1, once each
  per run, with trace logging:
  - `Config` first (R4l, spike mode `giveup-config`): NM moved to
    ip-config-get and failed the VPN from there 0.6–1.1 ms later. It
    removed the DNS entry while `nmss0` still existed. resolved got an
    explicit reset. NM logged only the expected `connect-failed` warning.
    R4n (SIGTERM in a gap) and R8l (`nmss0` unmanaged) went the same way.
  - STOPPED first (R4k): NM failed the VPN from "connect". Its entry stayed
    in NM until the driver restarted NM (R4k-d FAIL). Run 5's rebuilt
    configuration still held it, without an `interface`. resolved kept the
    server on `nmss0` while the link existed (R4k-k FAIL) and dropped it
    only when the plugin deleted the link.
  - The link first (R4j, runs 1 to 6): NM's commit on the dead link moved
    the VPN to pre-up 36–41 ms before NM handled STOPPED. That removed the
    entry, but NM also dispatched vpn-pre-up, with no vpn-up or vpn-down
    after it, and tried to re-add the address. resolved answered
    NoSuchLink, and NM never asked firewalld to drop `nmss0`.

  The plugin picks the branch from its cached state, with no D-Bus call
  before the signals:
  1. Its connection is already deactivating or gone: send only the final
     STOPPED, if not yet sent. NM removes the DNS itself (R4h).
  2. It is "activated" now with `nmss0` intact, or never was in this
     activation: `Failure`, then STOPPED. **[spike]** Only the first
     connect ran (R1a). From "activated" NM removes the DNS **[src]**,
     untested inside a gap.
  3. It is reconnecting, and `nmss0` has its ifindex and NM a device for
     it: the byte-identical `Config`, then `Failure`, then STOPPED, back to
     back from one callback. **[src]** NM handles all plugin signals in
     order in one handler, ahead of its own idle work, so STOPPED cannot
     overtake `Config`. A wait between them only widens the window.
     `Failure` goes before STOPPED. With `CONNECT_FAILED` it changes
     nothing clients see: NM maps that code to "unknown", its default
     (R4l, R4n, R8l: reason 0) **[src]**. Use `LOGIN_FAILED` where it
     fits.
  4. `nmss0` is gone, whatever NM shows unless it is deactivating: give up
     at once, without bouncing first. The same `Config`, `Failure` and
     STOPPED make NM fail the VPN itself ("IP configuration invalid",
     reason 5) and remove the DNS. **[spike]** R4p did this from
     "connect" after a bounce, and R8p with `nmss0` unmanaged (run 6). NM
     fails the VPN on the `Config` itself. In R8p NM disposed of the VPN
     1.3 ms after the `Config` and logged neither the plugin's `Failure`
     nor its STOPPED (inferred: they arrived too late). The burst of
     NoSuchLink warnings that follows is NM resetting the vanished link in
     resolved, not a leak. In the 10 s R4p waited for its timer,
     resolved had no `nmss0` link, so split names went to the uplink's
     resolver (inferred). From pre-up or "activated" this is **[src]**
     only; from "activated" NM also dispatches vpn-down. NM leaves
     firewalld's binding for `nmss0` in place, because the link has no
     name any more (R4p, R8p; §2.1). If a link named `nmss0` with another
     ifindex exists, never send `Config` (§2.1). Send `Failure` and
     STOPPED instead, and log the stale entry that follows.
  5. Only then stop the tunnel. NM's `Disconnect` follows 1.0–1.3 ms
     later (R4l, R4n, R8l). Answer it without a second STOPPED. Stay on
     the bus until it has arrived: if the plugin has already exited, that
     call starts it again (R4m).
  6. Remove the guard and `nmss0` 100 ms after the connection is
     deactivated, and at most 2 s after STOPPED. **[spike]** After
     STOPPED, NM removed the address in 2.5–3.4 ms, resolved's DNS in
     6–9 ms and the firewalld zone in 14–25 ms (R4l in runs 4 and 5, R4n,
     R8l). The spike removed the link 145 ms (run 4) or 220 ms (R4n) after
     STOPPED, and no warnings followed.

  No order changes two things. Clients see a brief ip-config-get (R4l),
  which the plugin's own watcher must not count as progress. Dispatcher
  scripts get no vpn-down after a give-up during a reconnect, because NM
  sends it only from "activated" (R4j to R4p, R8l, R8p;
  `nm-vpn-connection.c:1099`).
- **The plugin stops during a gap** (SIGTERM, or a stop or restart of
  `nm-sshuttle.service`): it cancels every timer and gives up as above. It
  sends its signals before any blocking call, waits for NM's `Disconnect`
  (at most 1 s), then tears down. **[spike]** In R4n, NM failed the VPN
  from ip-config-get and removed its DNS. The link was gone 220 ms after
  STOPPED, and no warnings followed.
- **The plugin dies during a gap** (SIGKILL, a crash, the OOM killer, a
  stop timeout): **[spike]** NM sees the bus name vanish and sets the VPN
  "disconnected" from "connect" (R4m, and R8m with `nmss0` unmanaged;
  `nm-vpn-connection.c:2766-2776`). It removes the address and the
  firewalld zone, but it keeps the DNS entry until it restarts (R4m-d,
  R8m-d FAIL). Its `Disconnect` call D-Bus-activates a new plugin about
  70 ms later. NM never removed `nmss0`, managed (R4m) or unmanaged (R8m),
  because it deletes only links it created **[src]**. With `nmss0`
  unmanaged NM did not assume the leftover link either, so no profile or
  active connection showed it. The spike's new instance has no startup
  cleanup: it answered "already stopped", knew nothing of the link, and
  the link stayed until the driver removed it. It then lingered until NM
  restarted, because the spike arms its idle timer only on a change to
  STOPPED. So the startup cleanup (below) and `ExecStopPost` on
  `nm-sshuttle.service` (§4.7) remove the link, and the new instance then
  exits. With resolved
  and `split`, removing the link ends the leak's effect on lookups (R4k).
  With `all`, or without resolved, the leak lasts until NM restarts
  (§4.5). Make kills rare: no `Restart=`, an `OOMScoreAdjust`, and
  packaging that never restarts the plugin under an active VPN.
- **Fallback, the invisible reconnect:** the plugin stays STARTED and sends
  nothing during a gap, so NM and GNOME show "connected" while the guard
  keeps traffic failing closed (R4d). It is used when the plugin cannot
  find its active connection, when NM's version is marked as above, or on
  request (`reconnect-display=invisible`). Before it becomes the default
  anywhere, the plugin needs a desktop notification for long outages.
- **Never:**
  - recreate `nmss0` while NM holds the VPN, or send `Config` for a link
    with another ifindex. That crashes NM 1.56, and even without a crash
    the VPN's DNS stays on the old ifindex (R4f). If the link is lost, the
    plugin fails the connection instead;
  - send `Config` or an IP config before NM has a device for the link, or
    to an NM instance other than the one that called `Connect`;
  - send more than one `Config` and one real `Ip4Config` per
    (re)configuration, plus the one sentinel;
  - change DNS servers or domains within an activation, because NM keeps
    the VPN's DNS as it was at "activated" (§2.1);
  - send anything after NM has begun deactivating, except the final
    STOPPED.
- **The user switches off during a reconnect:** NM deactivates with "user
  disconnected", so GNOME shows no notification. NM removes the VPN's DNS
  itself and calls `Disconnect`. The plugin cancels any pending reconnect
  timer, stops the tunnel and removes the guard and `nmss0`. **[spike]**
  In R4h resolved dropped `nmss0`'s DNS before the plugin removed the link,
  and nothing came back. In runs 4 to 6 the plugin cancelled the timer at
  `Disconnect` (R4h-k) and was still running when it was due. In runs 5
  and 6 it exited at its idle timeout, 60 s after STOPPED. Run 5's R4h-p
  FAIL came from the driver's `busctl status` check, not from the plugin;
  its cause is unknown. Run 6 checked by bus name and PID, and R4h-p
  passed. If a STARTING raced the click and NM went back to "connecting",
  the plugin calls `DeactivateConnection` on its own connection (untested).
- **Behind the lock screen**, a key the agent has already loaded signs
  without a prompt. A key that is not loaded fails fast, because gcr's
  prompt is cancelled at once (**[spike]** R6b: 1 s). On that failure,
  with logind's `LockedHint` set, the plugin waits (NM keeps showing
  "connecting"), retries once after unlock (allowing 110 s for the
  prompt), and reports "login failed" if that fails. The wait and the
  retry are untested: the spike plugin has neither, and it reported
  "connect failed".
- **The network changes** (primary connection or its IP config changes,
  connectivity returns): treat it as a drop and restart the tunnel **at
  once**. Lab T10 shows ssh does not notice a new address for tens of
  seconds. If the first hop's address changed, it goes into the guard's
  exclude set before the tunnel starts.
- **Suspend:** the plugin holds a logind *delay* inhibitor while the toggle
  is on.
  - `PrepareForSleep(true)`: send STARTING, stop the tunnel, release the
    inhibitor. After resume the lock screen shows "connecting" until the
    tunnel is really back.
  - `PrepareForSleep(false)`: take the inhibitor again and reconnect as
    soon as NM reports connectivity.
- **Health (every 10 s while up)**, from the plugin's own checks; NM's
  state is not evidence (see "Activated is not proof"):
  - the tunnel unit is active;
  - sshuttle's nft table exists and a listener holds its port;
  - the guard table exists (re-install it if something flushed the ruleset);
  - optionally, the `probe` TCP connect through the tunnel, where two
    failures in a row cause a restart. This covers sshuttle's "process
    alive, forwarding dead" reports (#285 and others).
- **Toggle off (`Disconnect`):** stop the tunnel, remove the guard and
  `nmss0`, then sweep. A `Disconnect` while already stopped is ignored, so
  that NM sees only one STOPPED.
- **The plugin starts:** at every process start, before or right after
  answering the first call, remove a leftover guard table, `nmss0`, stale
  sshuttle tables and a running tunnel unit. Do not wait for a `Connect`:
  after a kill the first call is NM's `Disconnect` (R4m). This is safe
  because NM disconnects the VPN whenever the plugin's name vanishes
  **[src]**. With the shim this is plain systemd and nft work. In direct
  mode it needs a `NetworkManager-*` tunnel unit, because a plugin in
  `NetworkManager_t` could not even query one with an ordinary name (R1a,
  R1e).
- **NetworkManager goes away** (its bus name is lost): remove `nmss0`
  first, so that a quickly restarted NM cannot pick it up. Then stop the
  tunnel, remove the guard and exit, without answering the new NM. The
  restarted NM does not reattach the VPN, so the toggle is off and nothing
  may stay behind. **[spike]** In R7 (runs 4 to 6) the plugin removed
  `nmss0` first, and the restarted NM did not list it.

Expected timing:

- after a roam, the tunnel is back after one ssh handshake through the jump
  host;
- a silently dead path is noticed within about 30 s (ServerAlive 10 s × 3);
- traffic to tunnelled networks fails immediately in between, and never
  leaks to the local network.

**Checklist for the reconnect code**, from the six spike runs. Each
item is explained above or in §2.1.

- One `nmss0` per activation, same ifindex throughout; if it vanishes,
  give up at once (branch 4), without bouncing.
- After the first "activated", `nmss0` must be in state 10 (unmanaged);
  any other state: log risk 11.
- Before any `Config` except a give-up's (branch 4): same ifindex, NM has
  a device for it (not a known stale path), same NM bus owner, own active
  connection still activating.
- Follow only the plugin's own active connection (by UUID). Ignore reason
  6 on reconnects.
- Burst from one callback: `Config`, sentinel `Ip4Config`, real
  `Ip4Config`, STARTED, with nothing in between. Same `dns` and `domains`
  in both `Ip4Config`s.
- Accept either NM outcome (sentinel committed for a moment, or never).
- Escalate: sentinel alone after 10 s, fail and mark the NM version after
  60 s. Millisecond timers: GLib's `timeout_add_seconds` fires up to
  0.25 s early or 0.75 s late **[src]**; the spike's 20 s timer took
  19.80–20.69 s (run 6).
- Look up NM's device for `nmss0` at once, then poll; never reuse a path
  from an earlier activation.
- Stop the tunnel unit without blocking the main loop.
- NM's "activated" during a gap is not health; bounce at most 3 times.
- `Disconnect` cancels every pending timer first, then tears down, then one
  STOPPED.
- Give-up: pick the branch from cached state, with no D-Bus call before
  the signals. In a gap or on link loss, send `Config`, `Failure` and
  STOPPED from one callback, with no `systemctl` call or link change in
  between. Remove the link 100 ms after "deactivated", at most 2 s after
  STOPPED.
- SIGTERM or a service stop in a gap runs the give-up first, sends its
  signals before any blocking call and waits for NM's `Disconnect`.
- After a kill, the next instance (which NM's `Disconnect` starts) or
  `ExecStopPost` stops the tunnel and removes `nmss0`, the guard and stale
  tables. That instance then exits.
- NM vanishes: remove `nmss0` first, then the tunnel and the guard; exit.
- Failure markers come from the tunnel unit's journal; with `LockedHint`
  set, wait for the unlock and retry once.
- Any systemd or D-Bus error is a failure, never "inactive".
- A `Connect` after STOPPED in the same process starts from clean state.
- In tests, read resolved per link while `nmss0` exists. `nmss0` has the
  servers and domains and default-route no. The uplink keeps its servers
  and default route. A split name resolves through the tunnel. Read
  `DnsManager.Configuration` only after `nmcli general reload dns-rc`. It
  must hold one VPN entry for this connection, on `nmss0`, after a
  (re)configuration, and none after a give-up. It must hold no entry
  without an interface, and no non-VPN `nmss0` entry. Check this, and the
  uplink without a default route, after every drop, not only the first.

### 4.5 DNS

| Mode | What happens |
|---|---|
| `split` (recommended) | `nmss0` carries `dns-servers` and `~dns-domains`. resolved sends only those domains there. sshuttle runs with `--ns-hosts` and `--to-ns` set to the same servers, and their /32s are added to `subnets` for DNS over TCP. |
| `all` | As `split`, with routing domain `~.`, so resolved sends every lookup through the tunnel. This replaces `--dns`, whose server list is a snapshot taken at start. |
| `none` | The link exists (for reconnects, §4.4) but carries no DNS. DNS is not touched. |

`--to-ns` is always set. It pins the remote resolver and avoids the tunnel
crash in lab T9.

`split` takes routing-only domains (`~corp.example`) by default. A plain
`corp.example` also becomes a search domain, so short names such as `git`
get completed with it; the profile can ask for that.

**Coverage.** The kernel side is covered: a query pinned to the link with
`IP_UNICAST_IF` is redirected and answered through the tunnel **[lab T3]**.
**[spike]** On Fedora 44, NM configured resolved's link as planned
(`DefaultRoute=no` plus the domains). Lookups for the domain went through
the tunnel while other names stayed on the uplink (§2.1, R3a and R3b).
resolved kept the link's DNS through every reconnect gap (R4). In runs 4
to 6 a split name resolved after every reconnect that kept the link (R4b,
R4c, R4g and R4i, -q), and in runs 5 and 6 also with `nmss0` unmanaged
(R8b, R8g, R8c, and R8j in run 6). Not yet covered: `all`, TCP fallback,
and a real DNS server with EDNS0 and AAAA records.

**AAAA records are a leak path.** sshuttle runs with `--disable-ipv6`, and
the v1 guard covers IPv4. If a split domain answers AAAA and the uplink
has IPv6, applications would connect around the tunnel. The lab's DNS
server answers no AAAA, so this is untested. Either filter AAAA for the
split domains or bring the IPv6 guard sets forward (§7).

**Two DNS entries for one link (risk 11).** After a commit on a managed
`nmss0` that changes content while its external device is activated, NM
also files
`nmss0`'s merged configuration, VPN DNS included, as a non-VPN entry
(§2.1). That happened after every content-changing commit on a managed
`nmss0`: R4c and R4i in runs 4 to 6, R4g in run 4, and a reapply.
**[spike]** With the uplink's default routes removed (R4c-o, runs 4 to
6), that entry took resolved's default route. resolved set `nmss0` to
`DefaultRoute=yes`, set the uplink to `no` and reset the uplink's DNS
server list. For those 3–4 s an outside name (fedoraproject.org) went
through the tunnel to the internal server. In runs 4 and 5 another
process sent its lookup there first, within a second of the change. Its
query pattern matches NM's own connectivity check (inferred). Run 6 saw
no such lookup in 4 s. In run 6's other -o probes, where no such entry
existed, the uplink kept resolved's default route (R4b, R8b, R8c, R8j).
`DnsManager.Configuration` did not change. **[src]** Why:
when no entry with servers has a main-table default route, NM gives the
automatic `~` to every non-VPN entry, and the lower priority value wins.
The `nmss0` entry carries the VPN's 50 and beats the uplink's 100.
resolved's plugin then sends no servers for a link with neither domains
nor default route. The VPN entry alone never takes `~`. The trigger is an
uplink whose DNS-carrying configurations lack a main-table default route in
both families (`never-default`, a custom route table). An offline uplink
triggers it too, but then there is no other resolver to lose. When it
happens, every lookup goes to the corporate resolver and fails during a
gap. After a reapply the entry has priority 100, ties with the uplink, and
both links would get the default (untested). **The fix is an unmanaged
`nmss0`** (§4.1). **[spike]** With it, R8b, R8g, R8c and R8j filed no
non-VPN entry. With the uplink's default routes removed, `nmss0` kept
`DefaultRoute=no`, and the uplink kept its server and default route (-o
PASS). R8b and R8g committed no new content, so only R8c and R8j tell the
two forms apart: their commits changed the content (R8j's twice: the
sentinel, then the real config), and NM still filed nothing, because the
device never enters the ip-config range **[src]**.
A user override that makes `nmss0` managed brings the risk back, and the
plugin logs it (§4.1). A profile `dns-priority` above 100 would not
cover the reapply entry and does not suit `all`.

**Without resolved** (NM writing `/etc/resolv.conf` itself, common on
Debian), `split` degrades to "every lookup goes to `dns-servers`". Document
it rather than special-case it. **[src]** A VPN DNS entry that NM fails to
remove, after a give-up from "connecting", a plugin kill in a gap (R4m, R8m)
or a relink (§4.4), would then stay in `/etc/resolv.conf` until NM restarts.
**[src]** NM merges every entry into the file by priority without checking
that its link exists. A leaked server therefore comes first, and its domain
becomes a search domain. With the tunnel and guard gone, each lookup would
first wait out the resolver timeout (5 s in glibc), and query names would
leave over the uplink. Untested. With resolved and `all`, a leaked entry
keeps claiming `~.`, so the uplink stays without DNS servers until NM
restarts **[src]**, untested. The give-up order in §4.4 is therefore
required, not cosmetic.

### 4.6 Fail-closed guard

The guard is present from `Connect` until `Disconnect`:

```nft
table inet nm-sshuttle-guard {
    set subnets4 { type ipv4_addr; flags interval; elements = { 10.0.0.0/8 } }
    set exclude4 { type ipv4_addr; flags interval; elements = { 203.0.113.7 } }
    chain output {
        type filter hook output priority filter; policy accept;
        ip daddr @exclude4 accept
        ip daddr @subnets4 ct status dnat accept            # redirected by sshuttle
        ip daddr @subnets4 meta l4proto tcp reject with tcp reset
        ip daddr @subnets4 reject with icmp admin-prohibited
    }
}
```

The same pattern applies to IPv6. sshuttle's NAT redirect runs earlier, at
priority `dstnat`, so packets it redirected carry conntrack status `dnat`
when they reach the guard.

**[lab T4]** In the lab, while the tunnel was down, connections failed in
about 10 ms instead of leaking to the default route. In the lab's container,
that default route even answered queries for the internal DNS server's
address.

v1 guards only locally generated traffic (`output`), not forwarded traffic
from local VMs or containers.

### 4.7 systemd units

```ini
# nm-sshuttle.service
[Unit]
Description=NetworkManager sshuttle VPN plugin

[Service]
Type=dbus
BusName=org.freedesktop.NetworkManager.sshuttle
ExecStart=/usr/libexec/nm-sshuttle/nm-sshuttle-service
# after a stop, kill or crash: stop the tunnel unit, remove nmss0, the
# guard and stale tables
ExecStopPost=/usr/libexec/nm-sshuttle/nm-sshuttle cleanup
Restart=no
OOMScoreAdjust=-500
```

```ini
# nm-sshuttle-tunnel.service
[Unit]
Description=sshuttle tunnel for nm-sshuttle
StopWhenUnneeded=no

[Service]
Type=notify
NotifyAccess=main
ExecStartPre=/usr/libexec/nm-sshuttle/nm-sshuttle sweep
# reads /run/nm-sshuttle/tunnel.json (written by the plugin) and execs sshuttle
ExecStart=/usr/libexec/nm-sshuttle/nm-sshuttle exec-tunnel
ExecStopPost=/usr/libexec/nm-sshuttle/nm-sshuttle sweep
Restart=no
TimeoutStartSec=45
TimeoutStopSec=15
```

**Why the restart policy is `Restart=no`.** The supervisor owns the restart
policy, because only it knows about connectivity, sleep and authentication
failures. sshuttle must be the main process, so its own `READY=1` counts.

**Why `ExecStopPost`, and no `BindsTo=`.** A killed plugin leaves `nmss0`
behind, and NM does not remove it (R4m, R8m). `ExecStopPost` runs after the
main process is gone, on a stop and after a kill. It stops the tunnel
unit and removes `nmss0`, the guard and stale tables. The instance that
NM's `Disconnect` starts sweeps again (§4.4). The tunnel unit has no
`BindsTo=` or `PartOf=` on the plugin unit. With `After=`, a stop of the
plugin unit would stop the tunnel before the plugin gets SIGTERM; without
it, both would stop at once **[src]**. Either way the plugin could see a
drop before it gives up, against the order in §4.4. At system shutdown
both units still stop at once: a tunnel that dies first is a drop, and
the SIGTERM then gives up from the gap. All of this is untested, and so
is whether a D-Bus activation queued during `ExecStopPost` waits for it.
In direct mode there is no plugin unit and no `ExecStopPost`. The OOM
value is a choice, not a measurement.

**Why the default `KillMode=control-group` stays.** On stop it kills
sshuttle, its firewall helper and the user's `ssh`, which otherwise
outlives sshuttle on a dead path (lab T10e). The ssh process stays in the
tunnel unit's cgroup because `setpriv` changes only the user ID.
**[spike]** The cost is that the kill can also hit the helper's own
`nft delete table`, as it did 5 times in 142 stops (§2.2). The `ExecStopPost`
sweep is therefore required, not a safety net. Whether `KillMode=mixed`
lets the helper finish and still removes ssh is a lab question.

**Sandboxing** such as `ProtectSystem=strict`, `PrivateTmp`,
`RestrictAddressFamilies` and a capability bounding set (`CAP_NET_ADMIN`,
`CAP_SETUID`, `CAP_SETGID`) is added and tested one directive at a time.
The ssh child inherits the sandbox, and it must still read `~/.ssh` and
write `known_hosts`.

**The plugin unit's sandbox.** **[spike]** The shim-started plugin runs as
`unconfined_service_t` with every capability (R1b). A plugin that NM
spawns gets NetworkManager.service's eleven capabilities and, per its unit
file, `ProtectSystem=true` and `ProtectHome=read-only` (R1e).
`nm-sshuttle.service` starts from at least that set. R1e ran only a
minimal path under it; `ip` and `nft` there are untested.

### 4.8 Security notes

- **The sshuttle client runs as root**, and it parses the multiplexed
  stream coming from the remote host. Mitigations:
  - the sandboxing above;
  - `--auto-nets` off, so the remote cannot choose what gets captured;
  - `--auto-hosts` off, so `/etc/hosts` is never edited.

  Longer term, propose an upstream hook that lets an already-privileged
  caller provide the firewall helper. The client could then run as the user,
  which is upstream's privilege model without sudo.
- **The profile is validated** (§4.2) and its permissions are pinned to one
  user.
- **The bridge never runs anything as root except `setpriv`**, and ssh
  never runs as root.
- **D-Bus access:** only root may own or call the plugin's name.

### 4.9 Code layout and size

```
nm_sshuttle/
  service.py      VPN D-Bus interface (Gio), about 250 lines
  supervisor.py   state machine, backoff, health; pure logic, unit-testable
  watch.py        NM, logind and systemd D-Bus watchers
  profile.py      vpn.data parsing and validation
  tunnel.py       sshuttle argv, exec-tunnel, sweep, ssh-as-user
  guard.py        nft guard table, dummy link
  cli.py          add / check / list / remove (wraps nmcli); sweep and
                  cleanup, which the units run
data/             .name file, D-Bus policy and activation file, the two units,
                  the conf.d snippet that leaves nmss0 unmanaged,
                  auth-dialog stub (shell)
```

That is roughly 1,200 lines of Python.

- **Runtime dependencies:** python3 ≥ 3.10, python3-gobject, sshuttle ≥
  1.3.1, nftables, iproute2, util-linux (`setpriv`) and openssh-clients.
- **Packaging:** start with `meson` for install paths, then add RPM and
  Debian packaging.

### 4.10 Testing

- **Unit tests:** profile validation, argv building, exit classification,
  and the supervisor state machine driven by fake events and a fake clock.
- **`lab/run.sh`:** sshuttle and kernel behaviour, runnable in any
  root-capable container (§5).
- **VM tests** on Fedora (SELinux enforcing) and Ubuntu with GNOME:
  - the toggle appears and works;
  - `resolvectl status nmss0`;
  - `systemctl suspend`, or `rtcwake -m mem -s 20`;
  - roaming by switching between two NM connections;
  - `nft flush ruleset` while up;
  - `kill -9` of sshuttle;
  - `systemctl restart NetworkManager` while up;
  - after every reconnect: `resolvectl query --cache=no` for a split name
    and a public name, and `resolvectl dns`, `domain` and `default-route`
    for `nmss0` and the uplink. Then run `nmcli general reload dns-rc` and
    read NM's `DnsManager.Configuration`. It must show one VPN entry for
    this connection, on `nmss0`, and no entry without an interface;
  - reconnects with and without firewalld running;
  - the uplink without a default route, after each reconnect variant;
  - a give-up during a reconnect, checked while `nmss0` still exists;
    branch 2 inside a gap (NM showing "activated"); a give-up with
    firewalld stopped, and after a content-changing reconnect with a
    managed `nmss0`;
  - the plugin killed during a gap: the instance that NM's `Disconnect`
    starts removes `nmss0` and exits (untested in both forms: the spike
    plugin has no startup cleanup); resolved's state on `nmss0` between the
    kill and the link's removal, read before any `nmcli general reload
    dns-rc`; and the service stopped or restarted during a gap;
  - with `nmss0` unmanaged: a service stop in a gap, `nmcli device set
    nmss0 managed yes|no` in a gap, a NetworkManager restart under an
    active VPN, and one while a killed plugin's `nmss0` survives;
  - a link loss with a profile in a non-default firewalld zone, followed by
    another profile (NM leaves the binding, §2.1);
  - a user override of the unmanaged setting (plain `unmanaged-devices=`
    in `/etc`): the plugin logs it; and the setting applied with `nmcli
    general reload conf`, also under an active VPN;
  - branch 4 entered from pre-up and from "activated";
  - the park-in-ip-config-get candidate (§7, M2): a kill in a gap must
    then remove the DNS;
  - liveness checks by `GetNameOwner` and PID, never `busctl status`;
    every systemd tool runs with `--no-pager`;
  - a failing DNS check prints what it saw: resolved per link, NM's DNS
    entries and the lookup's output. A committed sentinel is read per
    burst, never by l3cd ID across a trace, because NM reuses the IDs
    (run 6);
  - `Connect` within the plugin's idle window after a `Disconnect`;
  - switching off while a reconnect timer is pending;
  - NetworkManager writing `/etc/resolv.conf` (`dns=default`), for Debian;
  - locking the key in the agent; a reconnect behind the lock screen,
    unlocked during the wait; and a lock at resume with auto-lock on.

## 5. Lab results

`lab/run.sh` builds a host, a jump host (sshd) and an internal network
(DNS + HTTP) with network namespaces. It runs sshuttle the way this design
does, prints PASS/FAIL for each claim, and tears everything down. The run
below used sshuttle 2.0.0 with the nft method in an Ubuntu 24.04 container
(kernel 6.18).

| Test | Claim | Result |
|---|---|---|
| T1 | Root sshuttle with ssh as the user (alias only in the user's config, key in the user's agent) carries TCP | PASS |
| T2 | 2.0.0's automatic remote exclusion fails when the client is root | PASS (it fails) |
| T3 | DNS to the internal server, also pinned to the tunnel link with `IP_UNICAST_IF`, is captured and answered via the jump host | PASS |
| T4 | Guard: redirected traffic passes and the excluded jump host stays reachable. Tunnel down: TCP refused in about 10 ms, DNS blocked locally | PASS |
| T5 | SIGTERM removes sshuttle's tables | PASS |
| T6 | A quick restart finds the old port in TIME_WAIT and picks another port, so table names change | PASS |
| T7 | SIGKILL leaves a stale table that refuses connections; the listener-based sweeper removes it | PASS |
| T8 | A fixed `--listen` port cannot be reused within TIME_WAIT (`EADDRINUSE`) | PASS |
| T9 | An unreachable remote resolver kills the tunnel on the first DNS query; `--to-ns` avoids it | PASS |
| T10 | After roaming: no keepalive leaves a dead tunnel running for 30 s or more; `ServerAlive` 5 s × 2 ends it in about 17 s, with cleanup. The user's `ssh` outlives sshuttle's exit | PASS |

The dummy link is replaced by a veth whose peer sits in an empty namespace,
because the lab kernel has no `dummy` module. For these tests the two
behave the same: link up, carrier, global /32.

## 6. Risks to retire first

[`spike/vm.sh`](../spike) checks these in a Fedora 44 VM with SELinux
enforcing; its report IDs are given for each. Six runs so far (2026-10-07
to 2026-10-10).

| # | Risk | Status after run 6 |
|---|---|---|
| 1 | NM accepts a shim `program=` that exits after D-Bus activation | **Retired** (R1b, runs 2 to 6): activation, traffic and teardown work. |
| 2 | SELinux | **Retired for the shim** (R1b, R2): plugin, sshuttle, tunnel unit and the user's `ssh` run as `unconfined_service_t`; the whole run logged no denials besides R1a's five. With an ordinary unit name, direct mode is denied even `is-active` (R1a). With a `NetworkManager-*` unit name it works on the minimal path only (R1e: start, stop, status, `dns=none`, no `nmss0`). `journalctl` is denied by policy; the link, guard and reconnects are untested (§4.1). |
| 3 | NM with a plugin-created dummy `tundev` | **Retired** (R3a to R3c, R8a): addressed, with split DNS through resolved and the tunnel, managed or unmanaged. NM's VPN code binds `nmss0` to the profile's firewalld zone (default `public`) and unbinds it at deactivation **[src]**. When the link is already gone, firewalld keeps `nmss0` in the zone until a later teardown or a firewalld restart (R4j, R4p, R8p). No DNS for `dns=none`. A single-label name is completed only with a plain domain. |
| 4 | gcr-ssh-agent unlock for an ssh outside the session | **Retired** with conditions (R6, §2.1). Behind the lock screen an unloaded key fails in 1 s (R6b). Waiting for the unlock and retrying is untested, and the test VM never locks at resume. |
| 5 | GNOME without an editor plugin | **Retired** (R5a to R5d, R5f). The toggle works. Without the stub Settings waits 25 s; with it the editor opens at once. GNOME shows the same "Connection failed" for every failure reason. |
| 6 | Reconnect display in NM | **Retired for 1.56.1** (§2.1, §4.4). In runs 3 to 6 the `nbns` sentinel recovered in every variant, each time 19–40 ms after `Config`: three drops (R4b), without firewalld (R4g), `dns=none` (R4e), two-phase (R4i), and with `nmss0` unmanaged (R8b, R8g, R8j). A split name resolved after each (-q, runs 4 to 6). The alternating address works (R4c, R8c). The invisible reconnect stays "activated" (R4d), and an unchanged config stays stuck (R4a, R8i). Relinking does not recover: it flips early and loses DNS (R4f). Other NM versions: same code **[src]**, untested. |
| 7 | NM restart while the VPN is up | **Retired** (R7, runs 3 to 6): the plugin tore down within about 0.1 s, and the restarted NM did not reattach. In runs 4 to 6 it removed `nmss0` first, and the restarted NM did not list it. R7 ran with a managed `nmss0`; untested with the unmanaged setting installed. The plugin's teardown does not depend on it. |
| 8 | Switching off during a reconnect | **Retired** (R4h, runs 3 to 6): NM ended the VPN with "user disconnected" and dropped its DNS, and nothing came back. The pending reconnect was cancelled at `Disconnect` (R4h-k, runs 4 to 6). Run 5's R4h-p FAIL came from the driver's `busctl status` check: the plugin ran until its idle exit 60 s after STOPPED. With a check by bus name and PID, R4h-p passed in run 6. Not tested: a STARTING that races the click **[src]**. |
| 9 | Giving up from "connecting" | **Retired on 1.56.1 for the order in §4.4** (runs 4 to 6, with trace). Re-sending the unchanged `Config` first failed the VPN from ip-config-get and removed its DNS while `nmss0` existed: R4l in each run, R4n (SIGTERM in a gap) and R8l (unmanaged). With `nmss0` lost, NM failed the VPN itself ("IP configuration invalid") and removed its DNS, from "connect", managed or unmanaged (R4p, R8p). STOPPED first left the entry until NM restarted, as on 1.46 **[smoke]**, and resolved kept the server on `nmss0` while it existed (R4k-d, R4k-k FAIL). Deleting the link first dropped the entry only through the early flip, with dead-link warnings (R4j). A SIGKILL in a gap leaks like R4k, managed or unmanaged (R4m-d, R8m-d FAIL); the startup cleanup and `ExecStopPost` limit it (§4.4). **[src]** The policy code is the same from 1.46 to `main`. Open, for M2: branch 4 from pre-up or "activated", branch 2 inside a gap, a give-up with firewalld stopped or after a content-changing reconnect with a managed `nmss0`, and SIGTERM with `nmss0` unmanaged. |
| 10 | Early "activated" during a gap | **Confirmed; handled by the health check** (§4.4). Traced triggers: deleting the link, managed or unmanaged (R4f 79–121 ms before the `Config`, R4j, R4p, R8p), and `nmcli device reapply nmss0` (R4a-i). With `nmss0` unmanaged the reapply is refused and does not flip (R8i); `nmcli device set nmss0 managed …` still does **[src]**. An address change and an uplink commit do not flip it (R4a, R4e, R8i, R8b). With the same ifindex the flip leaves the VPN's DNS correct **[src]**. With a managed `nmss0` a reapply also files a non-VPN entry (§4.5). |
| 11 | VPN DNS after the first "activated" | **Retired for an unmanaged `nmss0`** (R8, §4.1): no non-VPN entry and no default-route capture after R8b, R8g, R8c and R8j (-u, -o PASS). R8c and R8j changed the committed content, R8j with the sentinel committed and then the real config, and NM filed nothing, because it files a device entry only between ip-config and deactivating **[src]**. **A managed `nmss0` (user override) still has it:** every content-changing commit files a second, non-VPN entry at the VPN's priority (R4i, R4c, run 4's R4g, a reapply). In R4c-o it took resolved's default route in runs 4 to 6. Whether the `nbns` burst commits the sentinel is a race: never with firewalld (0 of 14 bursts), once without it (1 of 5, run 4's R4g). Relinking still loses DNS (R4f-d, -q, -n FAIL). |

**Upstream reports**, drafted and fact-checked in
[`docs/upstream/`](upstream/README.md) but not filed:

- NetworkManager: SIGSEGV in `_check_complete` when a VPN's `tundev` is
  recreated (1.56 and later), with a patch;
- NetworkManager: a VPN reconnect with an unchanged config stays
  "activating" forever (since 1.36); now with a 1.56.1 trace, the
  observed early "activated" and a trace of the reapply that releases it
  (run 4);
- NetworkManager: VPN DNS left behind or lost, drafted from run 4 and
  confirmed in runs 5 and 6. A VPN that fails from "connect", or whose
  plugin is killed, keeps its DNS entry (R4k, R4m, R8m, against R4l). A
  relinked VPN gets no DNS on the new link and keeps the old entry, also
  after `Disconnect` (R4f). `DnsManager.Configuration` is a stale
  snapshot. A tundev's external device entry can take resolved's default
  route from the uplink (R4c-o); an unmanaged tundev avoids it (R8c, R8j);
- GNOME Shell: a VPN plugin without `auth-dialog` leaves secrets requests
  unanswered;
- sshuttle: the `connect()` crash in server-side DNS forwarding (lab T9),
  `SO_REUSEADDR` on the listener (lab T6, T8), and automatic exclusion
  ignoring `-e` (lab T2).

Later, also propose a hook for an externally provided firewall helper
(§4.8).

## 7. Roadmap

1. **M0 (done):** research and lab.
2. **M1, spike (done, six runs):** [`spike/vm.sh`](../spike) on a
   Fedora 44 host creates a Fedora 44 VM with GNOME and runs a bare plugin
   through the risks in §6. Risks 1 to 9 are retired (9 for the order in
   §4.4, on 1.56.1), and risk 10 is handled by the health check. Risk 11
   is retired by an unmanaged `nmss0` (runs 5 and 6, R8). The sixth run
   (2026-10-10) covered three more cases with `nmss0` unmanaged: the
   two-phase escalation (R8j), link loss in a gap (R8p) and a kill in a
   gap (R8m). Each behaved as with a managed `nmss0`, except that R8j
   filed no non-VPN entry (unlike R4i). Still open with `nmss0` unmanaged:
   a service stop in a gap, `nmcli device set nmss0 managed …` in a gap,
   an NM restart under an active VPN, and one while a killed plugin's
   `nmss0` survives. These and the rest of §4.10 belong to M2's VM tests.
3. **M2, lifecycle:** persistence, reconnect and backoff, sleep, roaming,
   guard, sweeper, startup and `ExecStopPost` cleanup, the unmanaged
   `nmss0` setting and its check, health checks and unit tests. One
   candidate to try in the VM tests before adopting it: during a gap, keep
   NM in ip-config-get with a lone, unchanged `Config`. A kill would then
   remove the DNS too **[src]**. It is untested, and NM's firewalld call
   and `_apply_config` must commit nothing, or the VPN flips.
4. **M3, usable:** CLI, packaging (RPM, deb), documentation, and VM tests in
   CI if feasible.
5. **Later:** GTK4 editor plugin for GNOME Settings, several tunnels at once
   (template units), UDP through `tproxy`, IPv6 guard sets.
