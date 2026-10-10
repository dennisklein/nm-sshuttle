# Upstream reports (drafts)

Bugs found while building nm-sshuttle, written up for the projects that own
them. **None of them has been filed.** Each draft was written from the spike
and lab evidence and then fact-checked against the upstream sources by a
second, skeptical pass. Each one says what was observed, what was only read
in the source, and what was never run.

| Draft | Project and tracker | Our workaround | Before filing |
|---|---|---|---|
| [NetworkManager: SIGSEGV when a plugin recreates its tundev](networkmanager-vpn-tundev-crash.md) | NetworkManager, [gitlab.freedesktop.org](https://gitlab.freedesktop.org/NetworkManager/NetworkManager/-/issues); also Fedora's NetworkManager-1.56.1-2.fc44 | Never recreate `nmss0` while NM holds the VPN (waiting for NM's device avoided the crash in a later run, but the VPN still lost its DNS) | Search for duplicates (`_check_complete`, `check_device_added_idle_source`, RHEL-125796, MR !2347). Run the held reproducer once on a Fedora 44 VM with the packaged NM; so far it ran only in a sandbox. A third Fedora run re-created the link after waiting for NM's device and did not crash (see the draft's workaround). |
| [NetworkManager: a reconnect with unchanged config stays "activating"](networkmanager-vpn-reconnect-stuck.md) | NetworkManager, [gitlab.freedesktop.org](https://gitlab.freedesktop.org/NetworkManager/NetworkManager/-/issues) | The `nbns` sentinel (design §4.4) | Search for duplicates. The draft now has 1.56.1 traces of the early activation (reapply and link deletion) and says that without firewalld the sentinel commit is a race (runs 5 and 6). Optionally run the reproducer on 1.56. |
| [GNOME Shell: VPN secrets request unanswered without an auth dialog](gnome-shell-vpn-auth-dialog.md) | GNOME Shell, [gitlab.gnome.org](https://gitlab.gnome.org/GNOME/gnome-shell/-/issues) | Ship an auth-dialog stub (design §4.1) | Search for duplicates. "Actual result" now has the Fedora 44 user journal from the third spike run (R5f) and the stub control (R5d). |
| [sshuttle: three bugs](sshuttle.md): the server dies when `connect()` to the remote resolver fails; no `SO_REUSEADDR` on the listener; automatic exclusion ignores `-e` | sshuttle, [GitHub](https://github.com/sshuttle/sshuttle/issues) | Always pass `--to-ns` and `-x FIRST_HOP` (design §2.2, §4.5) | Check #1069, #89 and #275, which may be the same bugs, and comment there instead if they match. |

**Drafted from run 4, confirmed in runs 5 and 6:** [NetworkManager: VPN DNS left behind or lost](networkmanager-vpn-dns.md). A VPN that fails from "connect" keeps its DNS entry; a relinked tundev gets no DNS; `DnsManager.Configuration` is a stale snapshot; and a tundev's external device entry can take resolved's default route. Spike run 5 confirmed issue 1 with a rebuilt configuration, with systemd-resolved still serving the leaked server on the tundev (R4k-k). It added the plugin-kill path (R4m) and post-`Disconnect` evidence for issue 2 (R4f), and showed that an unmanaged tundev avoids issue 4 (R8c). Run 6 repeated these results, showed the kill leak with the tundev unmanaged (R8m), and added a second unmanaged control for issue 4, in which NetworkManager committed the sentinel (R8j). Before filing: search for duplicates, and run the draft's fake-plugin reproducer once (written, not yet run).

The drafts refer to the lab (`lab/run.sh`) and the spike by their names in
this repository. If the repository is not public when a report is filed,
leave those references out; none of the reproducers needs them.
