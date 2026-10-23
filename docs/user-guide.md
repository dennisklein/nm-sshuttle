# User guide

nm-sshuttle runs an [sshuttle](https://github.com/sshuttle/sshuttle) tunnel
as a NetworkManager VPN connection. Once a profile exists, the tunnel is a
toggle in GNOME's Quick Settings, like any other VPN. It comes back by itself
after suspend, a change of network or a dropped connection, and while it is
down, traffic to the tunnelled networks is blocked instead of leaking onto
the local network.

How it works and why is in the [design](design.md); this guide covers using
it.

## What you need

- Fedora 43 or 44, Debian 13 or Ubuntu 26.04, with NetworkManager 1.42 or
  later, nftables and sshuttle 1.3.1 or later. systemd-resolved is needed
  for the tunnel's DNS (`dns split` or `all`), with `/etc/resolv.conf`
  linked to its stub: otherwise lookups would go to the local network's
  resolver whenever the tunnel is down, and such a profile is refused.
- SSH access to a host on the far side that has Python 3. sshuttle needs
  nothing else there and no root.
- A key that logs you in without a password prompt from ssh itself: a key
  in your SSH agent (GNOME's agent unlocks it with your login), or a key
  without a passphrase. The plugin runs your `ssh` without a terminal, so
  it cannot ask you for anything.

## Installing

On Fedora, install from the signed package repository, which also brings
updates:

```console
$ sudo rpm --import https://dennisklein.github.io/nm-sshuttle/RPM-GPG-KEY-nm-sshuttle
$ sudo dnf install https://dennisklein.github.io/nm-sshuttle/nm-sshuttle-release.noarch.rpm
$ sudo dnf install nm-sshuttle
```

The packages are also built in CI (`rpm` and `deb` jobs; download them from a
run's artifacts) or from the repository:

```console
$ # Fedora
$ sudo dnf install ./nm-sshuttle-*.noarch.rpm
$ # Debian, Ubuntu
$ sudo apt install ./nm-sshuttle_*_all.deb
```

To create and edit profiles in GNOME Settings as well, install the editor,
which is a separate package (`sudo dnf install nm-sshuttle-gnome` from the
repository; `nm-sshuttle-gnome-*.rpm`, `nm-sshuttle-gnome_*.deb` as files).
Settings reads its list of VPN types once, so restart it afterwards (log out,
or `pkill gnome-control-center`).

From source:

```console
$ meson setup build --prefix=/usr
$ sudo meson install -C build
$ sudo nm-sshuttle post-install
```

This builds the editor for GNOME Settings too when `libnm` and GTK4 are
installed with their development files (`-Dgnome-editor=disabled` leaves it
out, `=enabled` makes it an error to lack them).

The packages run `post-install` themselves. It reloads NetworkManager's
configuration so that the tunnels' links, `nmss0` to `nmss7`, stay unmanaged. If an
nm-sshuttle VPN is active at that moment, it skips the reload, and the
setting takes effect at NetworkManager's next start.

## Setting up SSH

Everything about the connection to the far side belongs in your
`~/.ssh/config`, which only your own `ssh` reads: the host name, the user,
the key, `ProxyJump`. nm-sshuttle takes no ssh options. For example:

```
Host corp
    HostName gw.corp.example
    User alice
    ProxyJump bastion.corp.example
```

The host key must already be in your `known_hosts`: `nm-sshuttle check`
below makes a test login that adds it.

With `ProxyCommand` instead of `ProxyJump`, ssh cannot tell which address it
connects to first. Give that address to `add` as `--gateway`, so that the
tunnel does not swallow it.

The plugin looks up the first host ssh connects to (the `ProxyJump` host,
or else the `HostName`) on the network you are on, and that answer can be
forged. So it ignores an address of that host that lies in your
`--subnets` or `--dns-servers`, and uses at most 8 of its addresses.
If your jump host does lie in the tunnelled networks, for example a
bastion at `10.0.0.5` with `--subnets 10.0.0.0/8`, or any jump host of a
profile that tunnels `0.0.0.0/0`, say so in the profile: give its address
as `--gateway 10.0.0.5`, or `--exclude 10.0.0.5`. A `HostName` (or
`--remote`) that is already an address, such as `HostName 10.0.0.5`, is
not looked up and needs neither. A jump host whose address changes, such
as a home server behind a dynamic DNS name, cannot be named that way, so
it does not work with a profile that tunnels `0.0.0.0/0` or `::/0`.

If the name has an outside address and also one inside your `--subnets`,
as with split-horizon DNS on the office network, ssh is pinned to the
outside one. If the office network cannot reach that, exclude the inside
address too: `--exclude 10.1.2.3`.

## Creating a profile

Run these as yourself, not as root:

```console
$ nm-sshuttle add corp --remote corp --subnets 10.0.0.0/8 \
      --dns split --dns-servers 10.1.0.53 --dns-domains '~corp.example'
added corp; try: nm-sshuttle check corp
$ nm-sshuttle check corp
ok   profile fields
ok   ssh -G; first hop 198.51.100.1
ok   host key and test login
```

`check` makes the same login the plugin will make, so it may ask you to
accept a host key or unlock a key once. Run it again after you change your
ssh configuration.

The options of `add`:

| Option | Meaning |
|---|---|
| `--remote` | The ssh destination, as you would type it after `ssh`; usually a `Host` alias. `user@host` works too; put a port in the alias (`Port` in `~/.ssh/config`). A password (`user:password@host`) is refused: use a key. Required. |
| `--subnets` | The networks to send through the tunnel, IPv4 or IPv6, separated by commas. Required. A scope id (`fe80::%eth0/64`) is refused, here and in the other address options. |
| `--exclude` | Networks inside those to leave out. A jump host inside the subnets must be excluded here, or given as `--gateway`. |
| `--dns` | `none` (the default) leaves DNS alone. `split` sends lookups for `--dns-domains` to `--dns-servers` through the tunnel, and everything else as before. `all` sends every lookup through the tunnel. |
| `--dns-servers` | The DNS servers on the far side, IPv4 only. Required for `split` and `all`. |
| `--dns-domains` | The domains `split` sends there, subdomains included. `~corp.example` only routes lookups. A plain `corp.example` also completes short names, so `git` becomes `git.corp.example`. Quote a leading `~` in the shell. |
| `--gateway` | The address ssh connects to first, when the plugin cannot work it out (`ProxyCommand`), or when it lies inside the subnets. Nothing is looked up then. |
| `--probe` | `ADDRESS:PORT` (or `[IPV6]:PORT`) of a server on the far side that speaks first, such as an ssh server. The plugin connects to it every 10 s and restarts the tunnel after two failures in a row. Without it, a tunnel that is up but stuck is noticed only when sshuttle exits. |
| `--method` | `nft` (the default) or `nat`. `tproxy` also sends UDP through the tunnel, such as QUIC or a DNS server you query directly; it needs `iptables` installed. |
| `--fail-closed` | `yes` (the default) blocks traffic to the tunnelled networks while the tunnel is down. `no` lets it go out on the local network. |
| `--user` | Whose ssh configuration and agent to use. The default is you. |

`nm-sshuttle list` shows your profiles, and `nm-sshuttle remove corp` deletes
one. To change a profile, edit it in GNOME Settings (below), or remove it and
add it again.

The profile is visible only to you. Other users neither see it nor can use
it: it uses your ssh identity.

## Creating and editing a profile in GNOME Settings

With the `nm-sshuttle-gnome` package installed, the editor does what
`nm-sshuttle add` does. In Settings, open Network, press **+** next to VPN
and choose **sshuttle**; or press the gear next to an existing profile and
open the **Identity** tab. The form has a row for each option of `add`:

| Row | Option |
|---|---|
| SSH destination | `--remote` |
| Networks, Exclude | `--subnets`, `--exclude`; separate several with spaces or commas |
| DNS, DNS servers, DNS domains | `--dns`, `--dns-servers`, `--dns-domains`; the servers and domains are greyed out when the mode does not use them |
| First hop, Probe, Method, Block traffic while down | `--gateway`, `--probe`, `--method`, `--fail-closed`, under **Advanced** |
| Local user | `--user`: you, for a new profile; not editable |

A row that is wrong turns red, and the message appears when you point at it.
Apply stays off until every row is right. The editor fills in what Settings
does not: the profile is persistent, never connects by itself, and does not
get the host route NetworkManager would add.

Two other tabs can still break a profile:

- **Details, "Make available to other users":** the editor refuses at once
  and says so; switch it off. A profile of nm-sshuttle belongs to one user.
- **IPv4:** the method must stay **Automatic**. The editor refuses a change
  to Manual or Disabled, a moment after you make it, and when you connect.
  Also switch on "Use this connection only for resources on its network"
  on the IPv4 and IPv6 tabs, so that the tunnel's DNS entry cannot become
  the default.

A profile made in Settings has not yet logged in from your account, so run
`nm-sshuttle check corp` once to accept the host key and test the login.

## Connecting

Switch it on in Quick Settings (the VPN entry), or:

```console
$ nmcli connection up corp
$ nmcli connection down corp
```

Once on, it stays on until you switch it off:

- **Drops:** when sshuttle exits or the health check fails, the plugin
  reconnects after 1, 2, 4 … up to 60 s, and at once after a change of
  network. The VPN stays switched on meanwhile.
- **Suspend:** the tunnel is stopped before the machine sleeps and comes
  back after resume.
- **No network:** while no connection is up, or behind a captive portal,
  it waits instead of trying (behind a portal, once a minute).
- **Locked screen:** if a reconnect fails because your agent cannot sign
  while the screen is locked, the plugin waits for the unlock and tries
  once more, with time for an unlock prompt.
- **Giving up:** after 10 minutes of failed attempts, not counting time
  offline, asleep or locked, the VPN fails and GNOME shows it as off.

## Several VPNs at once

Up to 8 nm-sshuttle profiles can be active together, each with its own
tunnel, link (`nmss0`, `nmss1` …) and guard. A profile is refused, and
GNOME says only "Connection failed", when it would fight an active one:

- its networks overlap the other's (after `--exclude`), in either family;
- both use `--dns all`;
- both list the same DNS domain (a domain and a subdomain of it are fine);
- the first hops would run in a loop: each profile's jump host inside the
  other's networks.

`nm-sshuttle check NAME` runs the same checks against the VPNs active at
that moment, and `nm-sshuttle list` marks the active ones.

To nest a smaller network inside a larger one, exclude it from the larger
profile:

```console
$ nm-sshuttle add corp --remote corp --subnets 10.0.0.0/8 --exclude 10.50.0.0/16 …
$ nm-sshuttle add lab --remote lab --subnets 10.50.0.0/16 …
```

While the lab VPN is off, traffic to 10.50.0.0/16 is not tunnelled and
not blocked. If the lab's jump host lies inside the corporate network, the
lab tunnel runs over the corporate one; when the corporate tunnel has a
gap the lab one reconnects too, at once after the corporate one is back.

## Troubleshooting

GNOME shows the same "Connection failed" whatever went wrong. The reason
is in the journal:

```console
$ journalctl -b -u 'nm-sshuttle*'
```

Common causes:

- **Permission denied, or a host key question:** run
  `nm-sshuttle check corp` and fix what it reports.
- **"connection.permissions must be exactly user:…" or "ipv4.method must
  be auto":** GNOME Settings changed the profile. "Make available to other
  users" on the Details tab and the IPv4 tab both break it. With the editor
  installed, set them back in Settings (permissions private, IPv4 on
  Automatic); without it, remove the profile and add it again.
- **Names on the far side do not resolve:** check `resolvectl status
  nmss0` (or the link of that VPN, `nmss1` and so on, when several are
  up). It should list the `--dns-servers` and the domains. Short names
  are completed only with domains given without a leading `~`.
- **The journal says `nmss0` is managed:** the setting in
  `/usr/lib/NetworkManager/conf.d/90-nm-sshuttle.conf` is overridden or
  not loaded. Switch the VPN off and run `sudo nm-sshuttle post-install`.
- **"dns=split needs NetworkManager to use systemd-resolved":** install
  and enable systemd-resolved, and let NetworkManager use it (its `dns`
  setting, or `/etc/resolv.conf` linked to resolved's stub). `check`
  reports it too.
- **"dns=split needs every lookup to go to systemd-resolved":**
  `/etc/resolv.conf` lists other servers than resolved's stub
  `127.0.0.53`, for example because it links to
  `/run/systemd/resolve/resolv.conf`, resolved's list of the real servers.
  Programs then ask those servers directly, and fall back to the local
  network's when the tunnel is down. Link it to the stub:
  `sudo ln -sf ../run/systemd/resolve/stub-resolv.conf /etc/resolv.conf`.
- **`check` warns about `ip_unprivileged_port_start`:** the tunnels listen
  on `127.0.0.1` ports 225 to 240, which only root may bind unless the
  sysctl `net.ipv4.ip_unprivileged_port_start` was lowered to 240 or
  below, as some container setups do. Then another user can hold the
  ports and stop the tunnel from starting. Nothing leaks: the guard stays.
- **"no free port for sshuttle's DNS redirector (9001-12300)"**, or for
  its IPv6 redirector: sshuttle still picks those ports itself, and any
  user can bind them. Find who holds them with `sudo ss -lunp` (DNS) or
  `sudo ss -ltnp` (IPv6) and stop that program.
- **`check` warns that no cgroup hierarchy holds systemd's units:** the
  plugin cannot find the tunnel unit's processes, so it gives up 10 s
  after connecting. This happens only with an unusual cgroup setup.
- **`check` warns about `ipv6.auto-route-ext-gw`:** the profile was made
  before IPv6 support. Run the `nmcli connection modify` command it
  prints.
- **"the first hop's name resolves only into the profile's networks or DNS
  servers":** your jump host's address lies in `--subnets` or
  `--dns-servers`. If that is right, set `--gateway` to it, exclude it, or
  give it as the `HostName` (see [Setting up SSH](#setting-up-ssh)). If not, the network you are on
  may be answering for the name; the journal names the addresses.
- **"… the first hop's name also resolves to …, inside the profile's
  networks or DNS servers":** the tunnel did not start, and the plugin
  had kept only some of the jump host's addresses, for example only its
  IPv6 one on an IPv4-only network, or its outside one on the office
  network. Exclude the address it names if that is your jump host.
- **"subnets overlap with the active profile …", "… also uses dns=all",
  "… also has the DNS domain …" or "the first hops would run in a
  loop":** another active VPN claims the same traffic or DNS. See
  [Several VPNs at once](#several-vpns-at-once).
- **Something is left over after a crash** (an `nmss0` link, or blocked
  traffic to the tunnelled networks): with the VPN off,
  `sudo nm-sshuttle cleanup` removes it. The plugin also cleans up when it
  next starts.

## Limits

- Up to 8 tunnels at a time, and only ones that do not conflict (see
  [Several VPNs at once](#several-vpns-at-once)).
- TCP and DNS go through the tunnel; other UDP traffic, such as QUIC,
  does so only with `--method tproxy`. A UDP flow that is silent for 30 s
  starts afresh. `ping` never goes through: it is blocked.
- DNS servers on the far side must have IPv4 addresses. If a split domain
  answers with IPv6 addresses, list those networks in `--subnets` too, or
  connections to them go around the tunnel.
- On a machine with IPv6 switched off, IPv6 networks are blocked but not
  tunnelled.
- The tunnelled networks need a route on your machine; a default route is
  enough. On a network without one, the tunnel comes up but connections to
  those networks fail.
- An sshuttle you run yourself next to these tunnels keeps its firewall
  rules when it runs in the foreground, as root or as a user (whose
  firewall helper runs through sudo), and listens on `127.0.0.1`, `::1`
  or the wildcard address, as it does by default. Run with `--daemon` as
  a user, or listening on another address (`-l 192.168.1.5:0`), it loses
  them whenever one of these tunnels starts or stops, and its traffic
  then goes around it. The journal then has a warning from the sweep.
- GNOME Settings can edit these profiles only with the `nm-sshuttle-gnome`
  package. `nm-connection-editor` (GTK3) has no editor yet; use the commands
  above there.
