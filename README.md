# nm-sshuttle

[![CI](https://github.com/dennisklein/nm-sshuttle/actions/workflows/ci.yml/badge.svg)](https://github.com/dennisklein/nm-sshuttle/actions/workflows/ci.yml)
[![VM tests](https://github.com/dennisklein/nm-sshuttle/actions/workflows/vm-tests.yml/badge.svg)](https://github.com/dennisklein/nm-sshuttle/actions/workflows/vm-tests.yml)

**An [sshuttle](https://github.com/sshuttle/sshuttle) tunnel as a first-class
VPN on the GNOME desktop.** A toggle in Quick Settings, next to WireGuard,
backed by a NetworkManager VPN plugin. It needs only SSH access and Python on
the far side, and it uses your own `ssh`, so it works through `ProxyJump`
hosts where WireGuard is not an option.

- **A real VPN toggle.** The tunnel shows up in GNOME Quick Settings and in
  `nmcli`, like any other NetworkManager VPN. Profiles can be created in
  GNOME Settings or with the `nm-sshuttle` command.
- **It stays up.** Once switched on, it reconnects by itself after suspend,
  roaming, a captive portal, a dropped connection or a locked screen, with
  backoff, and it notices a tunnel that is up but stuck.
- **It fails closed.** While the tunnel is down, traffic to the tunnelled
  networks is blocked instead of leaking onto the local network.
- **Your SSH setup, unchanged.** `~/.ssh/config`, `ProxyJump`, `known_hosts`
  and your agent work as usual. Only `ssh` runs as you; sshuttle runs as
  root in its own systemd unit, and no sudoers rule is needed.
- **DNS that follows the tunnel.** Send lookups for your company's domains
  to its resolvers (split DNS through systemd-resolved), or all of them.
- **IPv4 and IPv6, TCP and UDP.** `method=tproxy` carries UDP such as QUIC
  too.
- **Several tunnels at once.** Up to 8 profiles can be active together;
  profiles that would fight over the same traffic or DNS are refused.

## Install

Fedora 43 and 44 can install from the signed package repository, which also
brings updates:

```console
$ sudo rpm --import https://dennisklein.github.io/nm-sshuttle/RPM-GPG-KEY-nm-sshuttle
$ sudo dnf install https://dennisklein.github.io/nm-sshuttle/nm-sshuttle-release.noarch.rpm
$ sudo dnf install nm-sshuttle nm-sshuttle-gnome
```

`nm-sshuttle-gnome` adds sshuttle to GNOME Settings' VPN list; leave it out
to use the command line only. Restart Settings afterwards (log out, or
`pkill gnome-control-center`).

Debian 13 and Ubuntu 26.04 packages are built by CI (download them from a
run's artifacts), and the plugin installs from source with
`meson setup build --prefix=/usr && sudo meson install -C build &&
sudo nm-sshuttle post-install`. The
[user guide](docs/user-guide.md#installing) has the details.

You need NetworkManager 1.42 or later, nftables, sshuttle 1.3.1 or later and,
for the tunnel's DNS, systemd-resolved. The package pulls in the rest.

## Quick start

1. Put the way to your far side in `~/.ssh/config`, and make sure a key logs
   you in without a prompt from ssh itself (a key in your agent, or a key
   without a passphrase):

   ```
   Host corp
       HostName gw.corp.example
       User alice
       ProxyJump bastion.corp.example
   ```

2. Create a profile as yourself, and test it. `check` makes the login the
   plugin will make, so it may ask you to accept a host key once:

   ```console
   $ nm-sshuttle add corp --remote corp --subnets 10.0.0.0/8 \
         --dns split --dns-servers 10.1.0.53 --dns-domains '~corp.example'
   $ nm-sshuttle check corp
   ```

   With `nm-sshuttle-gnome` installed, the same form is in GNOME Settings:
   Network, **+** next to VPN, **sshuttle**.

3. Switch it on in Quick Settings, or:

   ```console
   $ nmcli connection up corp
   ```

   It stays on until you switch it off. If it does not come up, GNOME says
   only "Connection failed"; the reason is in
   `journalctl -b -u 'nm-sshuttle*'`.

## Common setups

Each of these is one `nm-sshuttle add`; the
[user guide](docs/user-guide.md#creating-a-profile) lists every option.

**Split DNS for a company domain.** Tunnel the network, send lookups for the
domain to its resolver, leave everything else alone. The leading `~` only
routes lookups; a plain `corp.example` also completes short names, so `git`
becomes `git.corp.example`:

```console
$ nm-sshuttle add corp --remote corp --subnets 10.0.0.0/8 \
      --dns split --dns-servers 10.1.0.53 --dns-domains corp.example
```

**IPv4 and IPv6, UDP included.** `--subnets` takes both families. Add
`--method tproxy` to carry UDP, such as QUIC; it needs `iptables` installed:

```console
$ nm-sshuttle add corp --remote corp --subnets 10.0.0.0/8,2001:db8:10::/48 \
      --method tproxy --dns split --dns-servers 10.1.0.53 --dns-domains '~corp.example'
```

**A second tunnel next to the first.** Exclude the nested network from the
larger profile and give it a profile of its own:

```console
$ nm-sshuttle add corp --remote corp --subnets 10.0.0.0/8 --exclude 10.50.0.0/16
$ nm-sshuttle add lab  --remote lab  --subnets 10.50.0.0/16
```

**A jump host behind `ProxyCommand`.** ssh cannot tell which address it
connects to first, so name it, and the tunnel will not swallow it:
`--gateway 203.0.113.7`.

**Notice a stuck tunnel.** `--probe 10.1.0.10:22` makes the plugin connect
to a server on the far side that speaks first, such as an ssh server, every
10 seconds, and restart the tunnel after two failures.

**Let traffic out while the tunnel is down.** `--fail-closed no` turns the
block off.

## How it works

```
 Quick Settings / nmcli ──► NetworkManager ──► nm-sshuttle (VPN plugin, root)
                                                   │ guard · DNS link · health
                                                   ▼
                                      nm-sshuttle-tunnel@N.service
                                         sshuttle (root) ──► ssh (as you) ──► far side
```

The plugin is a small Python service that tells NetworkManager the VPN is
persistent, supervises the sshuttle unit, and watches NetworkManager, logind
and the tunnel itself to decide when to reconnect. An nftables guard table
keeps the tunnelled networks fail-closed, and a per-tunnel dummy link
(`nmss0`, `nmss1` …) hands the VPN's DNS servers and routing domains to
systemd-resolved. The [design](docs/design.md) has the research behind it,
the options considered and the architecture.

## Documentation

- [User guide](docs/user-guide.md): installing, SSH setup, every profile
  option, GNOME Settings, several VPNs at once, troubleshooting, limits.
- [Design](docs/design.md): findings, architecture, risks and roadmap.
  The [designs of the later additions](docs/later/README.md) (IPv6, tproxy,
  several tunnels, the GNOME editor) are next to it.
- [Packaging](packaging/README.md): the RPM and deb packages, the signed dnf
  repository and how to cut a release.
- [Upstream reports](docs/upstream/README.md): drafts of bugs found along
  the way in NetworkManager, GNOME Shell and sshuttle.

## Development

```console
$ python3 -m pytest                 # needs python3-gobject and dbus-daemon for the D-Bus tests
$ meson setup build --prefix=/usr
$ meson test -C build
$ test-vm/vm.sh                     # on a Fedora 44 host with KVM; see test-vm/README.md
```

- [`nm_sshuttle/`](nm_sshuttle) is the plugin and the `nm-sshuttle` command;
  [`data/`](data) holds the files it installs.
- [`gnome/`](gnome) is the editor for GNOME Settings (C, GTK4). It is built
  when `libnm` and GTK4 are installed with their development files
  (`-Dgnome-editor=enabled` requires them) and is tested under a virtual
  display: `xvfb-run -a meson test -C build --suite gnome-editor`.
- [`test-vm/`](test-vm) runs the plugin's scenarios in a throwaway Fedora 44
  VM with SELinux enforcing, from a Fedora host or in CI.
- [`lab/`](lab) checks the sshuttle and kernel behaviour the design relies
  on, in network namespaces. Run it as root in a throwaway VM or container.
- [`spike/`](spike) is the early spike that retired the design's open risks
  on Fedora 44 with GNOME.

Report security problems privately, as [SECURITY.md](SECURITY.md) describes.

## License

MIT, see [LICENSE](LICENSE).
