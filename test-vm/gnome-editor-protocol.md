# Protocol: the editor in GNOME Settings, on a Fedora 44 laptop

This closes the one open point of the editor ([PR #10], `docs/later/gnome-editor.md`):
nothing in CI runs inside GNOME Settings. About 40 minutes, on a real
GNOME session (Wayland or X11) with NetworkManager, systemd-resolved and an ssh
host you can tunnel through. It installs and removes packages; no other
system file is touched.

Record each result in the table at the end. A step that shows something other
than "Expect" is a finding, even if it looks harmless: write down what you saw.

[PR #10]: https://github.com/dennisklein/nm-sshuttle/pull/10

## 0. What you need

- An ssh alias that logs you in without a prompt (`ssh HOST true`), and an
  IPv4 network that is reachable only through it. Below: `HOST` and `NET`
  (for example `10.0.0.0/8`), and `INSIDE`, an address in `NET` that answers
  (ping, or a TCP port you can `curl`).
- Optionally a DNS server in `NET` and a domain it serves, for the DNS rows.
- A terminal. Run Settings from it for every step (`pkill gnome-control-center;
  gnome-control-center network`), because its warnings go to stderr.
- A working directory, here `~/src`. Packages are built from the PR branch so
  that removing them later is one `dnf remove`.

## 1. Build the packages

```console
$ sudo dnf install git rpm-build dnf5-plugins
$ git clone -b claude/project-thread-c5oyfl https://github.com/dennisklein/nm-sshuttle ~/src/nm-sshuttle
$ cd ~/src/nm-sshuttle
$ version=0.1.0~dev0
$ mkdir -p ~/rpmbuild/SOURCES
$ git archive --format=tar.gz --prefix="nm-sshuttle-$version/" \
      -o ~/rpmbuild/SOURCES/"nm-sshuttle-$version.tar.gz" HEAD
$ for spec in packaging/rpm/*.spec; do sudo dnf -y builddep "$spec"; rpmbuild -ba "$spec"; done
$ ls ~/rpmbuild/RPMS/*/
```

Expect two packages: `noarch/nm-sshuttle-…rpm` and
`x86_64/nm-sshuttle-gnome-…rpm`. The `%check` of the second one ran the form
test under a virtual display; if the build stops there, that is finding B1.

If `nm-sshuttle` is installed already, remove it first (`sudo dnf remove
nm-sshuttle`) so that the baseline is the same for everyone.

## 2. Baseline, without the editor

```console
$ sudo dnf install ~/rpmbuild/RPMS/noarch/nm-sshuttle-*.rpm
$ nm-sshuttle add t-cli --remote HOST --subnets NET
$ nm-sshuttle check t-cli
```

| # | Do | Expect |
|---|---|---|
| 2.1 | Settings, Network: look at the VPN list. | `t-cli` is listed, with a switch. |
| 2.2 | Press the gear of `t-cli`, open the Identity tab. | The tab opens at once and says it cannot load the VPN connection editor. |
| 2.3 | Press "+" next to VPN. | sshuttle is not offered. |

## 3. Install the editor

```console
$ sudo dnf install ~/rpmbuild/RPMS/x86_64/nm-sshuttle-gnome-*.rpm
$ rpm -ql nm-sshuttle-gnome
$ grep -A1 '^\[libnm\]' /usr/lib/NetworkManager/VPN/nm-sshuttle-service.name
$ pkill gnome-control-center
```

| # | Expect |
|---|---|
| 3.1 | `rpm -ql` lists two `.so` files in `/usr/lib64/NetworkManager/`, owned by root and not writable by others (libnm refuses anything else). |
| 3.2 | The `.name` file has `plugin=libnm-vpn-plugin-sshuttle.so`. |

## 4. Create a profile in Settings

Run `gnome-control-center network` from the terminal.

| # | Do | Expect |
|---|---|---|
| 4.1 | "+" next to VPN. | **sshuttle** is offered. |
| 4.2 | Choose it. Look at the form before typing. | Rows SSH destination, Networks, Exclude, DNS, DNS servers, DNS domains, an "Advanced" disclosure, Local user (your user name). Labels are right-aligned with no colon. DNS servers and DNS domains are greyed. The Add button is not available. |
| 4.3 | Open "Advanced". | First hop, Probe, Method (nft), "Block traffic while down" (on). Their labels are right-aligned with no colon too, in the same column as the rows above. |
| 4.4 | Type `-x` as SSH destination; `10.0.0.0/33` as Networks. | Both rows turn red; pointing at one shows a message. The Add button stays unavailable. |
| 4.5 | Fix them: `HOST` and `NET`. | Red goes away; the button becomes available. |
| 4.6 | Set DNS to "Only for the domains below" without servers. | The button becomes unavailable; the DNS servers row shows a message. Fill servers and domains (or set DNS back to "Leave unchanged"). |
| 4.7 | Look at the width of the form. | The entries are wide enough to read a 40-character value; nothing is cut off at the right edge of the list row. Take a screenshot. |
| 4.8 | Name it `t-gui` and add it. | It appears in the VPN list. |

```console
$ nmcli -f connection.permissions,connection.autoconnect,vpn.persistent,vpn.data,ipv4.method,ipv4.auto-route-ext-gw,ipv6.auto-route-ext-gw connection show t-gui
```

| # | Expect |
|---|---|
| 4.9 | `connection.permissions` is `user:YOU`; `connection.autoconnect` is `no`; `vpn.persistent` is `yes`; `ipv4.method` is `auto`; both `auto-route-ext-gw` are `no`. |
| 4.10 | `vpn.data` has `remote`, `local-user` (you), `subnets`, `dns`, `method=nft`, `fail-closed=yes`, and no empty keys. |

## 5. It connects

| # | Do | Expect |
|---|---|---|
| 5.1 | Switch `t-gui` on from Quick Settings, with no `nm-sshuttle check` for it. | It connects (the host key is in your `known_hosts` from step 2). |
| 5.2 | `ping -c1 INSIDE` or `curl` it. | It answers through the tunnel. |
| 5.3 | `sudo nft list tables` | Includes `nm-sshuttle-guard`. |
| 5.4 | Switch it off. | The VPN is off; the guard table is gone. |

## 6. Edit a profile made with the CLI

| # | Do | Expect |
|---|---|---|
| 6.1 | Gear of `t-cli`, Identity tab. | The form is there at once (no 25 s wait), filled with the CLI's values. |
| 6.2 | Add a key the form does not know: `nmcli connection modify t-cli +vpn.data from-a-newer-cli=x`. Reopen the Identity tab. Change Networks to `NET INSIDE/32`, Apply. | Apply works. |
| 6.3 | `nmcli -f vpn.data connection show t-cli` | Shows the new networks, space-separated, **and** `from-a-newer-cli = x`. |
| 6.4 | `nmcli connection modify t-cli +vpn.data method=tproxy`, reopen the tab. | Method shows "tproxy"; Apply is unavailable with a message about the method. Choose nat: Apply works. |

## 7. What Settings can still break

| # | Do | Expect |
|---|---|---|
| 7.1 | Details tab of `t-gui`: switch on "Make available to other users" (it may ask for authentication). | Apply becomes unavailable at once; its tooltip says the profile must stay private to you and what to switch off. |
| 7.2 | Switch it off again. | Apply is available again. |
| 7.3 | IPv4 tab: set Method to Manual. Then add an address (any, such as 192.0.2.5/24, gateway 192.0.2.1). | While the address is missing Apply is unavailable because of the IPv4 tab itself. Once it is complete Apply is unavailable too, and its hover text says the Identity tab accepts only "Automatic" for the IPv4 method. |
| 7.4 | Change anything else (the DNS row, say). | Apply stays unavailable, same message. |
| 7.5 | Set Method back to Automatic. | Apply becomes available by itself within a moment, and the hover text goes away (it needs no further click). |
| 7.6 | Switch on "Use this connection only for resources on its network" on the IPv4 and IPv6 tabs, and Apply. | Apply works. `nmcli -f ipv4.method,ipv4.never-default,ipv6.never-default connection show t-gui` says `auto`, `yes`, `yes`. |
| 7.7 | The text at the top of the Identity tab. | It tells the user to switch on that setting on the IPv4 and IPv6 tabs, and is easy to see. |
| 7.8 | Switch on the VPN after forcing `ipv4.method` to `manual` with `nmcli connection modify t-gui ipv4.method manual ipv4.addresses 192.0.2.5/24`. | It fails. `journalctl -b -u nm-sshuttle` names `ipv4.method`. Put it back with `nmcli connection modify t-gui ipv4.method auto ipv4.addresses ""`. |

## 8. Stability

| # | Do | Expect |
|---|---|---|
| 8.1 | Open and close the Identity tab of `t-gui` and `t-cli` ten times, and press Cancel and Apply in between. | No crash. `coredumpctl list gnome-control-center` shows nothing new. |
| 8.2 | Read the terminal Settings runs in. | No `Gtk-WARNING`, `GLib-CRITICAL` or `nm-sshuttle` messages from the editor. Copy any you see. (Settings' own "style-dark.css is deprecated" and "Failed to get secrets: No agents were available" lines are known and fine.) |
| 8.3 | Optional: `sudo dnf install nm-connection-editor`, run it, edit `t-gui`. | The VPN tab stays empty and the terminal says "nm-sshuttle has no GTK3 editor". `nm-connection-editor` then prints Gtk-CRITICAL lines and refuses to save; that is its way of handling a plugin without a GTK3 editor, and it must not crash. |

## 9. Remove the editor

```console
$ sudo dnf remove nm-sshuttle-gnome
$ pkill gnome-control-center; gnome-control-center network
```

| # | Expect |
|---|---|
| 9.1 | "+" next to VPN no longer offers sshuttle. |
| 9.2 | The gear of `t-cli` shows the same message as in 2.2, and nothing else. |
| 9.3 | The terminal shows at most "file \"libnm-vpn-plugin-sshuttle.so\" not found. Did you install the client package?"; no crash. |
| 9.4 | `t-gui` and `t-cli` still switch on from Quick Settings. |

## 10. Clean up

```console
$ nm-sshuttle remove t-gui; nm-sshuttle remove t-cli
$ sudo dnf remove nm-sshuttle      # if you did not have it before
```

## Results

Copy this table into the PR or the thread. Mark each row pass, fail or skipped.

| Step | Result | Notes (what you saw, if not "Expect") |
|---|---|---|
| 1 build / `%check` | | |
| 2.1 – 2.3 baseline | | |
| 3.1 – 3.2 install | | |
| 4.1 – 4.8 form | | |
| 4.9 – 4.10 nmcli | | |
| 5.1 – 5.4 connects | | |
| 6.1 – 6.4 CLI profile | | |
| 7.1 – 7.7 broken by Settings | | |
| 8.1 – 8.3 stability | | |
| 9.1 – 9.4 removed | | |

For a failure, also keep: a screenshot, the terminal output of Settings,
`journalctl -b -u nm-sshuttle -u NetworkManager --since "10 min ago"`,
`coredumpctl list gnome-control-center`, and
`nmcli -f all connection show t-gui`.

The two open questions of the design are decided here: 4.7 (is the list row
wide enough, the FIXME at `ce-page-vpn.c:107`) and 7.6–7.7 (is the hint for
`never-default` enough, or should the plugin require it).
