# SPDX-License-Identifier: MIT
"""nm-sshuttle: the commands the units and the plugin run.

  cleanup --slot N
                 stop slot N's tunnel unit, remove its link, guard, tproxy
                 policy routing and files, and stale sshuttle tables and
                 chains (ExecStopPost of nm-sshuttle@N.service, and the
                 plugin's startup cleanup)
  cleanup [--all]
                 the same for every slot whose plugin is not running, and
                 what a single-tunnel version left behind
  sweep          remove stale sshuttle tables and chains (the tunnel unit's
                 ExecStartPre and ExecStopPost)
  exec-tunnel N  ExecStart of the tunnel unit nm-sshuttle-tunnel@N
  ssh-as-user    sshuttle's ssh command
  first-hop      print the first hop's addresses, one per line, worked out as
                 the user
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

from . import claims, guard, profile, tunnel
from .const import (NM_DNS_IFACE, NM_DNS_PATH, NM_NAME, SERVICE_TYPE, SLOTS, V1_GUARD_TABLE,
                    V1_PLUGIN_UNIT, V1_TUNNEL_SPEC, V1_TUNNEL_UNIT, Slot, slot_number)

log = logging.getLogger("nm-sshuttle")


def systemctl(*args, timeout=30):
    try:
        return subprocess.run(["systemctl", "--no-pager", *args], capture_output=True,
                              text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(args, 255, "", str(e))


def _remove(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as e:
        log.warning("cannot remove %s: %s", path, e)


def _stop(unit):
    # Always stop it: "is-active" is false for a unit that is still starting,
    # and stop does nothing for an inactive one.
    p = systemctl("stop", unit)
    if p.returncode:
        log.warning("cannot stop %s: %s", unit, p.stderr.strip())
        return False
    return True


def cleanup_slot(n):
    """Remove everything slot n's activation leaves behind (design §4.4, §4.7)."""
    slot = Slot(n)
    ok = _stop(slot.tunnel_unit)
    if guard.link_ifindex(slot.link) is not None:
        log.info("removing a leftover %s", slot.link)
        ok &= guard.remove_link(slot.link)
    if guard.guard_present(slot.guard_table):
        log.info("removing a leftover guard table %s", slot.guard_table)
        ok &= guard.remove_guard(slot.guard_table)
    ok &= guard.remove_policy_routing(n)
    _remove(slot.spec)
    claims.release(n)
    return ok


def cleanup_v1():
    """What a single-tunnel version created, unless its plugin still runs."""
    ok = _stop(V1_TUNNEL_UNIT)
    if guard.guard_present(V1_GUARD_TABLE):
        log.info("removing a leftover guard table %s", V1_GUARD_TABLE)
        ok &= guard.remove_guard(V1_GUARD_TABLE)
    _remove(V1_TUNNEL_SPEC)
    return ok


def unit_running(unit):
    return systemctl("is-active", "--quiet", unit).returncode == 0


def cleanup(slot=None):
    """Remove what an activation leaves behind: slot's, or with slot None every
    slot whose plugin is not running, and a single-tunnel version's names.

    Safe at any time no activation runs in that slot's live plugin: after a
    kill NM has already disconnected the VPN, because it does so whenever the
    plugin's bus name vanishes."""
    if slot is not None:
        ok = cleanup_slot(slot)
    else:
        ok = True
        for n in range(SLOTS):
            if not unit_running(Slot(n).plugin_unit):
                ok &= cleanup_slot(n)
        if not unit_running(V1_PLUGIN_UNIT):
            ok &= cleanup_v1()
    tunnel.sweep()
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
        print("nm-sshuttle: a VPN is active; the unmanaged setting for the nmss links takes "
              "effect at NetworkManager's next start", file=sys.stderr)
        return 0
    p = subprocess.run(["nmcli", "general", "reload", "conf"], capture_output=True, text=True)
    if p.returncode:
        print(f"nm-sshuttle: nmcli general reload conf failed: {p.stderr.strip()}",
              file=sys.stderr)
    return 0


def first_hop(user, remote):
    try:
        print("\n".join(tunnel.first_hop(user, remote)))
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


def show_field(nmcli, field, uuid):
    """One field of a connection. `-g` escapes ':' and '\\' as in terse mode, so
    user:alice reads as user\\:alice; undo that."""
    return re.sub(r"\\(.)", r"\1", nmcli("-g", field, "connection", "show", uuid).strip())


def profile_connections(nmcli=nmcli_run):
    """(uuid, id) of every nm-sshuttle profile."""
    found = []
    for line in nmcli("-g", "UUID,TYPE", "connection", "show").splitlines():
        uuid, _, kind = line.partition(":")
        if kind != "vpn":
            continue
        if show_field(nmcli, "vpn.service-type", uuid) == SERVICE_TYPE:
            found.append((uuid, show_field(nmcli, "connection.id", uuid)))
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
        return show_field(nmcli, field, uuid)
    perms = [p for p in get("connection.permissions").split(",") if p]
    return {"connection": {"id": get("connection.id"), "uuid": uuid, "permissions": perms},
            "vpn": {"data": parse_vpn_data(get("vpn.data")),
                    "persistent": get("vpn.persistent") == "yes"},
            "ipv4": {"method": get("ipv4.method") or "auto"},
            "ipv6": {"auto-route-ext-gw": get("ipv6.auto-route-ext-gw")}}


# Design §4.1: no host route towards the first hop, and the VPN's DNS entry can
# never take resolved's default route by itself.
PROFILE_IP_SETTINGS = ("ipv4.auto-route-ext-gw", "no", "ipv6.auto-route-ext-gw", "no",
                       "ipv4.never-default", "yes", "ipv6.never-default", "yes")


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
          *PROFILE_IP_SETTINGS,
          "vpn.data", ", ".join(f"{k}={v}" for k, v in data.items()))
    print(f"added {args.name}; try: nm-sshuttle check {args.name}")
    return 0


def ssh_g_as_self(_user, target):
    p = subprocess.run(["ssh", "-G", "--", target], capture_output=True, text=True, timeout=20)
    if p.returncode:
        raise ValueError(f"ssh -G {target} failed: {p.stderr.strip()}")
    return p.stdout


def active_conflict(uuid, prof, hops, nmcli, ssh_g):
    """Whether prof would conflict with the nm-sshuttle VPNs active now (the
    ones this user can see), as the plugin would refuse it."""
    others = []
    for other in active_sshuttle_vpns(nmcli):
        if other == uuid:
            continue
        try:
            o = profile.parse(read_settings(other, nmcli), lambda n: pwd.getpwnam(n))
            o_hops = [o.gateway] if o.gateway else tunnel.first_hop(o.user, o.remote, ssh_g)
        except (profile.ProfileError, OSError, ValueError, RuntimeError,
                subprocess.TimeoutExpired):
            continue
        others.append(claims.make_claim(o, o_hops))
    return claims.conflict(claims.make_claim(prof, hops), others)


def nm_dns_mode(run=subprocess.run):
    """NetworkManager's DNS mode (systemd-resolved, default, dnsmasq, none), or None."""
    try:
        p = run(["busctl", "--system", "get-property", NM_NAME, NM_DNS_PATH, NM_DNS_IFACE,
                 "Mode"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.fullmatch(r's "(.*)"', p.stdout.strip()) if p.returncode == 0 else None
    return m.group(1) if m else None


def check(args, nmcli=nmcli_run, ssh_g=ssh_g_as_self, run=subprocess.run,
          iptables_available=None, dns_mode=None, resolv_conf=None, cgroup=None):
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
    if prof.method == "tproxy" and not (iptables_available or tunnel.iptables_available)():
        print("FAIL method=tproxy needs iptables and ip6tables; install iptables")
        return 1
    if prof.dns != "none":
        # The plugin refuses it too (design §4.5): unless every lookup goes to
        # resolved, they fall back to the LAN's resolver whenever the tunnel is down.
        mode = (dns_mode or nm_dns_mode)()
        if mode is None:
            print("WARN cannot read NetworkManager's DNS mode; dns=split and dns=all need "
                  "systemd-resolved")
        elif mode != "systemd-resolved":
            print(f"FAIL dns={prof.dns} needs NetworkManager to use systemd-resolved; its DNS "
                  f"mode is {mode}")
            return 1
        why = (resolv_conf or tunnel.resolv_conf_problem)()
        if why:
            print(f"FAIL dns={prof.dns} needs every lookup to go to systemd-resolved; {why}")
            return 1
    if (cgroup or tunnel.unit_cgroup)("nm-sshuttle.service") is None:
        print("WARN no cgroup hierarchy under /sys/fs/cgroup holds systemd's units: the health "
              "check cannot tell the tunnel's own listener, and the plugin gives up 10 s after "
              "it connects")
    if settings["ipv6"]["auto-route-ext-gw"] != "no":
        # Profiles added before IPv6 support lack it; NM then adds a host route
        # towards an IPv6 first hop (docs/later/ipv6.md).
        print(f"WARN ipv6.auto-route-ext-gw is not no; fix with: nmcli connection modify "
              f"{uuid} ipv6.auto-route-ext-gw no")
    if prof.user != pwd.getpwuid(os.getuid()).pw_name:
        print(f"FAIL run this as {prof.user}: ssh must see that user's config, agent "
              "and known_hosts")
        return 1
    try:
        if prof.gateway:
            hops = [prof.gateway]
            print(f"ok   ssh -G skipped; gateway is {prof.gateway}")
        else:
            hops = tunnel.first_hop(prof.user, prof.remote, ssh_g)
            print(f"ok   ssh -G; first hop {', '.join(hops)}")
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        print(f"FAIL ssh -G: {e}")
        return 1
    # The plugin refuses a profile that conflicts with an active one, and GNOME
    # only shows "Connection failed" (docs/later/multiple-tunnels.md).
    why = active_conflict(uuid, prof, hops, nmcli, ssh_g)
    if why:
        print(f"FAIL {why}")
        return 1
    # Not BatchMode: this is where the host key gets accepted once.
    p = run(["ssh", "-o", "ConnectTimeout=15", "--", prof.remote, "true"])
    if p.returncode:
        print(f"FAIL test login to {prof.remote} (ssh exit {p.returncode})")
        return 1
    print("ok   host key and test login")
    return 0


def list_profiles(nmcli=nmcli_run, out=print):
    active = set(active_sshuttle_vpns(nmcli))
    for uuid, name in profile_connections(nmcli):
        data = parse_vpn_data(show_field(nmcli, "vpn.data", uuid))
        line = f"{name}\t{data.get('remote', '?')}\t{data.get('subnets', '?')}"
        out(line + ("\tactive" if uuid in active else ""))
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
    cl = sub.add_parser("cleanup")
    which = cl.add_mutually_exclusive_group()
    which.add_argument("--slot", type=slot_number)
    which.add_argument("--all", action="store_true")
    sub.add_parser("sweep")
    et = sub.add_parser("exec-tunnel")
    et.add_argument("slot", type=slot_number, nargs="?")
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
    a.add_argument("--method", choices=["nft", "nat", "tproxy"])
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
        return cleanup(args.slot)
    if args.cmd == "sweep":
        tunnel.sweep()
        return 0
    if args.cmd == "exec-tunnel":
        return tunnel.exec_tunnel(args.slot)
    if args.cmd == "post-install":
        return post_install()
    if args.cmd == "first-hop":
        return first_hop(args.user, args.remote)
    return 2
