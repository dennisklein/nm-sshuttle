# IPv6 subnets and guard sets

Status: implemented. One of the "Later" items of the
[design](../design.md) (§7); the index is in [README.md](README.md).
"What was built" at the end lists where the code differs from this
design.

v1 is IPv4 only: the profile refuses IPv6 networks, sshuttle runs with
`--disable-ipv6`, and the guard has only `subnets4` and `exclude4`. §4.5
names the cost: if a split domain answers AAAA records and the uplink has
IPv6, applications connect around the tunnel. This design lets a profile
list IPv6 networks, tunnels them like IPv4 ones, and makes them fail closed.

## Goals and non-goals

Goals:

- `subnets` and `exclude` accept IPv6 networks; sshuttle tunnels them.
- The guard gets `subnets6` and `exclude6` and refuses IPv6 traffic to the
  tunnelled networks while the tunnel is down, as it does for IPv4.
- A first hop that has only an IPv6 address works, and every address of
  the first hop is excluded, whichever family ssh picks.
- `gateway` and `probe` accept IPv6 addresses.

Not in this step:

- **IPv6 DNS servers.** `dns-servers` stays IPv4 (below, "Why no IPv6 DNS
  servers").
- **IPv6 on `nmss0`.** The link keeps 192.0.0.8/32 and `has-ip6` stays
  false.
- **A filter for AAAA answers.** The plugin has no DNS proxy of its own;
  resolved passes answers through, and sshuttle forwards raw packets. The
  remedy for the leak is to list the internal IPv6 networks.
- **Forwarded traffic** (VMs, containers), as for IPv4 (§4.6).

## What the research found

Sources: sshuttle 1.3.2 and 2.0.0 (PyPI sdists, the same code in the
places below) and NetworkManager `main` (1.59.2-dev, ef234f8).

- **[src] sshuttle tunnels IPv6 unless told not to.** Without
  `--disable-ipv6` it listens on `::1` as well as `127.0.0.1`, on the same
  port (`client.py:885-899`, `:1052-1084`). Both the `nft` and the `nat`
  methods support IPv6. The `nft` method puts the IPv6 rules into a second
  table, `inet sshuttle-ipv6-PORT` (`methods/nft.py:20-23`), and only when
  there are IPv6 subnets or name servers (`firewall.py:333-338`). The
  sweeper's pattern already matches it (`tunnel.py`, `SSHUTTLE_TABLE_RE`).
- **[src] A host with IPv6 switched off makes sshuttle fail.** If the
  `::1` bind fails, sshuttle stops with "Could not bind to an IPv6 socket
  … Run sshuttle with '--disable-ipv6'" (`client.py:173-186`). With
  `--disable-ipv6` sshuttle drops IPv6 subnets and exclusions with a
  warning (`client.py:926-930`, `:950-956`).
- **[src] The redirect needs a route.** sshuttle's rules run in the output
  hook, after the kernel has picked a route. Without an IPv6 route to the
  destination, `connect()` fails with "network unreachable" before the
  rules see the packet. VM test M2-24 showed the same for IPv4, and
  sshuttle's `docs/tproxy.rst` says so for its other method. On an uplink
  without IPv6, IPv6 subnets therefore fail closed by themselves, and
  dual-stack clients fall back to IPv4 (inferred).
- **[src] NM takes an IPv6 external gateway.** `Config`'s `gateway` may be
  an `ay` of 16 bytes instead of a `u` (`nm-vpn-connection.c:1516-1529`).
  NM accepts the config if either family has a gateway (`:1879-1889`). The
  host route NM adds towards the gateway is suppressed per family by
  `ipv4.auto-route-ext-gw` and `ipv6.auto-route-ext-gw` (`:1316-1323`).
- **[src] DNS servers in `Ip4Config` are IPv4 only.** IPv6 servers go into
  an `Ip6Config`, which needs `has-ip6`. NM then waits for that config too
  before the VPN is complete, and NM's reconnect handling (§2.1) was
  traced only for `Ip4Config`.
- **[src] Python's `ipaddress.collapse_addresses` refuses mixed
  families** (a `TypeError`), and nft refuses overlapping intervals in a
  set (§7, M2 first run). Both collapse steps (`profile.collapse`) must
  work per family.
- **[src] nft's `reject` in an `inet` table** takes `with icmpx
  admin-prohibited` for both families, and `ct status dnat` matches
  IPv6 NAT the same way. sshuttle's IPv6 redirect is a DNAT to `::1`.

## Design

### Profile

| Key | Change |
|---|---|
| `subnets` | IPv4 and IPv6 networks, mixed. At least one network of either family. |
| `exclude` | IPv4 and IPv6. |
| `gateway` | An IPv4 or IPv6 address. |
| `probe` | `ADDRESS:PORT` or `[IPV6]:PORT`. |
| `dns-servers` | Unchanged: IPv4 only. An IPv6 server is refused with a message that names this limit. |

No address or network key takes a scope id (`fe80::1%eth0`): `ipaddress`
accepts one and keeps its text, which is free, so `profile.parse` refuses it
(design §4.2). Only `remote` may have one, since it never reaches nft.

`profile.parse` keeps networks as strings; `Profile` gains nothing new.
`guarded_networks()` returns both families, each collapsed on its own.
The IPv6 /128s of DNS servers do not arise, since servers stay IPv4.

The CLI sets `ipv6.auto-route-ext-gw=no` next to the IPv4 one, for the
same reason (§4.1), and `nm-sshuttle check` reports a profile without it as
a warning, not a failure, because profiles made before this change lack it.

### sshuttle's command line

- `--disable-ipv6` stays when the profile has no IPv6 network. sshuttle
  then behaves exactly as today.
- With an IPv6 network it goes, unless IPv6 is off on the host: the
  plugin reads `/proc/sys/net/ipv6/conf/lo/disable_ipv6` (and checks that
  `/proc/net/if_inet6` exists). If IPv6 is off, it keeps
  `--disable-ipv6`, logs that the IPv6 networks are guarded but not
  tunnelled, and still installs the IPv6 guard sets. No IPv6 packet can
  leave such a host, so nothing leaks.
- `-x` is given for every first-hop address (below) and every `exclude`
  entry, of both families.

### The first hop

Today `first_hop()` resolves the last hop's name with `AF_INET` only and
returns one address. ssh resolves the same name with its own
`AddressFamily` (default `any`) and connects to whichever address works
first, which is often IPv6. If that address lies inside a tunnelled IPv6
network, sshuttle would capture ssh's own connection.

- `first_hop()` resolves with `AF_UNSPEC` and returns every address.
- All of them go into `-x` and the guard's exclude sets.
- `Config`'s `gateway` gets the first IPv4 address, or the first IPv6
  address (as `ay`) if there is none. NM needs only one (above).
- The tunnel spec (`tunnel.json`) carries the list instead of one string.
  `exec-tunnel` accepts both forms for one release, so that a tunnel unit
  started by an older plugin during an upgrade still runs.

### The guard

```nft
table inet nm-sshuttle-guard {
    set subnets4 { type ipv4_addr; flags interval; elements = { 10.0.0.0/8 } }
    set exclude4 { type ipv4_addr; flags interval; elements = { 203.0.113.7 } }
    set subnets6 { type ipv6_addr; flags interval; elements = { 2001:db8:10::/48 } }
    set exclude6 { type ipv6_addr; flags interval; elements = { 2001:db8:ffff::7 } }
    chain output {
        type filter hook output priority filter; policy accept;
        ip daddr @exclude4 accept
        ip6 daddr @exclude6 accept
        ip daddr @subnets4 ct status dnat accept
        ip6 daddr @subnets6 ct status dnat accept
        ip daddr @subnets4 meta l4proto tcp reject with tcp reset
        ip6 daddr @subnets6 meta l4proto tcp reject with tcp reset
        ip daddr @subnets4 reject with icmpx admin-prohibited
        ip6 daddr @subnets6 reject with icmpx admin-prohibited
    }
}
```

- The IPv6 sets are always there, empty when the profile has no IPv6
  network. The ruleset then has one shape, and the health check's "guard
  present" test does not change.
- Collapsing is per family; the IPv6 sets get their own `collapse`.
- A profile with only IPv6 subnets has an empty `subnets4`, which nft
  accepts.

### Network changes

The network key (`watch.py`, §4.4) is the primary connection's UUID with
its IPv4 addresses and gateway; "an IPv6 change (the tunnel is IPv4) is no
change". Once the tunnel can use IPv6, that is wrong for a first hop
reached over IPv6.

- When the tunnel uses IPv6 (an IPv6 network in the profile, or a first
  hop with an IPv6 address), the key also holds the primary connection's
  IPv6 gateway and its global prefixes, **not** its addresses. Temporary
  (privacy) addresses rotate during the day and must not restart the
  tunnel. Prefixes come from `AddressData` (`address`/`prefix`), skipping
  link-local ones.
- Otherwise the key stays as it is.

### Health

`nft_health()` reports whether a sshuttle table with a live listener
exists. With IPv6 tunnelled, sshuttle has two tables. The check asks for
each family it expects: `sshuttle-ipv4-P` when there is an IPv4 network,
`sshuttle-ipv6-Q` when IPv6 is tunnelled, each with root's listener of that
family on its port. A missing one is a drop, as now. P and Q differ since
the plugin passes the IPv4 port: `--listen 127.0.0.1:P,[::1]:0`, because
sshuttle fails when both families get a port (design §2.2, §4.8).

### Why no IPv6 DNS servers

An IPv6 `dns-servers` entry would need `has-ip6` and an `Ip6Config` with
`dns` on `nmss0`. NM then waits for both configs before "activated", the
`nbns` sentinel has no IPv6 twin, and none of the reconnect tracing of
§2.1 covers `Ip6Config`. That is a new round of spike work for little
gain: an internal resolver almost always also listens on IPv4. If a site
has IPv6-only resolvers, that becomes its own design, starting with a
spike run like R4 on `Ip6Config`.

## What changes in the code

- `profile.py`: networks of both families; `gateway` and `probe` of both
  families; `dns-servers` IPv4 only, with a clear message; per-family
  `collapse`.
- `guard.py`: the IPv6 sets and rules; `guard_ruleset(networks, exclude)`
  splits by family.
- `tunnel.py`: `first_hop()` returns all addresses; `sshuttle_argv` drops
  `--disable-ipv6` as above and passes every first-hop address to `-x`;
  `nft_health()` reports tables per family.
- `supervisor.py`: the gateway for `Config` from the first-hop list;
  `_install_guard` with both families; the health check per family.
- `service.py`: `config_variant` encodes an IPv6 `gateway` as `ay`.
- `watch.py`: the IPv6 part of the network key, when asked for.
- `cli.py`: `ipv6.auto-route-ext-gw=no`; `check` warns when it is missing.

About 150 lines of Python, most of them in `profile.py`, `guard.py` and
`tunnel.py`, plus tests.

## Tests

- **Unit:** profile parsing of mixed families, IPv6 `gateway` and
  `probe`, IPv6 `dns-servers` refused; per-family collapsing (a DNS /32
  inside a subnet, adjacent IPv6 prefixes); the guard ruleset with empty
  and filled IPv6 sets; `sshuttle_argv` with and without IPv6, and with
  IPv6 off on the host; `first_hop()` with A only, AAAA only and both;
  the network key ignoring a rotated temporary address.
- **Lab** (`lab/run.sh`, new tests, with IPv6 addresses on the
  namespaces):
  - T11: TCP to an IPv6 address in a tunnelled IPv6 subnet goes through
    the jump host.
  - T12: the guard with the tunnel down refuses IPv6 TCP within about
    10 ms, as T4 does for IPv4, and the excluded first hop stays
    reachable over IPv6.
  - T13: a first hop with only an IPv6 address inside a tunnelled IPv6
    subnet: ssh's connection is not captured.
  - T14: IPv6 switched off on the host: the tunnel starts with
    `--disable-ipv6` and IPv4 works.
- **VM** (new `test-vm/` scenarios): an IPv6 subnet through the tunnel
  and fail closed in a gap; a roam that changes only the IPv6 prefix
  restarts the tunnel when the first hop is reached over IPv6; a rotated
  temporary address does not.

## What was built

The design as above, with these differences and details:

- **A profile with only IPv6 networks on a host with IPv6 off** fails at
  `Connect` ("IPv6 is off on this host, and the profile tunnels only IPv6
  networks"). sshuttle would have nothing to tunnel.
- **sshuttle gets no IPv6 at all when IPv6 is not tunnelled:** no IPv6
  subnet and no IPv6 `-x`, rather than letting it drop them with a
  warning. The guard still has them.
- **Who decides:** the plugin reads `/proc` at `Connect` and writes the
  answer into `tunnel.json` (`"ipv6": true`), so the health check and
  `exec-tunnel` agree. A spec without the key (from an older plugin) runs
  with `--disable-ipv6`, and one whose `first_hop` is a string still works.
- **The health check** expects an IPv4 table only when the profile has an
  IPv4 network or a DNS server, and an IPv6 table only when IPv6 is
  tunnelled.
- **The network key** always carries the IPv6 part; the supervisor
  compares it only while the tunnel uses IPv6. A first hop whose name has
  a link-local address keeps it without the scope.
- **The CLI** also sets `ipv4.auto-route-ext-gw=no` and both
  `never-default` settings, which §4.1 names and the CLI had lacked.
  `check` warns about a profile without `ipv6.auto-route-ext-gw=no` and
  prints the `nmcli` command that fixes it.
- **Tests:** lab T11 to T14 as above, with a route to the internal IPv6
  network through the jump host, so that a leak would arrive (T12d shows
  it does without the guard). VM scenario V6-01 is an IPv6 subnet through
  the tunnel and fail closed in a gap; V6-02 changes the uplink's IPv6
  addresses on NM's primary connection: a second address in the prefix is
  no change, a new prefix restarts a tunnel whose first hop is IPv6, and
  not an IPv4 one.

## Open questions

- Whether resolved, NM or applications behave differently when a split
  domain answers AAAA records inside a tunnelled IPv6 network: the lab's
  DNS server answers no AAAA today (§4.5), so the lab needs AAAA records
  for these tests.
- Whether to add, for both families, routes for the subnets on `nmss0`, so
  that the tunnelled networks work on an uplink without a default route
  (M2-24) or without IPv6. The redirect would then still happen in the
  output hook, but `Ip4Config` would carry routes for the first time,
  which changes what NM does at every reconnect. That needs its own spike
  run and is not part of this design.
