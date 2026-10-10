# SPDX-License-Identifier: MIT
"""nm-sshuttle: the commands the units and the plugin run.

  cleanup        stop the tunnel unit, remove nmss0, the guard and stale
                 sshuttle tables (ExecStopPost of nm-sshuttle.service, and
                 the plugin's startup cleanup)
  sweep          remove stale sshuttle tables (the tunnel unit's
                 ExecStartPre and ExecStopPost)
  exec-tunnel    ExecStart of the tunnel unit
  ssh-as-user    sshuttle's ssh command
  first-hop      print the first hop's IPv4 address, worked out as the user
  post-install   reload NetworkManager's configuration unless an nm-sshuttle
                 VPN is active

The profile commands (add, check, list, remove) come with M3.
"""

import argparse
import logging
import os
import subprocess
import sys

from . import guard, tunnel
from .const import GUARD_TABLE, LINK, SERVICE_TYPE, TUNNEL_SPEC, TUNNEL_UNIT

log = logging.getLogger("nm-sshuttle")


def systemctl(*args, timeout=30):
    try:
        return subprocess.run(["systemctl", "--no-pager", *args], capture_output=True,
                              text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(args, 255, "", str(e))


def cleanup():
    """Remove everything an activation leaves behind (design §4.4, §4.7).

    Safe at any time no activation runs in a live plugin: after a kill NM has
    already disconnected the VPN, because it does so whenever the plugin's
    bus name vanishes."""
    ok = True
    # Always stop it: "is-active" is false for a unit that is still starting,
    # and stop does nothing for an inactive one.
    p = systemctl("stop", TUNNEL_UNIT)
    if p.returncode:
        log.warning("cannot stop %s: %s", TUNNEL_UNIT, p.stderr.strip())
        ok = False
    if guard.link_ifindex() is not None:
        log.info("removing a leftover %s", LINK)
        ok &= guard.remove_link()
    if guard.guard_present():
        log.info("removing a leftover guard table %s", GUARD_TABLE)
        ok &= guard.remove_guard()
    tunnel.sweep()
    try:
        os.remove(TUNNEL_SPEC)
    except FileNotFoundError:
        pass
    except OSError as e:
        log.warning("cannot remove %s: %s", TUNNEL_SPEC, e)
    return 0 if ok else 1


def active_sshuttle_vpns(nmcli=None):
    """UUIDs of active connections that are nm-sshuttle VPNs."""
    def run(*args):
        p = subprocess.run(["nmcli", *args], capture_output=True, text=True, timeout=10)
        if p.returncode:
            raise RuntimeError(p.stderr.strip())
        return p.stdout
    nmcli = nmcli or run
    uuids = []
    for line in nmcli("-g", "UUID,TYPE", "connection", "show", "--active").splitlines():
        uuid, _, kind = line.partition(":")
        if kind == "vpn" and nmcli("-g", "vpn.service-type", "connection", "show",
                                   uuid).strip() == SERVICE_TYPE:
            uuids.append(uuid)
    return uuids


def post_install():
    """Apply 90-nm-sshuttle.conf now, unless that could disturb an active VPN
    (design §4.1, "Installing"). Otherwise it takes effect at NM's next start."""
    try:
        active = active_sshuttle_vpns()
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as e:
        print(f"nm-sshuttle: cannot ask NetworkManager ({e}); not reloading its "
              "configuration", file=sys.stderr)
        return 0
    if active:
        print("nm-sshuttle: a VPN is active; the unmanaged setting for nmss0 takes effect "
              "at NetworkManager's next start", file=sys.stderr)
        return 0
    p = subprocess.run(["nmcli", "general", "reload", "conf"], capture_output=True, text=True)
    if p.returncode:
        print(f"nm-sshuttle: nmcli general reload conf failed: {p.stderr.strip()}",
              file=sys.stderr)
    return 0


def first_hop(user, remote):
    try:
        print(tunnel.first_hop(user, remote))
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        print(f"first-hop: {e}", file=sys.stderr)
        return 1
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["ssh-as-user"]:
        # Everything after the user goes to ssh unchanged; argparse would eat options.
        if len(argv) < 2:
            print("usage: nm-sshuttle ssh-as-user USER [SSH-ARGS...]", file=sys.stderr)
            return 2
        tunnel.ssh_as_user(argv[1], argv[2:])
        return 255   # not reached: ssh_as_user execs
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    parser = argparse.ArgumentParser(prog="nm-sshuttle")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("cleanup")
    sub.add_parser("sweep")
    sub.add_parser("exec-tunnel")
    sub.add_parser("post-install")
    fh = sub.add_parser("first-hop")
    fh.add_argument("user")
    fh.add_argument("remote")
    args = parser.parse_args(argv)
    if args.cmd == "cleanup":
        return cleanup()
    if args.cmd == "sweep":
        tunnel.sweep()
        return 0
    if args.cmd == "exec-tunnel":
        return tunnel.exec_tunnel()
    if args.cmd == "post-install":
        return post_install()
    if args.cmd == "first-hop":
        return first_hop(args.user, args.remote)
    return 2
