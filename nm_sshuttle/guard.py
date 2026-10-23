# SPDX-License-Identifier: MIT
"""The dummy link nmssN and the fail-closed nft guard table (design §4.1, §4.6),
and the policy routing of method=tproxy, each named after the activation's slot
(docs/later/multiple-tunnels.md).

Each call is short and synchronous (one ip or nft run), so the plugin can make
it from the main loop, and the cleanup command can make it too.
"""

import logging
import subprocess

from .profile import collapse

from .const import TPROXY_MARK, TPROXY_PRIORITY, TPROXY_TABLE
from .tunnel import inet_tables

log = logging.getLogger("nm-sshuttle")


def run(argv, input=None, timeout=10):
    """Run a command; return (ok, output). Never raises."""
    try:
        p = subprocess.run(argv, input=input, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    return p.returncode == 0, (p.stderr or p.stdout).strip()


def link_ifindex(name):
    """The link's ifindex, or None when there is no such link."""
    try:
        with open(f"/sys/class/net/{name}/ifindex") as f:
            return int(f.read())
    except (OSError, ValueError):
        return None


def create_link(name):
    """Create the dummy link, replacing any leftover, and return its ifindex.

    NetworkManager sets the address itself (design §4.1)."""
    if link_ifindex(name) is not None:
        log.info("replacing a leftover %s", name)
        remove_link(name)
    ok, out = run(["ip", "link", "add", name, "type", "dummy"])
    if not ok:
        raise RuntimeError(f"cannot create {name}: {out}")
    ok, out = run(["ip", "link", "set", name, "up"])
    if not ok:
        remove_link(name)
        raise RuntimeError(f"cannot set {name} up: {out}")
    ifindex = link_ifindex(name)
    if ifindex is None:
        raise RuntimeError(f"{name} vanished right after it was created")
    return ifindex


def remove_link(name):
    if link_ifindex(name) is None:
        return True
    ok, out = run(["ip", "link", "del", name])
    if not ok and link_ifindex(name) is not None:
        log.warning("cannot remove %s: %s", name, out)
        return False
    return True


def guard_ruleset(table, networks, exclude, mark=None):
    """The guard table (design §4.6, docs/later/ipv6.md) as an nft script.

    sshuttle's redirect runs at priority dstnat, so its packets carry conntrack
    status dnat when they reach this output hook; everything else to the
    tunnelled networks is refused at once instead of leaking. Each family has
    its own pair of sets; the IPv6 ones are empty for an IPv4-only profile.

    With method=tproxy there is no NAT: sshuttle's mangle chain marks what it
    takes (priority mangle, before this hook), so a packet with that mark is
    let through (docs/later/tproxy.md)."""
    def set_def(name, items, version):
        # collapse() rebuilds each network from its number, so only address
        # characters reach the script, whatever text the profile held.
        items = collapse(items, version)
        body = f"type ipv{version}_addr; flags interval;"
        if items:
            body += f" elements = {{ {', '.join(items)} }}"
        return f"    set {name} {{ {body} }}"

    return "\n".join([
        f"table inet {table} {{",
        set_def("subnets4", networks, 4),
        set_def("exclude4", exclude, 4),
        set_def("subnets6", networks, 6),
        set_def("exclude6", exclude, 6),
        "    chain output {",
        "        type filter hook output priority filter; policy accept;",
        "        ip daddr @exclude4 accept",
        "        ip6 daddr @exclude6 accept",
        *([f"        ip daddr @subnets4 meta mark {mark:#010x} accept",
           f"        ip6 daddr @subnets6 meta mark {mark:#010x} accept"] if mark else []),
        "        ip daddr @subnets4 ct status dnat accept",
        "        ip6 daddr @subnets6 ct status dnat accept",
        "        ip daddr @subnets4 meta l4proto tcp reject with tcp reset",
        "        ip6 daddr @subnets6 meta l4proto tcp reject with tcp reset",
        "        ip daddr @subnets4 reject with icmpx admin-prohibited",
        "        ip6 daddr @subnets6 reject with icmpx admin-prohibited",
        "    }",
        "}",
        "",
    ])


def install_guard(table, networks, exclude, mark=None):
    """Install (or replace) the guard table in one nft transaction."""
    script = f"table inet {table}\ndelete table inet {table}\n"
    script += guard_ruleset(table, networks, exclude, mark)
    ok, out = run(["nft", "-f", "-"], input=script)
    if not ok:
        # Only the line that names the error (a warning may come before it):
        # the lines after it quote the script, or a file that the script included.
        lines = out.splitlines() or ["nft failed"]
        first = next((line for line in lines if "Error:" in line), lines[0])
        raise RuntimeError(f"cannot install the guard table: {first}")


def guard_present(table):
    return run(["nft", "list", "table", "inet", table])[0]


def guard_listed(table):
    """Whether the guard table is there, from 'nft list tables'. Unlike
    guard_present, None when nft cannot be asked, which is not "gone"."""
    try:
        p = subprocess.run(["nft", "list", "tables"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return None if p.returncode else table in inet_tables(p.stdout)


def remove_guard(table):
    if not guard_present(table):
        return True
    ok, out = run(["nft", "delete", "table", "inet", table])
    if not ok and guard_present(table):
        log.warning("cannot remove the guard table %s: %s", table, out)
        return False
    return True


# ------------------------------------------------------- policy routing
# method=tproxy: packets sshuttle marked must be routed to lo, where TPROXY
# hands them to its listener. sshuttle leaves that to the user; the plugin
# owns it and removes it by its exact values, never flushing a table or rules
# it does not own (docs/later/tproxy.md).
def tproxy_mark(slot):
    return TPROXY_MARK + slot


def _ip(family):
    return ["ip", "-6"] if family == 6 else ["ip", "-4"]


def _rule(slot):
    return ["priority", str(TPROXY_PRIORITY + slot),
            "fwmark", f"{tproxy_mark(slot):#x}/0xffffffff", "lookup", str(TPROXY_TABLE + slot)]


def _local_route(slot):
    return ["local", "default", "dev", "lo", "table", str(TPROXY_TABLE + slot)]


def _routing_parts(family, slot):
    """(rule there, route there) for one family."""
    ok, rules = run(_ip(family) + ["rule", "list"])
    rule = ok and any(
        line.startswith(f"{TPROXY_PRIORITY + slot}:")
        and f"fwmark {tproxy_mark(slot):#x}" in line
        and f"lookup {TPROXY_TABLE + slot}" in line
        for line in rules.splitlines())
    ok, routes = run(_ip(family) + ["route", "show", "table", str(TPROXY_TABLE + slot)])
    route = ok and any(line.startswith("local default dev lo") for line in routes.splitlines())
    return rule, route


def policy_routing_present(families, slot):
    """Whether the rule and the route are there for every one of families."""
    return all(all(_routing_parts(f, slot)) for f in families)


def install_policy_routing(families, slot):
    """Install the rule and the route for each family, replacing any copy."""
    for family in families:
        for _ in range(10):         # "ip rule add" twice makes two rules
            if not run(_ip(family) + ["rule", "del", *_rule(slot)])[0]:
                break
        ok, out = run(_ip(family) + ["rule", "add", *_rule(slot)])
        if ok:
            ok, out = run(_ip(family) + ["route", "replace", *_local_route(slot)])
        if not ok:
            raise RuntimeError(f"cannot install the policy routing for IPv{family}: {out}")


def remove_policy_routing(slot):
    """Remove the rule and the route for both families; True when none is left."""
    clean = True
    for family in (4, 6):
        for _ in range(10):
            if not run(_ip(family) + ["rule", "del", *_rule(slot)])[0]:
                break
        if _routing_parts(family, slot)[1]:
            run(_ip(family) + ["route", "del", *_local_route(slot)])
        if any(_routing_parts(family, slot)):
            log.warning("cannot remove the policy routing for IPv%d", family)
            clean = False
    return clean
