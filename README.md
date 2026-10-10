# nm-sshuttle

Make an [sshuttle](https://github.com/sshuttle/sshuttle) tunnel behave like a
first-class VPN connection on a GNOME desktop: a toggle in Quick Settings,
next to WireGuard, backed by a systemd unit. The tunnel should survive
suspend, roaming and SSH key unlock without manual work.

sshuttle needs only SSH access and Python on the remote side, and it uses the
system `ssh` client, so it works through ProxyJump hosts where WireGuard is
not an option.

## Status

**M2, lifecycle, in progress.** The plugin runs its lifecycle and passes the
first VM run on Fedora 44 (`test-vm/`, 226 checks). There is no profile CLI or
distribution package yet.

- [`nm_sshuttle/`](nm_sshuttle) is the plugin (design §4.9): the VPN D-Bus
  service, its lifecycle state machine, the guard table and `nmss0`, and the
  commands the systemd units run. [`data/`](data) holds the files it
  installs, among them the NetworkManager `conf.d` snippet that keeps
  `nmss0` unmanaged.
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

## Building and testing

```console
$ python3 -m pytest                 # needs python3-gobject and dbus-daemon for the D-Bus test
$ meson setup build --prefix=/usr
$ sudo meson install -C build
$ sudo /usr/libexec/nm-sshuttle/nm-sshuttle post-install
```

`post-install` reloads NetworkManager's configuration so that `nmss0` is
unmanaged, unless an nm-sshuttle VPN is active; then the setting takes
effect at NetworkManager's next start. Until M3's CLI exists, a profile is
created with `nmcli` as in [`spike/spike.sh`](spike/spike.sh), plus
`vpn.persistent yes` (design §4.2).

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
