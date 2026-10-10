# SPDX-License-Identifier: MIT
"""The dummy link nmss0 and the fail-closed nft guard table (design §4.1, §4.6).

Each call is short and synchronous (one ip or nft run), so the plugin can make
it from the main loop, and the cleanup command can make it too.
"""

import logging
import subprocess

from .profile import collapse

from .const import GUARD_TABLE, LINK

log = logging.getLogger("nm-sshuttle")


def run(argv, input=None, timeout=10):
    """Run a command; return (ok, output). Never raises."""
    try:
        p = subprocess.run(argv, input=input, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    return p.returncode == 0, (p.stderr or p.stdout).strip()


def link_ifindex(name=LINK):
    """The link's ifindex, or None when there is no such link."""
    try:
        with open(f"/sys/class/net/{name}/ifindex") as f:
            return int(f.read())
    except (OSError, ValueError):
        return None


def create_link(name=LINK):
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


def remove_link(name=LINK):
    if link_ifindex(name) is None:
        return True
    ok, out = run(["ip", "link", "del", name])
    if not ok and link_ifindex(name) is not None:
        log.warning("cannot remove %s: %s", name, out)
        return False
    return True


def guard_ruleset(networks, exclude):
    """The guard table (design §4.6) as an nft script.

    sshuttle's redirect runs at priority dstnat, so its packets carry conntrack
    status dnat when they reach this output hook; everything else to the
    tunnelled networks is refused at once instead of leaking."""
    def elements(items):
        return ", ".join(items) if items else ""

    def set_def(name, items):
        items = collapse(items)
        body = "type ipv4_addr; flags interval;"
        if items:
            body += f" elements = {{ {elements(items)} }}"
        return f"    set {name} {{ {body} }}"

    return "\n".join([
        f"table inet {GUARD_TABLE} {{",
        set_def("subnets4", networks),
        set_def("exclude4", exclude),
        "    chain output {",
        "        type filter hook output priority filter; policy accept;",
        "        ip daddr @exclude4 accept",
        "        ip daddr @subnets4 ct status dnat accept",
        "        ip daddr @subnets4 meta l4proto tcp reject with tcp reset",
        "        ip daddr @subnets4 reject with icmp admin-prohibited",
        "    }",
        "}",
        "",
    ])


def install_guard(networks, exclude):
    """Install (or replace) the guard table in one nft transaction."""
    script = f"table inet {GUARD_TABLE}\ndelete table inet {GUARD_TABLE}\n"
    script += guard_ruleset(networks, exclude)
    ok, out = run(["nft", "-f", "-"], input=script)
    if not ok:
        raise RuntimeError(f"cannot install the guard table: {out}")


def guard_present():
    return run(["nft", "list", "table", "inet", GUARD_TABLE])[0]


def remove_guard():
    if not guard_present():
        return True
    ok, out = run(["nft", "delete", "table", "inet", GUARD_TABLE])
    if not ok and guard_present():
        log.warning("cannot remove the guard table: %s", out)
        return False
    return True
