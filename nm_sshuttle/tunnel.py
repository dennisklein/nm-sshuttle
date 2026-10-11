# SPDX-License-Identifier: MIT
"""sshuttle side: the tunnel unit's ExecStart, the ssh bridge, the first hop
and the sweep of stale sshuttle tables (design §2.2, §4.3, §4.7)."""

import ipaddress
import json
import logging
import os
import pwd
import re
import shutil
import socket
import stat
import subprocess
import sys
import threading

from .const import LIBEXEC, SSH_CONFIG, V1_TUNNEL_SPEC, Slot
from .profile import collapse, family

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


def bridge_command(user, ssh_config=None):
    cmd = f"{os.path.join(LIBEXEC, 'nm-sshuttle')} ssh-as-user {user}"
    return f"{cmd} -F {ssh_config}" if ssh_config else cmd


def first_hops(spec):
    """The first hop's addresses from a tunnel spec. A spec written before IPv6
    support holds one address as a string (docs/later/ipv6.md)."""
    hops = spec["first_hop"]
    return [hops] if isinstance(hops, str) else list(hops)


def sshuttle_argv(spec, sshuttle, ssh_config=None):
    """sshuttle's command line for a tunnel spec written by the plugin; ssh
    reads ssh_config instead of the usual files when it is given.

    Without spec["ipv6"] sshuttle runs with --disable-ipv6 and gets no IPv6
    network at all, which it would only drop with a warning; the guard still
    covers them."""
    ipv6 = bool(spec.get("ipv6"))

    def wanted(nets):
        return [n for n in nets if ipv6 or family(n) == 4]
    subnets = wanted(spec["subnets"])
    argv = [sshuttle, "-v", "--method", spec.get("method", "nft")]
    if not ipv6:
        argv.append("--disable-ipv6")
    if spec.get("tmark"):
        argv += ["--tmark", f"{spec['tmark']:#x}"]
    argv += ["-r", spec["remote"]]
    for net in wanted(first_hops(spec) + list(spec.get("exclude", []))):
        argv += ["-x", net]
    argv += ["-e", bridge_command(spec["user"], ssh_config)]
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
    argv = sshuttle_argv(spec, sshuttle, ssh_config)
    print("exec-tunnel: exec " + " ".join(argv), file=sys.stderr, flush=True)
    os.execv(sshuttle, argv)


def classify_failure(journal):
    """'login' when the tunnel failed on authentication, else 'connect'."""
    return "login" if any(m in journal for m in AUTH_MARKERS) else "connect"


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
    as the user; it never runs as root."""
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
def listening_ports(proc_files=("/proc/net/tcp", "/proc/net/tcp6"), inodes=None):
    """Ports with a TCP listener; with inodes, only those of these sockets."""
    ports = set()
    for path in proc_files:
        try:
            with open(path) as f:
                next(f, None)
                for line in f:
                    fields = line.split()
                    if len(fields) > 3 and fields[3] == "0A" and (     # TCP_LISTEN
                            inodes is None or (len(fields) > 9 and fields[9] in inodes)):
                        ports.add(int(fields[1].rsplit(":", 1)[1], 16))
        except OSError:
            pass
    return ports


def unit_cgroup(unit, root="/sys/fs/cgroup"):
    """Where systemd puts a system service's processes: a template instance
    sits in its template's slice."""
    if "@" in unit:
        template = unit.split("@", 1)[0].replace("-", "\\x2d")
        return os.path.join(root, "system.slice", f"system-{template}.slice", unit)
    return os.path.join(root, "system.slice", unit)


def unit_runs(unit, comm, root="/sys/fs/cgroup", proc="/proc"):
    """Does a process of this unit have the command name comm? sshuttle 1.3
    can outlive its ssh, listening on with nothing behind it. None when the
    unit's cgroup is not where it is expected."""
    try:
        with open(os.path.join(unit_cgroup(unit, root), "cgroup.procs")) as f:
            pids = f.read().split()
    except FileNotFoundError:
        return None if not os.path.isdir(os.path.dirname(unit_cgroup(unit, root))) else False
    except OSError:
        return None
    for pid in pids:
        try:
            with open(os.path.join(proc, pid, "comm")) as f:
                if f.read().strip() == comm:
                    return True
        except OSError:
            continue
    return False


def unit_ports(unit, root="/sys/fs/cgroup", proc="/proc"):
    """The TCP ports processes of this unit listen on: one tunnel's own
    sshuttle, never another's (docs/later/multiple-tunnels.md). None when
    the unit's cgroup is not where it is expected."""
    try:
        with open(os.path.join(unit_cgroup(unit, root), "cgroup.procs")) as f:
            pids = f.read().split()
    except FileNotFoundError:
        return None if not os.path.isdir(os.path.dirname(unit_cgroup(unit, root))) else set()
    except OSError:
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
    return listening_ports((os.path.join(proc, "net/tcp"), os.path.join(proc, "net/tcp6")),
                           inodes)


def inet_tables(nft_list_tables):
    """Names of the inet tables in 'nft list tables' output."""
    names = []
    for line in nft_list_tables.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0] == "table" and parts[1] == "inet":
            names.append(parts[2])
    return names


def sshuttle_tables(nft_list_tables, ports, live):
    """sshuttle tables whose redirect port has a listener (live) or none (stale)."""
    out = []
    for name in inet_tables(nft_list_tables):
        m = SSHUTTLE_TABLE_RE.match(name)
        if m and (int(m.group(2)) in ports) == live:
            out.append(name)
    return out


def stale_tables(nft_list_tables, ports):
    """sshuttle tables whose redirect port has no listener (lab T7c)."""
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
    listener: the jumps from OUTPUT and PREROUTING first, then flush every
    chain, then delete them (the t-chain jumps to the d-chain)."""
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


def nft_health(method, guard_table, ports=None):
    """What the health check reads from the firewall (design §4.4, "Health"):
    is the guard table there, and for which families (4, 6) does sshuttle have
    its rules with a listener on their port? Those are a table in nft, or for
    method=tproxy a mark chain in iptables' mangle table. ports limits it to
    this tunnel's own listeners. None when nft cannot be asked."""
    try:
        p = subprocess.run(["nft", "list", "tables"], capture_output=True, text=True,
                           timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode:
        return None
    if ports is None:
        ports = listening_ports()
    if method == "tproxy":
        families = set()
        for version in IPTABLES:
            chains = mangle_chains(_mangle(version) or "")
            if any(c.startswith("sshuttle-m-") and port in ports for c, port in chains.items()):
                families.add(version)
    else:
        live = sshuttle_tables(p.stdout, ports, live=True)
        families = {int(SSHUTTLE_TABLE_RE.match(t).group(1)) for t in live}
    return {"guard": guard_table in inet_tables(p.stdout), "sshuttle": families}


def sweep():
    """Delete sshuttle tables and chains left by a killed tunnel; they only
    black-hole traffic."""
    try:
        out = subprocess.run(["nft", "list", "tables"], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("sweep: cannot list nft tables: %s", e)
        out = ""
    ports = listening_ports()
    for table in stale_tables(out, ports):
        p = subprocess.run(["nft", "delete", "table", "inet", table], capture_output=True,
                           text=True, timeout=10)
        if p.returncode == 0:
            log.info("sweep: deleted stale table %s", table)
        else:
            log.warning("sweep: cannot delete %s: %s", table, p.stderr.strip())
    # method=tproxy leaves iptables chains; through iptables, so that this also
    # works with iptables-legacy (docs/later/tproxy.md).
    for version, cmd in IPTABLES.items():
        if not shutil.which(cmd, path=SEARCH_PATH):
            continue
        listing = _mangle(version)
        if listing is None:
            continue
        for args in stale_chain_commands(listing, ports):
            p = subprocess.run([cmd, "-w", "-t", "mangle", *args], capture_output=True,
                               text=True, timeout=10)
            if p.returncode:
                log.warning("sweep: %s -t mangle %s: %s", cmd, " ".join(args),
                            p.stderr.strip())
            elif args[0] == "-X":
                log.info("sweep: deleted stale chain %s (IPv%d)", args[1], version)
