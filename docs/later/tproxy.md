# UDP through `method=tproxy`

Status: implemented. One of the "Later" items of the
[design](../design.md) (§7); the index is in [README.md](README.md).
"What was built" at the end lists the results and where the code differs
from this design.

v1 tunnels TCP only. sshuttle's `nft` and `nat` methods redirect TCP, plus
UDP to port 53 of the `--ns-hosts` servers; any other UDP to a tunnelled
network is refused by the guard. Internal services that speak UDP (QUIC,
a DNS server queried directly, NTP, syslog, SNMP, RADIUS, some VoIP) do
not work. sshuttle carries UDP only with its `tproxy` method. This design
adds `method=tproxy` to the profile.

## Goals and non-goals

Goals:

- `method=tproxy` tunnels TCP and UDP to the profile's networks, IPv4 and,
  with the [IPv6 design](ipv6.md), IPv6.
- The guard keeps everything failing closed while the tunnel is down, UDP
  included.
- The plugin owns the policy routing that `tproxy` needs, and removes it
  with everything else.

Not in this step:

- **ICMP.** sshuttle carries no ICMP; `ping` to a tunnelled address still
  fails (the guard refuses it).
- **UDP to the internet** (`subnets=0.0.0.0/0`) is possible, but not
  designed for: the first hop's own traffic and DNS are covered, other
  corner cases of a full tunnel are not.
- **Changing the default.** `nft` stays the default; `tproxy` is opt-in
  per profile.

## What the research found

Sources: sshuttle 1.3.2 and 2.0.0 (PyPI sdists; `methods/tproxy.py` is the
same in both) and NetworkManager `main` (1.59.2-dev, ef234f8).

- **[src] `tproxy` is the only method with UDP** (`docs/tproxy.rst`).
  sshuttle turns UDP on by itself when the method has it
  (`client.py`, `required.udp = avail.udp`).
- **[src] It uses iptables, not nft.** The firewall helper adds three
  chains to the `mangle` table per family, named after the redirect port:
  `sshuttle-m-PORT` (hooked into `OUTPUT`), `sshuttle-t-PORT` (hooked into
  `PREROUTING`) and `sshuttle-d-PORT` (`methods/tproxy.py:132-140`). It
  needs both `iptables` and `ip6tables` (`is_supported`, `:261-266`).
  With iptables-nft these chains are nft chains in `table ip mangle` and
  `table ip6 mangle`.
- **[src] How a packet travels.** In `mangle OUTPUT` the mark chain sets
  the mark (`--set-mark TMARK`, the whole 32-bit mark) on TCP and UDP to
  the subnets, and on UDP port 53 to the name servers
  (`methods/tproxy.py:148-152`, `:195-232`). A policy routing rule must
  then send marked packets to a table whose `local` default route points
  at `lo`. They come back in through `PREROUTING`, where `TPROXY` hands
  them to sshuttle's listener (`--on-port`), and `-m socket` diverts the
  packets of existing flows. Exclusions and local destinations `RETURN`
  before the mark (`:166-171`).
- **[src] sshuttle does not install the routing.** Its documentation asks
  the user to run, once per boot:

  ```
  ip route add local default dev lo table 100
  ip rule add fwmark {TMARK} lookup 100
  ip -6 route add local default dev lo table 100
  ip -6 rule add fwmark {TMARK} lookup 100
  ```

  `TMARK` is `--tmark`, `0x01` by default (`docs/tproxy.rst`,
  `options.py`). sshuttle 1.3.1 and later have `--tmark`.
- **[src] UDP flows expire.** The client forgets a UDP source after 30 s
  without traffic (`client.py:555`); the server side expires its sockets
  likewise. A protocol that is silent for longer starts a new flow.
- **[src] The listener needs `IP_TRANSPARENT`,** which needs root
  (`setsockopt_error`). The sshuttle client already runs as root here
  (§4.3).
- **[src] NetworkManager leaves the routing alone.** NM prunes routes on a
  device only in the main table unless the profile sets a route table or
  a local route ("sync mode main", `nm-platform.c:4970-4973`,
  `nm-device.c:6112-6149`). The `local default` route sits in its own
  table on `lo`, which NM ≥ 1.42 manages through its in-memory `lo`
  connection; that connection has no route table set. NM removes routing
  rules only when it configured them itself (inferred from its global
  rule tracker, `nm-l3cfg.c`). Both are untested.
- **[reported] Other software uses policy routing too.** Tailscale uses
  table 52 with rules at priorities 5210 to 5270; wg-quick uses 32764 and
  32765. A rule of those that runs first and has a route for a tunnelled
  address would take marked packets away from `lo`.
- **[src] The guard does not see a DNAT.** The guard accepts packets that
  carry conntrack status `dnat` (§4.6); `TPROXY` does no NAT. What a
  `tproxy` packet carries at the guard's hook (`output`, priority
  `filter`, 0) is the mark set in `mangle OUTPUT` (priority -150) **[src,
  netfilter priorities]**. Without a change the guard would refuse every
  tunnelled packet.

## Design

### Profile

`method` takes `tproxy` next to `nft` and `nat`. Nothing else in the
profile changes.

At `Connect`, before anything is installed, the plugin checks that
`iptables` and `ip6tables` exist (the same test as sshuttle's
`is_supported`) and fails with "method=tproxy needs iptables and
ip6tables" otherwise.

### Mark, table and rule

Each activation gets its own mark and table, so that tunnels from the
[multiple tunnels design](multiple-tunnels.md) do not share them:

| | Value | Why |
|---|---|---|
| mark | `0x4e530000 + slot` | sshuttle sets the whole mark, so the value must be one nobody else sets. "NS" in the high half; the low byte is the slot (0 without multiple tunnels). |
| table | `0x4e53 + slot` (20051 + slot) | Outside the ranges listed above. |
| rule priority | `100 + slot` | Ahead of Tailscale's and wg-quick's rules, so marked packets reach `lo` first. The rule matches only this mark, so it touches no other traffic. |

The plugin installs, for each family the tunnel uses, before it starts the
tunnel unit and right after the guard:

```
ip rule add priority 100 fwmark 0x4e530000/0xffffffff lookup 20051
ip route replace local default dev lo table 20051
```

and passes `--tmark 0x4e530000` to sshuttle. Both are removed together
with the guard, by the plugin's teardown, its startup cleanup and
`nm-sshuttle cleanup` (`ExecStopPost`). Removal goes by the exact
priority, mark and table, and never flushes a table or rules it does not
own.

The routing stays in place for the whole activation, like the guard: it
does nothing to packets that carry no mark, and during a gap sshuttle's
chains, the only ones that set the mark, are gone.

### The guard

The guard gains one rule per family, ahead of the `dnat` rule:

```nft
ip daddr @subnets4 meta mark 0x4e530000 accept      # marked for tproxy by sshuttle
ip6 daddr @subnets6 meta mark 0x4e530000 accept
```

They are present only for `method=tproxy`. While the tunnel is down,
nothing sets the mark, so TCP gets a reset and UDP an ICMP "administratively
prohibited", as today. A stale mark chain left by a killed helper (§2.2)
would still mark packets; while the plugin's rule is in place they reach
`lo`, where `TPROXY` finds no listener and drops them (inferred), so they
still do not leak. The sweep removes such
chains (below).

### Sweep and health

- **Sweep:** a `tproxy` tunnel's leftovers are iptables chains, not an
  nft table. `tunnel.sweep()` also lists `iptables -t mangle -S` and
  `ip6tables -t mangle -S`, and for every `sshuttle-[mtd]-PORT` chain
  whose port has no listener it deletes the jump from `OUTPUT` or
  `PREROUTING`, then flushes and deletes the chain. It skips this when
  iptables is missing. It runs through iptables, not nft, so that it also
  works on hosts that use iptables-legacy.
- **Health:** the check that sshuttle's table exists becomes, for
  `tproxy`, a check that `sshuttle-m-PORT` exists in `mangle` for each
  family the tunnel uses, with a listener on its port, and that the
  plugin's own rule and route are there. A missing chain is a drop, as a
  missing nft table is now. A missing rule or route is put back, as the
  guard is.
- `nft flush ruleset` removes iptables-nft's chains too, so the check
  sees it like M2-17 does for `nft`. It does not remove the rule or route.

### DNS

With `tproxy`, sshuttle captures UDP port 53 to the `--ns-hosts` servers
through the same mark and routing (`methods/tproxy.py:148-152`), rather
than through `redirect`. resolved sends to `nmss0`'s servers with
`IP_UNICAST_IF` set to `nmss0` (lab T3b). That option fixes the output
interface of the first route lookup only. The kernel's second lookup
after the mark is set uses the socket's bound device, which
`IP_UNICAST_IF` does not set, so the packet should be rerouted to `lo`
like any other (inferred; the kernel source was not read for this
design). Lab T3b covered only the `nft` method's `redirect`, so this is
the first thing to test (T15 below). If it fails, the plugin refuses
`dns=split` and `dns=all` together with `method=tproxy` until there is a
fix.

### Dependencies

`iptables` (and `ip6tables`) become a weak dependency: `Recommends:` in
the RPM and in the deb. A profile with `method=tproxy` on a host without
them fails at `Connect` with the message above, and `nm-sshuttle check`
reports it too.

## What changes in the code

- `profile.py`: accept `tproxy`.
- `tunnel.py`: `--tmark` in `sshuttle_argv`; the iptables part of
  `sweep()`; the per-method health snapshot.
- `guard.py`: the `meta mark` rules; `install_policy_routing(slot,
  families)` and `remove_policy_routing(slot)`, each a few `ip` calls.
- `supervisor.py`: install the routing with the guard and remove it with
  the guard; the health check per method; the `iptables` check at
  `Connect`.
- `cli.py`: `cleanup` removes the rule and route; `add --method tproxy`;
  `check` looks for iptables.
- Packaging: the weak dependency.

About 120 lines of Python plus tests.

## Tests

- **Unit:** argv with `--tmark`; the guard ruleset with the mark rules for
  `tproxy` only; the sweep's parsing of `iptables -S` output and the
  commands it runs for a stale and a live port; teardown removing the
  rule and route by exact values.
- **Lab** (`lab/run.sh`; needs `iptables`, `xt_TPROXY` and `xt_socket` in
  the lab kernel, which is to be checked first):
  - T15: DNS sent to the internal server with `IP_UNICAST_IF` pinned to
    the tunnel link is captured and answered through the jump host, as
    T3b for `nft`.
  - T16: a UDP echo and a TCP connection to the internal network through
    the tunnel.
  - T17: tunnel down: UDP to the internal network fails locally at once,
    TCP is refused in about 10 ms, and nothing leaves the uplink
    (capture on the uplink).
  - T18: SIGKILL of sshuttle leaves the mangle chains; the sweep removes
    them, and the rule and route stay until the plugin removes them.
  - T19: a UDP flow silent for 40 s, then traffic again (a new flow).
- **VM:** a profile with `method=tproxy`: QUIC (`curl --http3`) or a UDP
  echo to the internal network; the reconnect scenarios M2-03 and M2-16;
  `nft flush ruleset` (M2-17); a gap, checked for UDP as well as TCP;
  that NetworkManager leaves the rule and route alone across a roam and
  `nmcli general reload`; no SELinux denial (`iptables` runs from the
  helper as `iptables_t`).

## What was built

The design as above, with these results and details:

- **DNS works (lab T15):** a query pinned to the tunnel link with
  `IP_UNICAST_IF` is rerouted to `lo` by the mark and answered through
  the jump host, so `dns=split` and `dns=all` stay allowed with `tproxy`.
- **Per slot:** slot N uses mark `0x4e530000` + N, table 20051 + N and
  priority 100 + N ([several tunnels](multiple-tunnels.md)).
- **The routing is installed whether or not the profile fails closed**
  (`fail-closed=no` drops only the guard), and only for the families the
  tunnel carries. It is removed for both families.
- **The sweep** runs `iptables -w -t mangle -S` and `ip6tables`, deletes
  the `OUTPUT` and `PREROUTING` jumps to stale chains, then flushes and
  deletes the chains. Listing does not create a `mangle` table.
- **Health:** a family without a live `sshuttle-m-PORT` chain is a drop,
  as a missing nft table is for `nft`; a missing rule or route is put
  back. `nft flush ruleset` removes iptables-nft's chains and the guard,
  not the rule or route. With iptables-legacy the chains are not in nft
  and survive the flush, so only the guard comes back and the tunnel
  stays up. Fedora's sshuttle package pulls in iptables-legacy, and in
  the test VM `iptables` then uses it (VM TP-02).
- **Packaging:** `Recommends: iptables` (deb) and `iptables-nft` (RPM).
  `nm-sshuttle check` fails a `tproxy` profile without them.
- **Tests:** lab T15 to T19 with the plugin's own guard, routing and
  sweep, a route to the internal network through the jump host and a
  counter on the uplink, so that a leak would show. T18 shows that stale
  chains black-hole UDP rather than leak it. VM scenarios TP-01 (UDP,
  TCP and split DNS, a gap), TP-02 (killed sshuttle, `nft flush
  ruleset`) and TP-03 (`nmcli general reload` and a roam leave the rule
  and route alone) pass on Fedora 44.

## Open questions

- Whether `rp_filter` or a firewalld setting drops packets that come back
  in on `lo` with an uplink source address. The loopback device is
  normally exempt from reverse path filtering (inferred).
- Whether NetworkManager leaves the rule and the route on `lo` alone on
  every target release. It does on Fedora 44 (VM TP-03).
