# nm-sshuttle

Make an [sshuttle](https://github.com/sshuttle/sshuttle) tunnel behave like a
first-class VPN connection on a GNOME desktop: a toggle in Quick Settings,
next to WireGuard, backed by a systemd unit. The tunnel should survive
suspend, roaming and SSH key unlock without manual work.

sshuttle needs only SSH access and Python on the remote side, and it uses the
system `ssh` client, so it works through ProxyJump hosts where WireGuard is
not an option.

## Status

**M3, usable, in progress.** The plugin runs its whole lifecycle (M2):
reconnects with backoff, suspend, roaming, captive portals, a health check,
the lock screen and the fail-closed guard. The profile commands and RPM and
deb packages exist. The [user guide](docs/user-guide.md) covers installing,
creating a profile and troubleshooting.

```console
$ nm-sshuttle add corp --remote corp --subnets 10.0.0.0/8 \
      --dns split --dns-servers 10.1.0.53 --dns-domains corp.example
$ nm-sshuttle check corp
$ nmcli connection up corp         # or the VPN toggle in Quick Settings
```

- [`nm_sshuttle/`](nm_sshuttle) is the plugin (design §4.9): the VPN D-Bus
  service, its lifecycle state machine, the NetworkManager and logind
  watchers, the guard table and `nmss0`, and the commands the systemd units
  and users run. [`data/`](data) holds the files it installs, among them
  the NetworkManager `conf.d` snippet that keeps `nmss0` unmanaged.
- [`packaging/rpm/`](packaging/rpm) and [`debian/`](debian) build the
  Fedora and Debian/Ubuntu packages.
- [`docs/design.md`](docs/design.md) is the design: the research findings,
  the options considered, the architecture, the risks and the roadmap.
- [`test-vm/`](test-vm) runs the plugin's scenarios in a throwaway Fedora
  44 VM, from a Fedora host or in CI (the VM tests workflow).
- [`lab/`](lab) is a self-contained test lab. It checks the sshuttle and
  kernel behaviour the design relies on (network namespaces; run as root in a
  throwaway VM or container).
- [`spike/`](spike) is the M1 spike: a bare VPN plugin run through the open
  risks in a Fedora 44 VM with GNOME.
- [`docs/upstream/`](docs/upstream) holds draft bug reports for
  NetworkManager, GNOME Shell and sshuttle, found along the way and not yet
  filed.

## Building and testing

```console
$ python3 -m pytest                 # needs python3-gobject and dbus-daemon for the D-Bus test
$ meson setup build --prefix=/usr
$ sudo meson install -C build
$ sudo nm-sshuttle post-install
$ test-vm/vm.sh                     # on a Fedora 44 host with KVM; see test-vm/README.md
```

`post-install` reloads NetworkManager's configuration so that `nmss0` is
unmanaged, unless an nm-sshuttle VPN is active; then the setting takes
effect at NetworkManager's next start. The packages run it themselves.

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
