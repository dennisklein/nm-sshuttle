# VM tests for the installed plugin

These tests drive the real plugin (`nm_sshuttle/`, installed with meson) in a
throwaway Fedora 44 VM, with SELinux enforcing, NetworkManager, systemd-resolved
and firewalld. They are the M2 tests of `docs/design.md` §4.10. `spike/` holds
the spike they grew from; nothing here changes it.

## What it checks

A jump host (sshd), a DNS server and a web server run in network namespaces on
the VM. A gap is made by letting the jump host refuse connections and killing
the tunnel unit; the plugin's own retries (1, 2, 4 ... s) bring it back once the
block is lifted. Each scenario has an id `M2-NN`; its checks are `M2-NNx`.

| id | scenario |
|---|---|
| M2-01 | install: files, SELinux labels, `90-nm-sshuttle.conf` takes effect after `nmcli general reload conf` |
| M2-02 | connect: address, resolved per link, split and outside lookups, guard, units, firewalld zone, nmss0 unmanaged |
| M2-03 | reconnect (nbns) three times with firewalld: same ifindex, DNS, one VPN entry in NM, fail closed in the gap |
| M2-04 | the same without firewalld |
| M2-05 | a gap longer than 10 s |
| M2-06 | `nmcli device set nmss0 managed no` in a gap: the plugin bounces the early "activated" |
| M2-07 | `ip link del nmss0` in a gap: give-up at once; then a profile in a non-default firewalld zone |
| M2-08 | `systemctl stop` and `restart` in a gap |
| M2-09 | `kill -9` in a gap: the new instance cleans up and exits at its idle timeout |
| M2-10 | switching off while a reconnect is pending |
| M2-11 | Connect right after Disconnect, within the idle window |
| M2-12 | a user override of the unmanaged setting: the plugin logs it |
| M2-13 | NetworkManager restart under an active VPN |
| M2-14 | `ExecStopPost` removes leftovers |
| M2-15 | no SELinux denials during the run |
| M2-16 | `kill -9` of sshuttle while up: a reconnect without a block |
| M2-17 | `nft flush ruleset` while up: the health check restores the guard and restarts the tunnel |
| M2-18 | `ip link del nmss0` while activated: give-up branch 4, no DNS left |
| M2-19 | `ip link del nmss0` right after the first `Ip4Config` (pre-up); the state hit is recorded |
| M2-20 | `nmcli device set nmss0 managed yes` in a gap |
| M2-21 | `systemctl stop` in a gap with the user override in place (a managed `nmss0`) |
| M2-22 | `nmcli general reload conf` under an active VPN, with and without the override |
| M2-23 | roaming: a second uplink becomes NM's primary connection and goes again; the plugin reconnects at once |
| M2-24 | the uplink without a default route: not offline, DNS after a reconnect; traffic to the subnets is INFO, since it has no route |
| M2-25 | lock screen: an authentication failure while locked waits for the unlock and retries once; unlocked it fails at once |
| M2-26 | the park-in-ip-config-get candidate (design §7): no early flip, and a kill in the gap drops NM's DNS entry |
| M2-27 | the probe: the sshuttle server on the jump host is stopped, the probe fails twice and the plugin reconnects |
| M2-28 | suspend and resume (only with `--suspend`) |

`PASS` and `FAIL` are asserted. `INFO` prints something the design marks as
unobserved (for example firewalld's zone binding after a link loss, or what
resolved holds between a kill and the link's removal): read those lines, they
are the answers. `SKIP` means the scenario cannot run here.

There are no manual GNOME checks; the VM is headless by default.

## Run it

On a Fedora 44 host with KVM:

    sudo dnf install qemu-kvm qemu-img xorriso openssh-clients curl gnupg2 virt-viewer
    test-vm/vm.sh

That downloads and checks the Fedora 44 Cloud image, creates and provisions the
VM (about as long as the spike's), installs meson and firewalld in it, copies
the repository in, runs `meson setup build --prefix=/usr`, `meson install -C
build` and `nm-sshuttle post-install`, and runs `test-vm/run.sh` as root. The
VM uses `~/.cache/nm-sshuttle-test-vm`, SSH port 2245 and VNC display 45, so a
spike VM can stay. The tests take about 12 minutes on a CI runner with KVM;
the kill scenario waits for the plugin's 60 s idle timer. CI runs them on
demand and weekly (the VM tests workflow), without M2-28.

After a change to the plugin, `test-vm/vm.sh run` copies, installs and tests
again. `test-vm/vm.sh ssh` opens a shell; `down` and `destroy` stop and delete
the VM.

## The report

`test-vm-results/<timestamp>/nm-sshuttle-test-report/` (also as a `.tar.gz`):

- `report.md`: one line per check, with the system under test.
- `details.md`: for every FAIL, the command's output and the state at that
  moment: resolved per link, NM's DNS entries (after `nmcli general reload
  dns-rc`), the connection, link, guard and unit state, and the plugin's
  journal tail. Start with the first FAIL; later ones are often its effects.
- `raw/`: journals, the plugin's D-Bus signals, command output.

The console shows the same, shortened. The exit status is 1 if any check failed.

## Re-run part of it

    test-vm/vm.sh run --only M2-05,M2-06

or inside the VM (`test-vm/vm.sh ssh`, then `cd /opt/nm-sshuttle-test`):

    sudo test-vm/run.sh --user tester --only M2-09 --keep

`--only` takes scenario ids (`M2-05`) or check ids (`M2-05a`, which runs its
scenario). Every scenario starts from a clean state and sets up its own
activation. `--keep` leaves the topology in place; `sudo test-vm/run.sh cleanup`
removes it. The plugin itself stays installed.

Suspend is opt-in, because a VM that does not wake from its RTC alarm ends the
run without a report:

    test-vm/vm.sh run --only M2-28 --suspend

## Limits

- The 10 s and 60 s escalation of design §4.4 starts after the reconnect burst,
  and only if NM does not show "activated". NM 1.56 does within milliseconds, so
  M2-05 checks a long gap and records whether the escalation ran; it cannot
  force it.
- Most gaps are made by killing the tunnel unit. M2-17 and M2-27 make the
  health check find them instead.
- The lock screen is simulated: the VM is headless, so M2-25 sets `LockedHint`
  on a logind session of the user as root, and makes the login fail by
  removing the user's key from the jump host. The agent's prompt behind a real
  GNOME lock screen is a manual check (below).
- M2-19's race between NM's "activated" and the link's removal is not
  controlled; the INFO line says which state the plugin gave up from.
- NetworkManager writing `/etc/resolv.conf` itself (`dns=default`, as on
  Debian) is not tested on Fedora; it belongs to a Debian or Ubuntu VM.

## Manual checks with GNOME

Provision with `test-vm/vm.sh up --gnome`, log in as `tester` on the VNC
display (`test-vm/vm.sh viewer`), and create the profile as in
`test-vm/lib.sh` (`create_profile`). Then:

1. The toggle in Quick Settings switches the VPN on and off.
2. With a passphrase key that is not loaded in the agent: switch on, type the
   passphrase in the prompt; the tunnel comes up.
3. Unload the key (`ssh-add -D`), lock the screen (Super+L), kill the tunnel (`sudo systemctl kill -s KILL
   nm-sshuttle-tunnel.service`), wait 30 s: the toggle shows "acquiring", and
   `journalctl -u nm-sshuttle` says it waits for the unlock. Unlock: the prompt
   appears, the tunnel comes back.
4. With automatic screen lock on, suspend (`systemctl suspend`) and resume:
   the tunnel comes back after the unlock.
