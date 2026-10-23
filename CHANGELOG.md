# Changelog

All notable changes to nm-sshuttle are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
uses [semantic versioning](https://semver.org/).

## [Unreleased]

### Security

- A profile address or network with an IPv6 scope id (`fe80::%eth0/64`) is
  refused. The scope is free text that `ipaddress` keeps, and a crafted one in
  `subnets`, `exclude` or `gateway` reached the root `nft` script, where an
  `include` made root read a file in `/etc` and log its first lines. The guard's set
  elements are now written from each network's number, and a failed guard
  logs only nft's error line.
- The `remote` destination is checked as sshuttle splits it, in the plugin and
  in the GNOME editor: an empty user or host (`@`, `@corp`), a host that starts
  with `-` (`@-gD1080`), a password (`user:password@host`, which ended up in
  sshuttle's command line and the journal) and a bad port are refused.
- The sweep and the health check count only root's listeners on `127.0.0.1`
  or `::1` (or the wildcard address). Any user's listener on the port of a
  table that a killed sshuttle left behind kept the table, and with it the
  redirect of the tunnelled traffic and DNS to that user's socket, past the
  guard; such a table is now removed at the next sweep, with a warning
  that names the user. A user's listener still counts while sshuttle's
  root firewall helper runs below it, as for an sshuttle the user runs in
  the foreground; one run with `--daemon`, or a root one listening on a
  LAN address, now loses its tables at the plugin's sweeps. The health
  check no longer falls back to every listener when it cannot find the
  tunnel unit's cgroup; it now also finds it on hybrid and legacy cgroup
  hosts, and gives up once, instead of reconnecting every 10 s, where
  there is none. `check` warns about that.
- sshuttle listens on an IPv4 port from 225 to 240, which only root may
  bind: its slot's own two first, then a free one of the others. Any user
  could hold sshuttle's ports from 12300 down and stop every tunnel from
  starting. The IPv6 and DNS ports are still sshuttle's choice, so a user
  can still block those, or bind them in the moment between sshuttle's
  exit and the sweep: this is only partly fixed for profiles with DNS or
  IPv6. The plugin now says when sshuttle found no free DNS or IPv6 port.
  `check` warns when `ip_unprivileged_port_start` lets users bind the new
  ports.
- The plugin answers `VPN.Plugin` calls only from NetworkManager.
- A profile with `dns split` or `all` is refused unless NetworkManager uses
  systemd-resolved and `/etc/resolv.conf` sends every lookup to its stub;
  otherwise lookups fell back to the local network's resolver during
  reconnects. `check` reports it.
- A guard removed during a reconnect is put back within 10 s, and before
  each attempt; before, only the health check while up did.
- A claim counts only for the process with the start time it recorded,
  not any process that has the PID and `nm-sshuttle-service` in its command
  line. A claim from the previous version needs that process to run with
  every user ID 0.
- The first hop's addresses from a lookup on the local network no longer
  decide alone what bypasses the tunnel and the guard. One that lies in the
  profile's networks or DNS servers is ignored, with a warning, unless
  `exclude` covers it, and at most 8 are used; with none left the
  activation fails. A forged DNS answer could take any internal address
  out of the tunnel while the VPN showed connected. A jump host reached by
  name inside the tunnelled networks now needs `gateway` or `exclude`; this
  includes every profile that tunnels `0.0.0.0/0` or `::/0`, where a jump
  host with a changing address, such as a dynamic DNS name, no longer
  works. A `HostName` or remote that is an address is not looked up and
  works as before. While addresses are ignored, ssh is pinned to a kept
  one, and a failed start names the ignored ones. `check` reports it.
- sshuttle gets `127.0.0.1` as its remote, and the ssh bridge gives ssh
  the profile's remote. sshuttle 2.0.0 looked the remote up itself, as
  root and on the local network, also with `gateway` set, and excluded
  every address it got.
- The tunnel unit has `/etc` read-only, so sshuttle's root firewall helper
  cannot write `/etc/hosts`; nm-sshuttle never passes `--auto-hosts`.
  `cleanup` removes the lines a stopped sshuttle left in `/etc/hosts`.
- The systemd units have a sandbox (design §4.7). All three keep the kernel
  tunables and cgroups read-only, lock the personality, and refuse realtime
  scheduling and new setuid files. The tunnel unit adds a read-only
  `/usr` and `/etc` and a capability bounding set; the plugin unit and the
  dispatcher a read-only `/usr`, and the dispatcher also a read-only
  `/home`, a private `/tmp` and fewer socket families and namespaces.
- The user's `ssh -G` for the first hop, and anything their ssh config runs
  from it, no longer inherit the plugin unit's `OOMScoreAdjust=-500`: the
  bridge sets 0, which also resets the floor, before it drops to the user.

## [0.1.0] - 2026-10-11

The first release.

### Added

- NetworkManager VPN plugin that runs an sshuttle tunnel, so the tunnel is a
  toggle in GNOME Quick Settings and a connection in `nmcli`. sshuttle runs
  as root in its own systemd unit; only `ssh` runs as the user, with their
  own `~/.ssh/config`, `ProxyJump`, `known_hosts` and agent.
- Fail-closed guard: traffic to the tunnelled networks is blocked while the
  tunnel is down.
- Lifecycle handling: reconnect with backoff after suspend, roaming, a
  captive portal, a dropped connection or a locked screen; health checks,
  with an optional `probe`, notice a tunnel that is up but stuck; another
  VPN's activation triggers an immediate reconnect.
- Split DNS through systemd-resolved, or all lookups through the tunnel.
- IPv4 and IPv6 networks, exclusions and first hops.
- UDP through the tunnel with `method=tproxy`.
- Up to 8 tunnels at once, one plugin instance per activation; a profile that
  conflicts with an active one is refused.
- The `nm-sshuttle` command with `add`, `check`, `list` and `remove` to
  manage profiles as the user.
- GTK4 editor for GNOME Settings (`nm-sshuttle-gnome`), validating profiles
  with the same rules as the plugin.
- Packages: RPMs for Fedora 43 and 44 from a signed dnf repository on GitHub
  Pages (`nm-sshuttle-release` sets it up), and Debian 13 and Ubuntu 26.04
  packages built by CI.
- User guide and design documentation, a lab that checks the sshuttle and
  kernel behaviour the design relies on, and VM tests that run in CI.

### Known limits

- The tunnel's DNS needs systemd-resolved. Without it (NetworkManager
  writing `/etc/resolv.conf` itself), `dns split` sends every lookup to the
  tunnel's resolvers, and a resolver entry that NetworkManager fails to
  remove can stay until it restarts. The case is described in the design
  (section 4.5) but not tested on a VM.
- There is no Debian or Ubuntu package repository; take the `.deb` files
  from a CI run.

[Unreleased]: https://github.com/dennisklein/nm-sshuttle/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/dennisklein/nm-sshuttle/releases/tag/v0.1.0
