# SPDX-License-Identifier: MIT
"""What an active profile claims, and the rules that keep two active profiles
from fighting over traffic or DNS (docs/later/multiple-tunnels.md, "Conflicts
between active profiles").

Each plugin instance writes its claim to its slot's claim.json while it holds
the claims lock, after reading the others' claims; a claim counts while the
process it names, by PID and start time, is a running plugin.
"""

import fcntl
import ipaddress
import json
import logging
import os

from .const import CLAIMS_LOCK, SLOTS, STATE_DIR, Slot

log = logging.getLogger("nm-sshuttle")


def effective_networks(networks, exclude):
    """networks minus exclude, as a sorted list of network strings, both families."""
    out = []
    excl = [ipaddress.ip_network(e, strict=False) for e in exclude]
    for text in networks:
        parts = [ipaddress.ip_network(text, strict=False)]
        for ex in excl:
            nxt = []
            for net in parts:
                if net.version != ex.version or not net.overlaps(ex):
                    nxt.append(net)
                elif not ex.supernet_of(net):
                    nxt.extend(net.address_exclude(ex))
            parts = nxt
        out.extend(parts)
    return [str(n) for n in sorted(ipaddress.collapse_addresses(n for n in out if n.version == 4))
            ] + [str(n) for n in sorted(ipaddress.collapse_addresses(
                n for n in out if n.version == 6))]


def make_claim(profile, hops, pid=None):
    """The claim of an activation: what the conflict rules compare. The
    instance adds its PID and start time when it takes it."""
    return {"id": profile.id, "uuid": profile.uuid, "pid": pid,
            "networks": effective_networks(profile.guarded_networks(), profile.exclude),
            "dns": profile.dns,
            "domains": sorted({d.lstrip("~").lower().rstrip(".") for d in profile.dns_domains}
                              if profile.dns != "none" else set()),
            "first_hop": list(hops)}


def _nets(claim):
    return [ipaddress.ip_network(n) for n in claim.get("networks", [])]


def _overlap(a, b):
    for x in _nets(a):
        for y in _nets(b):
            if x.version == y.version and x.overlaps(y):
                return x if x.prefixlen >= y.prefixlen else y
    return None


def _runs_over(a, b):
    """a's first hop lies in b's effective networks: a's ssh goes through b."""
    for hop in a.get("first_hop", []):
        try:
            addr = ipaddress.ip_address(hop)
        except ValueError:
            continue
        if any(addr.version == n.version and addr in n for n in _nets(b)):
            return True
    return False


def conflict(mine, others):
    """Why mine cannot be active next to others, or None."""
    for other in others:
        name = other.get("id") or other.get("uuid")
        net = _overlap(mine, other)
        if net is not None:
            return f"subnets overlap with the active profile {name}: {net}"
        if mine.get("dns") == "all" and other.get("dns") == "all":
            return f"the active profile {name} also uses dns=all"
        same = sorted(set(mine.get("domains", [])) & set(other.get("domains", [])))
        if same:
            return f"the active profile {name} also has the DNS domain {same[0]}"
    loop = first_hop_loop(mine, others)
    if loop:
        return "the first hops would run in a loop: " + " -> ".join(loop)
    return None


def first_hop_loop(mine, others):
    """The names along a loop of first hops through mine, or None."""
    nodes = [mine] + list(others)

    def visit(i, path):
        for j, other in enumerate(nodes):
            if j == i or not _runs_over(nodes[i], other):
                continue
            if j == 0:
                return path + [0]
            if j not in path:
                found = visit(j, path + [j])
                if found:
                    return found
        return None
    loop = visit(0, [0])
    return [nodes[i].get("id") or nodes[i].get("uuid") or "?" for i in loop] if loop else None


def process_start(pid, proc="/proc"):
    """When the process started, in clock ticks since boot (field 22 of
    /proc/PID/stat), or None. With the PID it names one process for good."""
    try:
        with open(f"{proc}/{int(pid)}/stat") as f:
            text = f.read()
        # the command name in field 2 may hold spaces and parentheses
        return int(text[text.rindex(")") + 2:].split()[19])
    except (OSError, ValueError, TypeError, IndexError):
        return None


def runs_as_root(pid, proc="/proc"):
    """Whether every user ID of the process is 0 (/proc/PID/status). A
    setuid-root program such as sudo has euid 0 at once, but keeps its
    caller's real user ID until it has checked a password."""
    try:
        with open(f"{proc}/{int(pid)}/status") as f:
            for line in f:
                if line.startswith("Uid:"):
                    return line.split()[1:] == ["0"] * 4
    except (OSError, ValueError, TypeError):
        pass
    return False


def _alive(pid, start=None, proc="/proc"):
    """pid is a running plugin instance: with the start time the claim
    recorded, so that another process that got the PID later never counts.
    A claim from before start times (an upgrade) needs a process that runs as
    root, not only one with euid 0, with nm-sshuttle-service in its command
    line."""
    try:
        with open(f"{proc}/{int(pid)}/cmdline", "rb") as f:
            if b"nm-sshuttle-service" not in f.read():
                return False
    except (OSError, ValueError, TypeError):
        return False
    if not runs_as_root(pid, proc):
        return False
    return start is None or process_start(pid, proc) == start


def read_claims(skip, alive=_alive, slots=range(SLOTS)):
    """The claims of the other slots' running instances."""
    out = []
    for n in slots:
        if n == skip:
            continue
        try:
            with open(Slot(n).claim) as f:
                claim = json.load(f)
        except (OSError, ValueError):
            continue
        if alive(claim.get("pid"), claim.get("start")):
            out.append(claim)
    return out


def take(slot, claim, alive=_alive):
    """Check claim against the other active profiles and, if it fits, write it
    for slot. Returns the conflict, or None."""
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    with open(CLAIMS_LOCK, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        why = conflict(claim, read_claims(slot, alive))
        if why:
            return why
        path = Slot(slot).claim
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(claim, f)
        os.replace(tmp, path)
    return None


def release(slot):
    try:
        os.remove(Slot(slot).claim)
    except FileNotFoundError:
        pass
    except OSError as e:
        log.warning("cannot remove the claim of slot %d: %s", slot, e)
