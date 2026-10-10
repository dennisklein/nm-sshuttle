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

  add, check, list, remove
                 manage profiles (design §4.2); they wrap nmcli
"""

import argparse
import logging
import os
import pwd
import re
import subprocess
import sys

from . import guard, profile, tunnel
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


# --------------------------------------------------------------- profiles
def nmcli_run(*args):
    p = subprocess.run(["nmcli", *args], capture_output=True, text=True, timeout=30)
    if p.returncode:
        raise RuntimeError(p.stderr.strip() or f"nmcli {args[0]} failed")
    return p.stdout


def profile_connections(nmcli=nmcli_run):
    """(uuid, id) of every nm-sshuttle profile."""
    found = []
    for line in nmcli("-g", "UUID,TYPE", "connection", "show").splitlines():
        uuid, _, kind = line.partition(":")
        if kind != "vpn":
            continue
        if nmcli("-g", "vpn.service-type", "connection", "show", uuid).strip() == SERVICE_TYPE:
            found.append((uuid, nmcli("-g", "connection.id", "connection", "show",
                                      uuid).strip()))
    return found


def find_profile(name, nmcli=nmcli_run):
    matches = [c for c in profile_connections(nmcli) if c[1] == name]
    if not matches:
        raise RuntimeError(f"no nm-sshuttle profile named {name!r}")
    if len(matches) > 1:
        raise RuntimeError(f"several profiles are named {name!r}")
    return matches[0][0]


def parse_vpn_data(text):
    """nmcli's 'k = v, k = v' rendering of vpn.data."""
    data = {}
    for item in re.split(r",\s*(?=[\w-]+\s*=)", text.strip()):
        key, sep, value = item.partition("=")
        if sep:
            data[key.strip()] = value.strip()
    return data


def read_settings(uuid, nmcli=nmcli_run):
    """The settings profile.parse reads, from nmcli."""
    def get(field):
        return nmcli("-g", field, "connection", "show", uuid).strip()
    perms = [p for p in get("connection.permissions").split(",") if p]
    return {"connection": {"id": get("connection.id"), "uuid": uuid, "permissions": perms},
            "vpn": {"data": parse_vpn_data(get("vpn.data")),
                    "persistent": get("vpn.persistent") == "yes"},
            "ipv4": {"method": get("ipv4.method") or "auto"}}


def default_user():
    return os.environ.get("SUDO_USER") or pwd.getpwuid(os.getuid()).pw_name


def profile_data(args):
    """vpn.data for `add`; lists are space-separated because nmcli splits on commas."""
    data = {"remote": args.remote, "local-user": args.user, "subnets": args.subnets,
            "exclude": args.exclude, "dns": args.dns, "dns-servers": args.dns_servers,
            "dns-domains": args.dns_domains, "method": args.method,
            "gateway": args.gateway, "probe": args.probe, "fail-closed": args.fail_closed}
    return {k: " ".join(re.split(r"[\s,]+", v.strip())) for k, v in data.items() if v}


def add(args, nmcli=nmcli_run):
    if args.name.startswith("-") or not args.name.strip():
        raise RuntimeError(f"unusable profile name {args.name!r}")
    data = profile_data(args)
    # The plugin validates again at Connect; fail here with a message instead.
    profile.parse({"connection": {"id": args.name, "permissions": [f"user:{args.user}"]},
                   "vpn": {"data": data, "persistent": True}, "ipv4": {"method": "auto"}},
                  lambda n: pwd.getpwnam(n))
    if any(c[1] == args.name for c in profile_connections(nmcli)):
        raise RuntimeError(f"a profile named {args.name!r} exists; remove it first")
    nmcli("connection", "add", "type", "vpn", "con-name", args.name, "ifname", "--",
          "vpn-type", SERVICE_TYPE,
          "connection.permissions", f"user:{args.user}",
          "connection.autoconnect", "no", "vpn.persistent", "yes", "ipv4.method", "auto",
          "vpn.data", ", ".join(f"{k}={v}" for k, v in data.items()))
    print(f"added {args.name}; try: nm-sshuttle check {args.name}")
    return 0


def ssh_g_as_self(_user, target):
    p = subprocess.run(["ssh", "-G", "--", target], capture_output=True, text=True, timeout=20)
    if p.returncode:
        raise ValueError(f"ssh -G {target} failed: {p.stderr.strip()}")
    return p.stdout


def check(args, nmcli=nmcli_run, ssh_g=ssh_g_as_self, run=subprocess.run):
    """Check a profile as the user it belongs to (design §4.2, §4.3).

    Returns 0 when everything passes. The test login may prompt for a host key
    or a passphrase, which is the point: the plugin cannot."""
    uuid = find_profile(args.name, nmcli)
    settings = read_settings(uuid, nmcli)
    try:
        prof = profile.parse(settings, lambda n: pwd.getpwnam(n))
    except profile.ProfileError as e:
        print(f"FAIL profile: {e}")
        print("     (GNOME Settings can change permissions and the IPv4 method; "
              "fix with nmcli or remove and add again)")
        return 1
    print("ok   profile fields")
    if prof.user != pwd.getpwuid(os.getuid()).pw_name:
        print(f"FAIL run this as {prof.user}: ssh must see that user's config, agent "
              "and known_hosts")
        return 1
    try:
        if prof.gateway:
            print(f"ok   ssh -G skipped; gateway is {prof.gateway}")
        else:
            print(f"ok   ssh -G; first hop {tunnel.first_hop(prof.user, prof.remote, ssh_g)}")
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        print(f"FAIL ssh -G: {e}")
        return 1
    # Not BatchMode: this is where the host key gets accepted once.
    p = run(["ssh", "-o", "ConnectTimeout=15", "--", prof.remote, "true"])
    if p.returncode:
        print(f"FAIL test login to {prof.remote} (ssh exit {p.returncode})")
        return 1
    print("ok   host key and test login")
    return 0


def list_profiles(nmcli=nmcli_run, out=print):
    for uuid, name in profile_connections(nmcli):
        data = parse_vpn_data(nmcli("-g", "vpn.data", "connection", "show", uuid))
        out(f"{name}\t{data.get('remote', '?')}\t{data.get('subnets', '?')}")
    return 0


def remove(args, nmcli=nmcli_run):
    nmcli("connection", "delete", "uuid", find_profile(args.name, nmcli))
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
    a = sub.add_parser("add", help="create a profile")
    a.add_argument("name")
    a.add_argument("--remote", required=True)
    a.add_argument("--subnets", required=True)
    a.add_argument("--user", default=default_user())
    for opt in ("exclude", "dns-servers", "dns-domains", "gateway", "probe"):
        a.add_argument("--" + opt)
    a.add_argument("--dns", choices=["none", "split", "all"])
    a.add_argument("--method", choices=["nft", "nat"])
    a.add_argument("--fail-closed", choices=["yes", "no"])
    c = sub.add_parser("check", help="check a profile as its user")
    c.add_argument("name")
    sub.add_parser("list", help="list profiles")
    r = sub.add_parser("remove", help="delete a profile")
    r.add_argument("name")
    args = parser.parse_args(argv)
    if args.cmd in ("add", "check", "list", "remove"):
        try:
            return {"add": add, "check": check, "remove": remove,
                    "list": lambda _: list_profiles()}[args.cmd](args)
        except (RuntimeError, profile.ProfileError, OSError,
                subprocess.TimeoutExpired) as e:
            print(f"nm-sshuttle: {e}", file=sys.stderr)
            return 1
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
