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
  for the tunnel's DNS (`dns split` or `all`).
- SSH access to a host on the far side that has Python 3. sshuttle needs
  nothing else there and no root.
- A key that logs you in without a password prompt from ssh itself: a key
  in your SSH agent (GNOME's agent unlocks it with your login), or a key
  without a passphrase. The plugin runs your `ssh` without a terminal, so
  it cannot ask you for anything.

## Installing

The packages are built in CI (`rpm` and `deb` jobs; download them from a
run's artifacts) or from the repository:

```console
$ # Fedora
$ sudo dnf install ./nm-sshuttle-*.noarch.rpm
$ # Debian, Ubuntu
$ sudo apt install ./nm-sshuttle_*_all.deb
```

From source:

```console
$ meson setup build --prefix=/usr
$ sudo meson install -C build
$ sudo nm-sshuttle post-install
```

The packages run `post-install` themselves. It reloads NetworkManager's
configuration so that the tunnel's link, `nmss0`, stays unmanaged. If an
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
| `--remote` | The ssh destination, as you would type it after `ssh`; usually a `Host` alias. Required. |
| `--subnets` | The IPv4 networks to send through the tunnel, separated by commas. Required. |
| `--exclude` | Networks inside those to leave out. |
| `--dns` | `none` (the default) leaves DNS alone. `split` sends lookups for `--dns-domains` to `--dns-servers` through the tunnel, and everything else as before. `all` sends every lookup through the tunnel. |
| `--dns-servers` | The DNS servers on the far side. Required for `split` and `all`. |
| `--dns-domains` | The domains `split` sends there, subdomains included. `~corp.example` only routes lookups. A plain `corp.example` also completes short names, so `git` becomes `git.corp.example`. Quote a leading `~` in the shell. |
| `--gateway` | The address ssh connects to first, when the plugin cannot work it out (`ProxyCommand`). |
| `--probe` | `ADDRESS:PORT` of a server on the far side that speaks first, such as an ssh server. The plugin connects to it every 10 s and restarts the tunnel after two failures in a row. Without it, a tunnel that is up but stuck is noticed only when sshuttle exits. |
| `--method` | `nft` (the default) or `nat`. |
| `--fail-closed` | `yes` (the default) blocks traffic to the tunnelled networks while the tunnel is down. `no` lets it go out on the local network. |
| `--user` | Whose ssh configuration and agent to use. The default is you. |

`nm-sshuttle list` shows your profiles, and `nm-sshuttle remove corp` deletes
one. To change a profile, remove it and add it again.

The profile is visible only to you. Other users neither see it nor can use
it: it uses your ssh identity.

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

## Troubleshooting

GNOME shows the same "Connection failed" whatever went wrong. The reason
is in the journal:

```console
$ journalctl -b -u nm-sshuttle -u nm-sshuttle-tunnel
```

Common causes:

- **Permission denied, or a host key question:** run
  `nm-sshuttle check corp` and fix what it reports.
- **"connection.permissions must be exactly user:…" or "ipv4.method must
  be auto":** GNOME Settings changed the profile. "Make available to other
  users" on the Details tab and the IPv4 tab both break it. Remove the
  profile and add it again.
- **Names on the far side do not resolve:** check `resolvectl status
  nmss0`. It should list the `--dns-servers` and the domains. Short names
  are completed only with domains given without a leading `~`.
- **The journal says `nmss0` is managed:** the setting in
  `/usr/lib/NetworkManager/conf.d/90-nm-sshuttle.conf` is overridden or
  not loaded. Switch the VPN off and run `sudo nm-sshuttle post-install`.
- **Something is left over after a crash** (an `nmss0` link, or blocked
  traffic to the tunnelled networks): with the VPN off,
  `sudo nm-sshuttle cleanup` removes it. The plugin also cleans up when it
  next starts.

## Limits

- One tunnel at a time.
- IPv4 only. TCP and DNS go through the tunnel; other UDP traffic, such as
  QUIC, does not.
- The tunnelled networks need a route on your machine; a default route is
  enough. On a network without one, the tunnel comes up but connections to
  those networks fail.
- GNOME Settings has no editor for these profiles yet. Use the commands
  above.
