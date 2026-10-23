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
