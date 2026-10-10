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
spike VM can stay. A full run takes about 20 minutes; the kill scenario waits
for the plugin's 60 s idle timer.

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

## Limits

- The 10 s and 60 s escalation of design §4.4 starts after the reconnect burst,
  and only if NM does not show "activated". NM 1.56 does within milliseconds, so
  M2-05 checks a long gap and records whether the escalation ran; it cannot
  force it.
- The plugin has no health probe yet: it notices a drop when the tunnel unit
  stops, so the gap is made by killing the unit.
