# GTK4 editor plugin for GNOME Settings

Status: design, not implemented. One of the "Later" items of the
[design](../design.md) (§7); the index is in [README.md](README.md).

v1 has no editor plugin (§1, option B in §3). Profiles come from `nm-sshuttle
add`; GNOME Settings lists them with a switch and a gear button, but its
Identity tab says "unable to load VPN connection editor", and "Add VPN"
does not offer sshuttle (§2.1, R5d). This design adds an editor, so that a
profile can be created and changed in Settings.

## Goals and non-goals

Goals:

- "Add VPN" in GNOME Settings offers "sshuttle", and the Identity tab
  edits every profile key of §4.2.
- A profile made in Settings passes the plugin's checks without a trip to
  the command line: the editor fills in what Settings does not.
- What the editor accepts and what the plugin accepts at `Connect` come
  from one set of test cases, so that the two cannot drift apart.
- No change to the plugin, the CLI or the auth-dialog stub.

Not in this step:

- **A GTK3 editor** for nm-connection-editor. The same source can later be
  built a second time against GTK3, as NetworkManager-openvpn does; the
  base plugin already picks the library by toolkit (below).
- **Import and export** of profiles from files.
- **KDE** (plasma-nm has its own Qt plugin API).
- **Translations.** The strings go through gettext so that they can be
  added later.

## What the research found

Sources: gnome-control-center `main` (67660ef), NetworkManager `main`
(1.59.2-dev, ef234f8) and NetworkManager-openvpn `main` (7fb52f4).

- **[src] Settings loads editors through libnm.** It lists only VPN types
  whose editor plugin loads (`vpn-helpers.c:55-75`), and caches that list
  for its process lifetime: a newly installed editor shows up after
  Settings restarts. The `.name` file's `[libnm] plugin=` names the
  library. A name without `/` is looked up in NetworkManager's VPN plugin
  directory (`nm-vpn-editor-plugin.c:250-256`), so the `.name` file can
  stay free of `/usr/lib64` versus `/usr/lib/x86_64-linux-gnu`. libnm
  refuses a library not owned by root or writable by others
  (`nm-utils.c:3528-3545`).
- **[src] The editor must be C.** libnm `dlopen`s the library and calls
  `nm_vpn_editor_plugin_factory()`, which returns a GObject implementing
  `NMVpnEditorPlugin`; its `get_editor()` returns an `NMVpnEditor` with
  `get_widget()`, `update_connection()` and a `changed` signal
  (`nm-vpn-editor-plugin.h`, `nm-vpn-editor.h`). PyGObject cannot provide
  that symbol, and §2.4 notes that libnm's VPN GIR annotations break
  subclassing from Python anyway.
- **[src] One base plugin, one editor library per toolkit.**
  NetworkManager-openvpn's base plugin links only libnm. In `get_editor()`
  it checks whether the process has a GTK3-only symbol
  (`gtk_container_add`) and loads `libnm-vpn-plugin-openvpn-editor.so`
  (GTK3) or `libnm-gtk4-vpn-plugin-openvpn-editor.so` (GTK4)
  (`nm-openvpn-editor-plugin.c:120-148`). Loading a GTK3 library into a
  GTK4 process would abort it.
- **[src] Settings sets two things for a new VPN profile:**
  `connection.autoconnect=no` and `connection.permissions=user:<you>`
  (`net-connection-editor.c:776-783`). It sets neither `vpn.persistent`
  nor `local-user`, which the plugin requires (§4.2).
- **[src] Every change re-validates every page in order**: Details, then
  the VPN page, then IPv4 and IPv6 (`net-connection-editor.c:455-486`,
  `:632-652`). The VPN page's validation is the editor's
  `update_connection()` on the connection being edited
  (`ce-page-vpn.c:140-155`). A page that fails disables Apply with the
  error as its tooltip. The editor may write any setting, but a later
  page overwrites what it shows: the IPv4 page writes `method`,
  addresses, DNS, routes and `never-default` on each validation
  (`ce-page-ip4.c:761-769`). It does not touch `auto-route-ext-gw`.
- **[src] The Details page's "Make available to other users"** clears the
  permissions or sets `user:<you>` the moment it is switched
  (`ce-page-details.c:185-195`), before the validation that follows.
- **[src] Settings removes the trailing colon from editor labels and
  right-aligns them** (`ce-page-vpn.c:62-88`), and puts the editor's
  widget into a list row (`:95-110`).

## Design

### Libraries and files

| File | Links | What it is |
|---|---|---|
| `libnm-vpn-plugin-sshuttle.so` | libnm | The base plugin: name "sshuttle", description, service type, no capabilities. `get_editor()` loads the GTK4 library, or fails with "nm-sshuttle has no GTK3 editor" in a GTK3 process. |
| `libnm-gtk4-vpn-plugin-sshuttle-editor.so` | libnm, GTK4 | The editor widget and `update_connection()`. |
| `nm-sshuttle-editor.ui` | | GtkBuilder XML, compiled in as a GResource. |

Both libraries go to NetworkManager's VPN plugin directory. The `.name`
file gains:

```ini
[libnm]
plugin=libnm-vpn-plugin-sshuttle.so
```

Without the libraries installed, Settings behaves as today: it logs that
the file was not found and shows "unable to load VPN connection editor".
So the key goes into the main package's `.name` file, and the libraries
into a separate package. NM itself ignores the `[libnm]` group, so the
edit to the `.name` file does not touch the daemon (and NM's habit of
ignoring edits to a loaded `.name` file does not matter here).

The editor loader is written for this project (about 40 lines over
`GModule`), rather than copied from NetworkManager's shared
`nm-vpn-plugin-utils.c`, which is LGPL; the project is MIT.

### The form

One column of rows, in the order a user fills them in:

| Row | Widget | Key |
|---|---|---|
| SSH destination | entry | `remote` |
| Networks | entry, commas or spaces | `subnets` |
| Exclude | entry | `exclude` |
| DNS | drop-down: none, split, all | `dns` |
| DNS servers | entry; sensitive unless "none" | `dns-servers` |
| DNS domains | entry; sensitive for "split" | `dns-domains` |
| Advanced (expander): first hop | entry, empty means "from ssh -G" | `gateway` |
| Advanced: probe | entry, `ADDRESS:PORT` | `probe` |
| Advanced: method | drop-down: nft, nat, and tproxy once it exists | `method` |
| Advanced: block traffic while down | switch, on by default | `fail-closed` |
| Local user | label | `local-user` |

`local-user` is not editable. A new profile gets the user running
Settings, and an existing one keeps its own: Settings shows a profile
only to the user in its permissions, so the two are the same.

Unknown `vpn.data` keys are kept as they are, so that a profile written
by a newer CLI survives an edit in an older editor.

Each row checks its own field as the user types and marks it with the
`error` style class. Hints sit in the rows' subtitles ("an alias from
~/.ssh/config, e.g. corp").

### What `update_connection()` writes and refuses

It writes:

- the `vpn.data` keys of the form, with lists space-separated as the CLI
  writes them, and empty optional keys removed;
- `vpn.persistent=yes`;
- `ipv4.auto-route-ext-gw=no` and `ipv6.auto-route-ext-gw=no`, which no
  Settings page touches;
- `connection.autoconnect=no`, which Settings sets already.

It refuses, with a message that becomes Apply's tooltip:

- any field the plugin would refuse (the rules of §4.2, below);
- `connection.permissions` other than exactly `user:<local-user>`
  ("nm-sshuttle profiles must stay private to alice: switch off "Make
  available to other users""). The Details page has written the
  permissions before the VPN page validates, so this holds on the same
  change.

What it cannot hold:

- **`ipv4.method`.** The IPv4 page writes it *after* the VPN page has
  validated. A change to "Manual" or "Disabled" therefore passes the
  editor's check on that change, and is caught only at the next one.
  Apply may stay sensitive for that one change. The plugin's own check at
  `Connect` and `nm-sshuttle check` (§4.2) still catch it, with a message
  that names the field.
- **`ipv4.never-default`.** The IPv4 page writes it from its "Use this
  connection only for resources on its network" switch, which is off for
  a new profile. The plugin does not depend on it: `split` needs domains,
  which keep resolved's default route off `nmss0`, and `all` sets `~.`
  on purpose (§2.1, §4.1). The editor's hint text tells the user to
  switch it on; `check` reports it as a warning.
- **Addresses, routes and DNS on the IPv4 and IPv6 pages.** Settings lets
  a user add them to any VPN, and the plugin does not check them today
  either. With a route there, NM would send that route's traffic into
  `nmss0`, a dummy link. A later change could have the plugin refuse profiles
  with such entries; it is outside this design, because it applies to
  CLI-made profiles too.

### Keeping the checks in step

The plugin validates in Python (`profile.parse`), the editor in C. Both
read one file of cases, `tests/profile-cases.json`: each case is a
`vpn.data` dictionary plus the connection fields the plugin checks, and
the expected outcome (`ok`, or the key that is wrong). pytest runs every
case through `profile.parse`; a small C test runs every case through the
editor's validation function, built without GTK. A rule added to one side
without the other fails one of the two test runs.

The C side does not look up users: whether `local-user` exists is
checked by the plugin only.

### Packaging

- **meson:** an option `gnome-editor` (`auto`, `enabled`, `disabled`),
  needing `libnm` ≥ 1.42 and `gtk4`. The Python parts do not change.
- **Debian:** a new binary package `nm-sshuttle-gnome` (`Architecture:
  any`, depends on `nm-sshuttle`), next to the existing `Architecture:
  all` package from the same source.
- **Fedora:** the RPM is `noarch`, and an RPM cannot add an
  architecture-specific subpackage to a `noarch` one. The editor gets its
  own spec, `packaging/rpm/nm-sshuttle-gnome.spec`, built from the same
  tarball with only `gnome-editor` enabled; the main spec turns the
  option off.
- **CI:** the `rpm` and `deb` jobs build the new packages; a job builds
  the editor and runs the C test.

## What changes in the code

- New `gnome/` directory: the base plugin (about 150 lines of C), the
  editor (about 450 lines of C), the `.ui` file, the GResource XML and a
  `meson.build`.
- `data/nm-sshuttle-service.name.in`: the `[libnm]` group.
- `tests/profile-cases.json` and a pytest that runs it; the C test.
- Packaging and CI as above.
- `README.md`, the user guide: creating a profile in Settings, and that
  `nm-sshuttle check` is still the way to accept the host key once.

This is the project's first compiled code, which §1 kept out of v1. It
runs only inside GNOME Settings, as the user, and is never loaded by
NetworkManager or the plugin.

## Tests

- **Unit:** the shared cases through both validators; the editor's
  `update_connection()` on a new connection (writes `persistent`,
  `auto-route-ext-gw`, `local-user`), on an existing one (keeps unknown
  keys), and with all-users permissions (refuses).
- **VM, by hand** (GNOME needs a display; added to the manual list in
  `test-vm/README.md`): "Add VPN" offers sshuttle; a profile made there
  connects without `nmcli`; switching on "Make available to other users"
  disables Apply with the message; changing the IPv4 method is caught by
  the plugin at `Connect`; the editor opens at once for a CLI-made
  profile (no 25 s wait, R5d); Settings shows no editor and no error
  besides today's when the editor package is not installed.

## Open questions

- Whether Settings' list row gives the editor enough width for the long
  entries, or the form needs a different layout (the "FIXME" at
  `ce-page-vpn.c:107`).
- Whether a hint in the editor is enough for `never-default`, or the
  plugin should start to require it for new profiles; that would reject
  profiles some users already have.
