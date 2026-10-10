# SPDX-License-Identifier: MIT
"""sshuttle side: the tunnel unit's ExecStart, the ssh bridge, the first hop
and the sweep of stale sshuttle tables (design §2.2, §4.3, §4.7)."""

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

from .const import GUARD_TABLE, LIBEXEC, TUNNEL_SPEC
from .profile import collapse

log = logging.getLogger("nm-sshuttle")

SSH_OPTIONS = ["-o", "BatchMode=yes", "-o", "ControlMaster=no", "-o", "ControlPath=none",
               "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=10",
               "-o", "ServerAliveCountMax=3"]
SEARCH_PATH = "/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin"
# Markers in the tunnel unit's journal that mean "the user has to act" (design §4.3).
AUTH_MARKERS = ("Permission denied", "Host key verification failed",
                "agent refused operation")
SSHUTTLE_TABLE_RE = re.compile(r"^sshuttle-ipv[46]-(\d+)$")


def bridge_command(user):
    return f"{os.path.join(LIBEXEC, 'nm-sshuttle')} ssh-as-user {user}"


def sshuttle_argv(spec, sshuttle):
    """sshuttle's command line for a tunnel spec written by the plugin."""
    subnets = list(spec["subnets"])
    argv = [sshuttle, "-v", "--method", spec.get("method", "nft"), "--disable-ipv6",
            "-r", spec["remote"], "-x", spec["first_hop"]]
    for net in spec.get("exclude", []):
        argv += ["-x", net]
    argv += ["-e", bridge_command(spec["user"])]
    servers = spec.get("dns_servers") or []
    if spec.get("dns", "none") != "none" and servers:
        # --to-ns pins the remote resolver and avoids the crash of lab T9.
        argv += ["--ns-hosts", ",".join(servers), "--to-ns", servers[0]]
        subnets += [f"{s}/32" for s in servers]   # DNS over TCP goes through too
    return argv + collapse(subnets)


def exec_tunnel():
    """ExecStart of the tunnel unit: exec sshuttle so that it is the main
    process and its own READY=1 counts."""
    with open(TUNNEL_SPEC) as f:
        spec = json.load(f)
    sshuttle = shutil.which("sshuttle", path=SEARCH_PATH)
    if not sshuttle:
        print("exec-tunnel: sshuttle not found", file=sys.stderr)
        return 1
    argv = sshuttle_argv(spec, sshuttle)
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


def first_hop(user, remote, ssh_g=ssh_g):
    """The first hop's IPv4 address, worked out as the user (design §2.2).

    A ProxyJump host may itself be an alias from the user's config, and may have
    a ProxyJump of its own, so follow the chain with 'ssh -G' as the user."""
    target = remote
    for _ in range(5):
        if target.startswith("-"):
            raise ValueError(f"unusable ssh destination {target!r}")
        hostname, jump = parse_ssh_g(ssh_g(user, target))
        if not jump:
            break
        target = jump
    else:
        raise ValueError("too many nested ProxyJump hosts; set 'gateway' in the profile")
    return socket.getaddrinfo(hostname, 22, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]


# ------------------------------------------------------------------ sweep
def listening_ports(proc_files=("/proc/net/tcp", "/proc/net/tcp6")):
    ports = set()
    for path in proc_files:
        try:
            with open(path) as f:
                next(f, None)
                for line in f:
                    fields = line.split()
                    if len(fields) > 3 and fields[3] == "0A":     # TCP_LISTEN
                        ports.add(int(fields[1].rsplit(":", 1)[1], 16))
        except OSError:
            pass
    return ports


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
        if m and (int(m.group(1)) in ports) == live:
            out.append(name)
    return out


def stale_tables(nft_list_tables, ports):
    """sshuttle tables whose redirect port has no listener (lab T7c)."""
    return sshuttle_tables(nft_list_tables, ports, live=False)


def nft_health():
    """What the health check reads from nft (design §4.4, "Health"): is the
    guard table there, and does a sshuttle table have a listener on its port?
    None when nft cannot be asked."""
    try:
        p = subprocess.run(["nft", "list", "tables"], capture_output=True, text=True,
                           timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode:
        return None
    return {"guard": GUARD_TABLE in inet_tables(p.stdout),
            "sshuttle": bool(sshuttle_tables(p.stdout, listening_ports(), live=True))}


def sweep():
    """Delete sshuttle tables left by a killed tunnel; they only black-hole traffic."""
    try:
        out = subprocess.run(["nft", "list", "tables"], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("sweep: cannot list nft tables: %s", e)
        return
    for table in stale_tables(out, listening_ports()):
        p = subprocess.run(["nft", "delete", "table", "inet", table], capture_output=True,
                           text=True, timeout=10)
        if p.returncode == 0:
            log.info("sweep: deleted stale table %s", table)
        else:
            log.warning("sweep: cannot delete %s: %s", table, p.stderr.strip())
