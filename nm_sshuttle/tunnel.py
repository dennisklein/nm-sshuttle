# SPDX-License-Identifier: MIT
"""sshuttle side: the tunnel unit's ExecStart, the ssh bridge, the first hop
and the sweep of stale sshuttle tables (design §2.2, §4.3, §4.7)."""

import ipaddress
import json
import logging
import os
import pwd
import re
import shlex
import shutil
import socket
import stat
import struct
import subprocess
import sys
import threading

from .claims import runs_as_root
from .const import LIBEXEC, LISTEN_PORTS, SSH_CONFIG, V1_TUNNEL_SPEC, Slot
from .profile import collapse, family, split_remote

log = logging.getLogger("nm-sshuttle")

SSH_OPTIONS = ["-o", "BatchMode=yes", "-o", "ControlMaster=no", "-o", "ControlPath=none",
               "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=10",
               "-o", "ServerAliveCountMax=3"]
SEARCH_PATH = "/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin"
# Markers in the tunnel unit's journal that mean "the user has to act" (design §4.3).
AUTH_MARKERS = ("Permission denied", "Host key verification failed",
                "agent refused operation")
SSHUTTLE_TABLE_RE = re.compile(r"^sshuttle-ipv([46])-(\d+)$")
# What may go into the generated ssh config unquoted.
SSH_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# method=tproxy: sshuttle's chains in the mangle table (docs/later/tproxy.md).
SSHUTTLE_CHAIN_RE = re.compile(r"^sshuttle-([mtd])-(\d+)$")
IPTABLES = {4: "iptables", 6: "ip6tables"}


# sshuttle's -r. sshuttle 2.0.0 excludes its remote by itself, through ssh -G
# and a lookup as root, on the network the host is on. An address needs no
# lookup, and sshuttle already excludes this one, its IPv4 listen address. The
# bridge gives ssh the profile's remote in its place (design §4.3).
STAND_IN = "127.0.0.1"


def bridge_command(user, remote=None, ssh_config=None):
    """sshuttle's -e: the bridge, which gives ssh remote for the stand-in."""
    cmd = f"{os.path.join(LIBEXEC, 'nm-sshuttle')} ssh-as-user {user}"
    if remote:
        cmd += f" --remote {shlex.quote(remote)}"
    return f"{cmd} -F {ssh_config}" if ssh_config else cmd


def bridged_args(remote, args):
    """ssh's arguments for sshuttle's: the profile's remote in place of the
    stand-in. sshuttle 1.3.1 and 2.0.0 append [-p PORT] [USER@]HOST -- COMMAND
    to the -e command, as their parse_hostport splits -r; the stand-in has no
    user and no port. Anything else is refused, so that only the remote the
    plugin checked reaches ssh, split the same way."""
    opts = args[:2] if args[:1] == ["-F"] else []
    rest = args[len(opts):]
    if len(rest) != 3 or rest[0] != STAND_IN or rest[1] != "--":
        raise ValueError(f"unexpected arguments from sshuttle: {' '.join(rest[:2])}")
    user, host, port = split_remote(remote)
    port_opts = ["-p", str(port)] if port is not None else []
    return [*opts, *port_opts, f"{user}@{host}" if user else host, "--", rest[2]]


def first_hops(spec):
    """The first hop's addresses from a tunnel spec. A spec written before IPv6
    support holds one address as a string (docs/later/ipv6.md)."""
    hops = spec["first_hop"]
    return [hops] if isinstance(hops, str) else list(hops)


def sshuttle_argv(spec, sshuttle, ssh_config=None, port=None):
    """sshuttle's command line for a tunnel spec written by the plugin; ssh
    reads ssh_config instead of the usual files when it is given.

    Without spec["ipv6"] sshuttle runs with --disable-ipv6 and gets no IPv6
    network at all, which it would only drop with a warning; the guard still
    covers them.

    port is the IPv4 redirect port (design §4.8). sshuttle 1.3.1 and 2.0.0
    stop with an UnboundLocalError when --listen gives a port for both
    families, so ::1 gets port 0: sshuttle picks its IPv6 port as before."""
    ipv6 = bool(spec.get("ipv6"))

    def wanted(nets):
        return [n for n in nets if ipv6 or family(n) == 4]
    subnets = wanted(spec["subnets"])
    argv = [sshuttle, "-v", "--method", spec.get("method", "nft")]
    if not ipv6:
        argv.append("--disable-ipv6")
    if port:
        argv += ["--listen", f"127.0.0.1:{port}" + (",[::1]:0" if ipv6 else "")]
    if spec.get("tmark"):
        argv += ["--tmark", f"{spec['tmark']:#x}"]
    argv += ["-r", STAND_IN]
    for net in wanted(first_hops(spec) + list(spec.get("exclude", []))):
        argv += ["-x", net]
    argv += ["-e", bridge_command(spec["user"], spec["remote"], ssh_config)]
    servers = spec.get("dns_servers") or []
    if spec.get("dns", "none") != "none" and servers:
        # --to-ns pins the remote resolver and avoids the crash of lab T9.
        argv += ["--ns-hosts", ",".join(servers), "--to-ns", servers[0]]
        subnets += [f"{s}/32" for s in servers]   # DNS over TCP goes through too
    return argv + collapse(subnets)


def ipv6_available(lo="/proc/sys/net/ipv6/conf/lo/disable_ipv6",
                   if_inet6="/proc/net/if_inet6"):
    """Whether sshuttle can listen on ::1: IPv6 is on in the kernel and on lo.
    Without it sshuttle stops with "Could not bind to an IPv6 socket"."""
    try:
        with open(lo) as f:
            if f.read().strip() != "0":
                return False
    except OSError:
        return False
    return os.path.exists(if_inet6)


def exec_tunnel(slot=None):
    """ExecStart of the tunnel unit: exec sshuttle so that it is the main
    process and its own READY=1 counts. Without a slot it reads the spec of a
    single-tunnel version's plugin, which may still run during an upgrade."""
    with open(V1_TUNNEL_SPEC if slot is None else Slot(slot).spec) as f:
        spec = json.load(f)
    sshuttle = shutil.which("sshuttle", path=SEARCH_PATH)
    if not sshuttle:
        print("exec-tunnel: sshuttle not found", file=sys.stderr)
        return 1
    ssh_config = None
    text = pinned_ssh_config(spec)
    if text:
        ssh_config = write_ssh_config(text, SSH_CONFIG if slot is None else Slot(slot).ssh_config)
        print(f"exec-tunnel: the first hop's name does not resolve; ssh gets its address "
              f"from connect through {ssh_config}", file=sys.stderr, flush=True)
    port = None if slot is None else listen_port(Slot(slot).listen_ports,
                                                  udp=spec.get("method") == "tproxy")
    argv = sshuttle_argv(spec, sshuttle, ssh_config, port)
    print("exec-tunnel: exec " + " ".join(argv), file=sys.stderr, flush=True)
    os.execv(sshuttle, argv)


def unprivileged_port_start(path="/proc/sys/net/ipv4/ip_unprivileged_port_start"):
    """The lowest port any user may bind (it covers IPv6 too), or None."""
    try:
        with open(path) as f:
            return int(f.read())
    except (OSError, ValueError):
        return None


def bindable(port, kind=socket.SOCK_STREAM):
    """Whether sshuttle could bind 127.0.0.1:port now. Like sshuttle, without
    SO_REUSEADDR, so that a port in TIME_WAIT counts as taken (lab T8)."""
    try:
        with socket.socket(socket.AF_INET, kind) as s:
            s.bind(("127.0.0.1", port))
    except OSError:
        return False
    return True


def listen_port(ports, udp=False, bindable=bindable, port_start=unprivileged_port_start):
    """The redirect port: the first of ports, in the slot's order, that is free
    (design §4.8). method=tproxy also binds UDP on it. When none is free, the
    first: sshuttle then stops with EADDRINUSE, a failed start like any other
    (the plugin gives up at Connect, and tries again in a gap)."""
    start = port_start()
    if start is not None and start <= max(ports):
        print(f"exec-tunnel: net.ipv4.ip_unprivileged_port_start is {start}, so any user "
              f"may bind the redirect ports {min(ports)}-{max(ports)} and stop the tunnel from "
              f"starting", file=sys.stderr, flush=True)
    for port in ports:
        if bindable(port) and (not udp or bindable(port, socket.SOCK_DGRAM)):
            return port
    print(f"exec-tunnel: no free redirect port among {min(ports)}-{max(ports)}; trying "
          f"{ports[0]}", file=sys.stderr, flush=True)
    return ports[0]


# systemd-resolved's stub listeners: the full one and the proxy-only one
RESOLVED_STUBS = ("127.0.0.53", "127.0.0.54")


def resolv_conf_problem(path="/etc/resolv.conf"):
    """None when path sends every lookup to systemd-resolved's stub, else why
    not (design §4.5). NM's DNS mode is systemd-resolved also when the file
    links to resolved's list of the real servers, or when NM leaves it alone:
    glibc then asks the listed servers itself, the LAN's among them."""
    try:
        with open(path) as f:
            servers = [w[1] for w in (line.split() for line in f)
                       if len(w) > 1 and w[0] == "nameserver"]
    except OSError as e:
        return f"cannot read {path}: {e.strerror}"
    others = [s for s in servers if s not in RESOLVED_STUBS]
    if others:
        return f"{path} lists {', '.join(others)}, not only resolved's stub {RESOLVED_STUBS[0]}"
    if not servers:
        return f"{path} lists no nameserver"
    return None


def classify_failure(journal):
    """'login' when the tunnel failed on authentication, else 'connect'."""
    return "login" if any(m in journal for m in AUTH_MARKERS) else "connect"


def port_failure(journal, ipv6):
    """What sshuttle found no free port for, from its journal, or None (design
    §4.8). sshuttle -v logs its TCP redirector once it is bound and then
    searches the DNS port; the IPv4 TCP port is exec-tunnel's choice, which
    logs when none of its ports was free."""
    if "Address already in use" not in journal:
        return None
    if "TCP redirector listening" in journal:
        return ("no free port for sshuttle's DNS redirector (9001-12300); another user may be "
                "holding them")
    if ipv6 and "no free redirect port" not in journal:
        return ("no free port for sshuttle's IPv6 redirector on ::1 (9001-12300); another user "
                "may be holding them")
    return (f"sshuttle's redirect port ({LISTEN_PORTS[0]}-{LISTEN_PORTS[-1]}) was taken, by "
            f"another tunnel or by root")


# ------------------------------------------------------------- ssh bridge
def user_environment(user, pw):
    """The user's systemd manager environment, read as the user."""
    rt = f"/run/user/{pw.pw_uid}"
    try:
        p = subprocess.run(
            ["setpriv", f"--reuid={pw.pw_uid}", f"--regid={pw.pw_gid}", "--init-groups",
             "--reset-env", "env", f"XDG_RUNTIME_DIR={rt}",
             f"DBUS_SESSION_BUS_ADDRESS=unix:path={rt}/bus",
             "systemctl", "--user", "--no-pager", "show-environment"],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    env = {}
    for line in p.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            env.setdefault(key, value)
    return env


def ssh_as_user(user, args):
    """sshuttle's -e command: run ssh as USER with the user's agent and display,
    so ~/.ssh/config, known_hosts and agent prompts work as usual. Only ssh runs
    as the user; it never runs as root. With --remote first, ssh gets that
    remote in place of sshuttle's stand-in."""
    if args[:1] == ["--remote"]:
        if len(args) < 2:
            raise ValueError("--remote needs a value")
        args = bridged_args(args[1], args[2:])
    pw = pwd.getpwnam(user)
    rt = f"/run/user/{pw.pw_uid}"
    menv = user_environment(user, pw)
    sock, origin = menv.get("SSH_AUTH_SOCK", ""), "user manager"
    if not sock:
        origin = "fallback"
        for cand in (f"{rt}/gcr/ssh", f"{rt}/keyring/ssh"):
            try:
                if stat.S_ISSOCK(os.stat(cand).st_mode):
                    sock = cand
                    break
            except OSError:
                pass
    print(f"ssh-as-user: user={user} agent={sock or 'none'} ({origin})", file=sys.stderr,
          flush=True)
    env = [f"HOME={pw.pw_dir}", f"USER={user}", f"LOGNAME={user}",
           "PATH=/usr/local/bin:/usr/bin:/bin", f"XDG_RUNTIME_DIR={rt}",
           f"SSH_AUTH_SOCK={sock}"]
    for key in ("WAYLAND_DISPLAY", "DISPLAY", "XAUTHORITY"):
        if menv.get(key):
            env.append(f"{key}={menv[key]}")
    argv = ["setpriv", f"--reuid={pw.pw_uid}", f"--regid={pw.pw_gid}", "--init-groups",
            "--reset-env", "env", *env, "ssh", *SSH_OPTIONS, *args]
    os.execvp("setpriv", argv)


# -------------------------------------------------------------- first hop
def host_of(spec):
    """'user@host:port' or '[v6]:port' -> host"""
    spec = spec.rsplit("@", 1)[-1]
    if spec.startswith("["):
        return spec[1:spec.index("]")]
    return spec.split(":", 1)[0]


def parse_ssh_g(output):
    """(hostname, first jump host or None) from 'ssh -G' output."""
    hostname = jump = None
    for line in output.splitlines():
        key, _, value = line.partition(" ")
        if key == "hostname":
            hostname = value
        elif key == "proxyjump" and value != "none":
            jump = host_of(value.split(",")[0])
        elif key == "proxycommand" and value != "none":
            raise ValueError("the ssh config uses ProxyCommand; set 'gateway' in the profile")
    if not (jump or hostname):
        raise ValueError("ssh -G printed no hostname")
    return hostname, jump


def ssh_g(user, target):
    p = subprocess.run([os.path.join(LIBEXEC, "nm-sshuttle"), "ssh-as-user", user,
                        "-G", "--", target], capture_output=True, text=True, timeout=20)
    if p.returncode:
        raise ValueError(f"ssh -G {target} failed: {p.stderr.strip()}")
    return p.stdout


def first_hop_config(user, remote, ssh_g=ssh_g):
    """(name, settings): the first hop as ssh is given it, and its 'ssh -G'
    settings, worked out as the user.

    A ProxyJump host may itself be an alias from the user's config, and may have
    a ProxyJump of its own, so follow the chain."""
    target = remote
    for _ in range(5):
        if target.startswith("-"):
            raise ValueError(f"unusable ssh destination {target!r}")
        out = ssh_g(user, target)
        _, jump = parse_ssh_g(out)
        if not jump:
            settings = {}
            for line in out.splitlines():
                key, _, value = line.partition(" ")
                settings.setdefault(key, value)
            return target, settings
        target = jump
    raise ValueError("too many nested ProxyJump hosts; set 'gateway' in the profile")


def first_hop(user, remote, ssh_g=ssh_g, getaddrinfo=socket.getaddrinfo):
    """Every address of the first hop, worked out as the user (design §2.2).

    ssh connects to whichever of the name's addresses works first, of either
    family, so all of them are returned (docs/later/ipv6.md)."""
    hostname = first_hop_config(user, remote, ssh_g)[1]["hostname"]
    addrs = []
    for *_, sockaddr in getaddrinfo(hostname, 22, socket.AF_UNSPEC, socket.SOCK_STREAM):
        addr = sockaddr[0].split("%", 1)[0]       # no scope id: it is no network
        if addr not in addrs:
            addrs.append(addr)
    if not addrs:
        raise ValueError(f"{hostname} has no address")
    return addrs


def resolves(name, timeout=5.0, getaddrinfo=socket.getaddrinfo):
    """Whether name resolves within timeout seconds."""
    found = []

    def look():
        try:
            found.append(bool(getaddrinfo(name, 22, socket.AF_UNSPEC, socket.SOCK_STREAM)))
        except OSError:
            found.append(False)
    t = threading.Thread(target=look, daemon=True)
    t.start()
    t.join(timeout)
    return bool(found and found[0])


def pinned_ssh_config(spec, ssh_g=ssh_g, resolves=resolves, getpwnam=pwd.getpwnam):
    """An ssh config that gives ssh the first hop's address, or None.

    The first hop's name may lie in a domain that the tunnel's own DNS answers,
    such as a bastion inside ~corp.example. During a reconnect that DNS is down,
    so ssh cannot look the name up. The address looked up at connect still
    works: pin it, and read the user's and the system's config after it, as ssh
    would (design §4.3). When the name resolves, ssh runs as usual."""
    if spec.get("dns", "none") == "none" or not first_hops(spec):
        return None
    try:
        name, settings = first_hop_config(spec["user"], spec["remote"], ssh_g)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    hostname = settings.get("hostname", "")
    try:
        ipaddress.ip_address(hostname)
        return None
    except ValueError:
        pass
    keyalias = settings.get("hostkeyalias", hostname)
    if not all(SSH_NAME_RE.match(n) for n in (name, hostname, keyalias)):
        return None
    if resolves(hostname):
        return None
    home = getpwnam(spec["user"]).pw_dir
    hops = first_hops(spec)
    # One address only: IPv4 when there is one, which every uplink can reach.
    addr = next((a for a in hops if family(a) == 4), hops[0])
    return (f"# Written by nm-sshuttle: {hostname} did not resolve when the tunnel\n"
            f"# started, so ssh gets the address looked up at connect.\n"
            f"Host {name}\n"
            f"    HostName {addr}\n"
            f"    HostKeyAlias {keyalias}\n\n"
            f"Match all\n"
            f"Include {home}/.ssh/config\n"
            f"Include /etc/ssh/ssh_config\n")


def write_ssh_config(text, path=SSH_CONFIG):
    """Write it where ssh, running as the user, can read it, whatever the umask."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    os.chmod(os.path.dirname(path), 0o755)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
    os.fchmod(fd, 0o644)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)
    return path


# ------------------------------------------------------------------ sweep
# Where sshuttle listens: loopback, or the wildcard with an admin's --listen.
LISTEN_ADDRESSES = {4: {"127.0.0.1", "0.0.0.0"}, 6: {"::1", "::"}}


def proc_address(text):
    """An address from /proc/net/tcp{,6}: 32-bit words in host byte order."""
    raw = b"".join(struct.pack("=I", int(text[i:i + 8], 16)) for i in range(0, len(text), 8))
    return socket.inet_ntop(socket.AF_INET if len(raw) == 4 else socket.AF_INET6, raw)


def loopback_listeners(proc="/proc"):
    """The TCP listeners on the address sshuttle listens on, as (family, port,
    uid, socket inode)."""
    out = []
    for version, name in ((4, "net/tcp"), (6, "net/tcp6")):
        try:
            with open(os.path.join(proc, name)) as f:
                next(f, None)
                for line in f:
                    fields = line.split()
                    if len(fields) < 10 or fields[3] != "0A":   # TCP_LISTEN
                        continue
                    addr, _, port = fields[1].rpartition(":")
                    try:
                        if proc_address(addr) in LISTEN_ADDRESSES[version]:
                            out.append((version, int(port, 16), int(fields[7]), fields[9]))
                    except (ValueError, OSError, struct.error):
                        continue
        except OSError:
            pass
    return out


def listening_ports(proc="/proc", inodes=None):
    """{4: ports, 6: ports}: the ports root listens on for TCP, on the
    address sshuttle listens on; with inodes, only those of these sockets.

    Another user's listener never counts (design §4.8): one on the port of a
    leftover table would receive what that table still redirects."""
    ports = {4: set(), 6: set()}
    for version, port, uid, inode in loopback_listeners(proc):
        if uid == 0 and (inodes is None or inode in inodes):
            ports[version].add(port)
    return ports


def _firewall_helper(pid, proc):
    """Whether pid is sshuttle's firewall helper, run as root."""
    try:
        with open(os.path.join(proc, str(pid), "cmdline"), "rb") as f:
            args = f.read().split(b"\0")
    except OSError:
        return False
    return (b"--firewall" in args and any(b"sshuttle" in a for a in args)
            and runs_as_root(pid, proc))


def user_sshuttle_ports(listeners, proc="/proc"):
    """{4: ports, 6: ports}: those of listeners whose process has sshuttle's
    firewall helper, run as root, among its descendants: an sshuttle a user
    runs, with its helper through sudo (design §4.8). Only root, or a user
    whom sudo lets run that helper, can start one: sudo waiting for a
    password still has the caller's real user ID."""
    inodes = {inode: (version, port) for version, port, _, inode in listeners}
    parent, holders, helpers = {}, {}, []
    try:
        pids = [int(name) for name in os.listdir(proc) if name.isdigit()]
    except OSError:
        pids = []
    for pid in pids:
        base = os.path.join(proc, str(pid))
        try:
            with open(os.path.join(base, "stat")) as f:
                text = f.read()
            parent[pid] = int(text[text.rindex(")") + 2:].split()[1])
            fds = os.listdir(os.path.join(base, "fd"))
        except (OSError, ValueError, IndexError):
            continue
        for fd in fds:
            try:
                target = os.readlink(os.path.join(base, "fd", fd))
            except OSError:
                continue
            if target.startswith("socket:[") and target[8:-1] in inodes:
                holders.setdefault(pid, set()).add(inodes[target[8:-1]])
        if _firewall_helper(pid, proc):
            helpers.append(pid)
    ancestors = set()
    for pid in helpers:
        pid = parent.get(pid)
        while pid and pid not in ancestors:
            ancestors.add(pid)
            pid = parent.get(pid)
    ports = {4: set(), 6: set()}
    for pid in ancestors & holders.keys():
        for version, port in holders[pid]:
            ports[version].add(port)
    return ports


# Where systemd's units are: cgroup v2; the unified hierarchy of a hybrid
# host; systemd's own name=systemd hierarchy on a legacy (v1) host.
CGROUP_ROOTS = ("/sys/fs/cgroup", "/sys/fs/cgroup/unified", "/sys/fs/cgroup/systemd")


def unit_cgroup(unit, roots=CGROUP_ROOTS):
    """Where systemd puts a system service's processes: a template instance
    sits in its template's slice. None when no hierarchy holds system.slice."""
    root = next((r for r in roots if os.path.isdir(os.path.join(r, "system.slice"))), None)
    if root is None:
        return None
    if "@" in unit:
        template = unit.split("@", 1)[0].replace("-", "\\x2d")
        return os.path.join(root, "system.slice", f"system-{template}.slice", unit)
    return os.path.join(root, "system.slice", unit)


def unit_pids(unit, roots=CGROUP_ROOTS):
    """The processes of this unit, [] once it has stopped, or None when
    systemd's cgroups cannot be found."""
    cgroup = unit_cgroup(unit, roots)
    if cgroup is None:
        return None
    try:
        with open(os.path.join(cgroup, "cgroup.procs")) as f:
            return f.read().split()
    except FileNotFoundError:
        return []           # systemd removed the cgroup, and an empty slice's
    except OSError:
        return None


def unit_runs(unit, comm, roots=CGROUP_ROOTS, proc="/proc"):
    """Does a process of this unit have the command name comm? sshuttle 1.3
    can outlive its ssh, listening on with nothing behind it. None when
    systemd's cgroups cannot be found."""
    pids = unit_pids(unit, roots)
    if pids is None:
        return None
    for pid in pids:
        try:
            with open(os.path.join(proc, pid, "comm")) as f:
                if f.read().strip() == comm:
                    return True
        except OSError:
            continue
    return False


def unit_ports(unit, roots=CGROUP_ROOTS, proc="/proc"):
    """The ports processes of this unit listen on, as listening_ports: one
    tunnel's own sshuttle, never another's (docs/later/multiple-tunnels.md).
    None when systemd's cgroups cannot be found."""
    pids = unit_pids(unit, roots)
    if pids is None:
        return None
    inodes = set()
    for pid in pids:
        fd_dir = os.path.join(proc, pid, "fd")
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(os.path.join(fd_dir, fd))
            except OSError:
                continue
            if target.startswith("socket:["):
                inodes.add(target[8:-1])
    return listening_ports(proc, inodes)


def inet_tables(nft_list_tables):
    """Names of the inet tables in 'nft list tables' output."""
    names = []
    for line in nft_list_tables.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0] == "table" and parts[1] == "inet":
            names.append(parts[2])
    return names


def sshuttle_tables(nft_list_tables, ports, live):
    """sshuttle tables whose redirect port has a listener of their family
    (live) or none (stale); ports as listening_ports returns them."""
    out = []
    for name in inet_tables(nft_list_tables):
        m = SSHUTTLE_TABLE_RE.match(name)
        if m and (int(m.group(2)) in ports.get(int(m.group(1)), ())) == live:
            out.append(name)
    return out


def stale_tables(nft_list_tables, ports):
    """sshuttle tables whose redirect port has no root listener (lab T7c)."""
    return sshuttle_tables(nft_list_tables, ports, live=False)


def mangle_chains(iptables_s):
    """sshuttle's chains in 'iptables -t mangle -S' output: {name: port}."""
    chains = {}
    for line in iptables_s.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "-N":
            m = SSHUTTLE_CHAIN_RE.match(parts[1])
            if m:
                chains[parts[1]] = int(m.group(2))
    return chains


def stale_chain_commands(iptables_s, ports):
    """The iptables arguments that remove the sshuttle chains whose port has no
    listener among ports, those of the listing's family: the jumps from
    OUTPUT and PREROUTING first, then flush every chain, then delete them
    (the t-chain jumps to the d-chain)."""
    stale = [c for c, port in mangle_chains(iptables_s).items() if port not in ports]
    cmds = []
    for line in iptables_s.splitlines():
        parts = line.split()
        if (len(parts) >= 4 and parts[0] == "-A" and parts[1] in ("OUTPUT", "PREROUTING")
                and parts[-2] == "-j" and parts[-1] in stale):
            cmds.append(["-D"] + parts[1:])
    cmds += [["-F", c] for c in stale]
    cmds += [["-X", c] for c in stale]
    return cmds


def iptables_available():
    """Whether both iptables and ip6tables exist, as sshuttle's tproxy method
    needs (its is_supported)."""
    return all(shutil.which(cmd, path=SEARCH_PATH) for cmd in IPTABLES.values())


def _mangle(family):
    """'iptables -t mangle -S' for one family, or None."""
    try:
        p = subprocess.run([IPTABLES[family], "-w", "-t", "mangle", "-S"], capture_output=True,
                           text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout if p.returncode == 0 else None


def nft_health(method, guard_table, ports):
    """What the health check reads from the firewall (design §4.4, "Health"):
    is the guard table there, and for which families (4, 6) does sshuttle have
    its rules with a listener on their port? Those are a table in nft, or for
    method=tproxy a mark chain in iptables' mangle table. ports are this
    tunnel's own listeners, as unit_ports returns them. None when nft cannot
    be asked."""
    try:
        p = subprocess.run(["nft", "list", "tables"], capture_output=True, text=True,
                           timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode:
        return None
    if method == "tproxy":
        families = set()
        for version in IPTABLES:
            chains = mangle_chains(_mangle(version) or "")
            if any(c.startswith("sshuttle-m-") and port in ports.get(version, ())
                   for c, port in chains.items()):
                families.add(version)
    else:
        live = sshuttle_tables(p.stdout, ports, live=True)
        families = {int(SSHUTTLE_TABLE_RE.match(t).group(1)) for t in live}
    return {"guard": guard_table in inet_tables(p.stdout), "sshuttle": families}


def sweep(proc="/proc"):
    """Delete sshuttle tables and chains left by a killed tunnel (lab T7).

    A table or chain is live while root listens on its port, on 127.0.0.1 for
    IPv4 or ::1 for IPv6 (or the wildcard), whichever tunnel unit holds it: a
    tunnel's sweep must spare the other tunnels, and an sshuttle run as root
    outside the plugin. Another user's listener keeps it only while sshuttle's
    root firewall helper runs below that listener's process, as for an
    sshuttle the user runs; any other listener would receive the redirected
    traffic (design §4.8)."""
    try:
        out = subprocess.run(["nft", "list", "tables"], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("sweep: cannot list nft tables: %s", e)
        out = ""
    # method=tproxy leaves iptables chains; through iptables, so that this also
    # works with iptables-legacy (docs/later/tproxy.md).
    listings = {}
    for version, cmd in IPTABLES.items():
        if shutil.which(cmd, path=SEARCH_PATH):
            listing = _mangle(version)
            if listing is not None:
                listings[version] = listing
    listeners = loopback_listeners(proc)
    ports = {4: set(), 6: set()}
    users = {}
    for version, port, uid, _ in listeners:
        if uid == 0:
            ports[version].add(port)
        else:
            users.setdefault((version, port), set()).add(uid)
    stale = {(int(m.group(1)), int(m.group(2))) for m in
             (SSHUTTLE_TABLE_RE.match(t) for t in stale_tables(out, ports)) if m}
    stale |= {(version, port) for version, listing in listings.items()
              for port in mangle_chains(listing).values() if port not in ports[version]}
    held = [x for x in listeners if x[2] != 0 and (x[0], x[1]) in stale]
    if held:
        for version, kept in user_sshuttle_ports(held, proc).items():
            ports[version] |= kept

    def deleted(what, version, port):
        uids = users.get((version, port))
        if uids:
            log.warning("sweep: deleted %s although uid %s listens on its port, without "
                        "sshuttle's root firewall helper below it; an sshuttle run that way "
                        "has lost its rules (design §4.8)", what,
                        ", ".join(str(u) for u in sorted(uids)))
        else:
            log.info("sweep: deleted stale %s", what)

    for table in stale_tables(out, ports):
        p = subprocess.run(["nft", "delete", "table", "inet", table], capture_output=True,
                           text=True, timeout=10)
        if p.returncode == 0:
            m = SSHUTTLE_TABLE_RE.match(table)
            deleted(f"table {table}", int(m.group(1)), int(m.group(2)))
        else:
            log.warning("sweep: cannot delete %s: %s", table, p.stderr.strip())
    for version, listing in listings.items():
        cmd = IPTABLES[version]
        chains = mangle_chains(listing)
        for args in stale_chain_commands(listing, ports[version]):
            p = subprocess.run([cmd, "-w", "-t", "mangle", *args], capture_output=True,
                               text=True, timeout=10)
            if p.returncode:
                log.warning("sweep: %s -t mangle %s: %s", cmd, " ".join(args),
                            p.stderr.strip())
            elif args[0] == "-X":
                deleted(f"chain {args[1]} (IPv{version})", version, chains[args[1]])
