# nm-sshuttle

Make an [sshuttle](https://github.com/sshuttle/sshuttle) tunnel behave like a
first-class VPN connection on a GNOME desktop: a toggle in Quick Settings,
next to WireGuard, backed by a systemd unit. The tunnel should survive
suspend, roaming and SSH key unlock without manual work.

sshuttle needs only SSH access and Python on the remote side, and it uses the
system `ssh` client, so it works through ProxyJump hosts where WireGuard is
not an option.

## Status

**Design phase.** Nothing installable yet.

- [`docs/design.md`](docs/design.md) is the design proposal. It covers the
  research findings, the options considered, the proposed architecture, and
  the risks to retire first.
- [`lab/`](lab) is a self-contained test lab. It checks the sshuttle and
  kernel behaviour the design relies on (network namespaces; run as root in a
  throwaway VM or container).
- [`spike/`](spike) is the M1 spike. `spike/vm.sh` on a Fedora 44 host
  creates a throwaway Fedora 44 VM with GNOME and runs a bare VPN plugin
  through the open risks (NetworkManager activation, SELinux, split DNS,
  reconnects, the GNOME toggle, agent key unlock), then copies a report
  back.
- [`docs/upstream/`](docs/upstream) holds draft bug reports for
  NetworkManager, GNOME Shell and sshuttle, found along the way and not yet
  filed.

## Proposed shape, in short

- A thin NetworkManager VPN service plugin (`vpn-type sshuttle`), written in
  Python, that runs as a D-Bus-activated systemd service.
- sshuttle runs in its own systemd unit as root. Only `ssh` runs as the
  desktop user, so `~/.ssh/config`, ProxyJump, `known_hosts` and the
  session's agent work as usual, and no sudoers rule is needed.
- The plugin is persistent: once switched on, it reconnects by itself after
  suspend, roaming and drops. While the tunnel is down, traffic to the
  tunnelled networks fails closed instead of leaking.
- Split DNS uses a per-tunnel dummy link handed to systemd-resolved.

## License

MIT, see [LICENSE](LICENSE).
