# SPDX-License-Identifier: MIT
"""Parse and validate a profile (design §4.2).

The profile reaches a root process from NetworkManager, and any local user
who can edit connections can write it, so everything is checked here and
nothing from it reaches a shell or ssh as an option.
"""

import ipaddress
import pwd
import re
from dataclasses import dataclass, field

REMOTE_RE = re.compile(r"^[A-Za-z0-9._@:%\[\]-]+$")
DOMAIN_RE = re.compile(r"^~?(\.|[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.?)$")
USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")   # also keeps it one word for sshuttle -e
KNOWN_KEYS = {"remote", "local-user", "subnets", "exclude", "dns", "dns-servers",
              "dns-domains", "method", "gateway", "probe", "fail-closed"}


class ProfileError(ValueError):
    pass


@dataclass(frozen=True)
class Profile:
    id: str
    uuid: str
    remote: str
    user: str
    subnets: list = field(default_factory=list)
    exclude: list = field(default_factory=list)
    dns: str = "none"
    dns_servers: list = field(default_factory=list)
    dns_domains: list = field(default_factory=list)
    method: str = "nft"
    gateway: str | None = None
    probe: str | None = None
    fail_closed: bool = True
    ignored_keys: list = field(default_factory=list)

    def tunnel_spec(self, first_hop):
        """What exec-tunnel needs, as JSON-friendly data."""
        return {"remote": self.remote, "user": self.user, "subnets": self.subnets,
                "exclude": self.exclude, "dns": self.dns, "dns_servers": self.dns_servers,
                "method": self.method, "first_hop": first_hop}

    def guarded_networks(self):
        """Networks the guard makes fail closed: the subnets plus the DNS servers."""
        nets = list(self.subnets)
        if self.dns != "none":
            nets += [f"{s}/32" for s in self.dns_servers]
        return collapse(nets)


def collapse(networks):
    """Merge overlapping and adjacent IPv4 networks, as strings.

    nft refuses an interval set with overlapping elements ("conflicting
    intervals"), and a DNS server's /32 usually lies inside a profile subnet."""
    nets = [ipaddress.ip_network(n, strict=False) for n in networks]
    return [str(n) for n in ipaddress.collapse_addresses(nets)]


def _split(value):
    return [v for v in re.split(r"[\s,]+", value or "") if v]


def _ipv4_networks(key, value):
    nets = []
    for item in _split(value):
        try:
            net = ipaddress.ip_network(item, strict=False)
        except ValueError:
            raise ProfileError(f"{key}: {item!r} is not a network") from None
        if net.version != 4:
            # sshuttle runs with --disable-ipv6 and the guard covers IPv4 only (design §4.5).
            raise ProfileError(f"{key}: {item} is IPv6, which is not supported yet")
        nets.append(str(net))
    return nets


def _ipv4(key, value):
    try:
        return str(ipaddress.IPv4Address(value))
    except ValueError:
        raise ProfileError(f"{key}: {value!r} is not an IPv4 address") from None


def _bool(key, value, default):
    if value is None or value == "":
        return default
    if value in ("yes", "true", "1"):
        return True
    if value in ("no", "false", "0"):
        return False
    raise ProfileError(f"{key}: expected yes or no, got {value!r}")


def parse(settings, getpwnam=pwd.getpwnam):
    """Validate the connection settings NetworkManager passes to Connect."""
    s_con = settings.get("connection", {})
    s_vpn = settings.get("vpn", {})
    s_ip4 = settings.get("ipv4", {})
    data = s_vpn.get("data", {})

    remote = data.get("remote", "")
    if not REMOTE_RE.match(remote) or remote.startswith("-"):
        raise ProfileError(f"remote: {remote!r} is not a valid ssh destination")

    user = data.get("local-user", "")
    if not USER_RE.match(user):
        raise ProfileError(f"local-user: {user!r} is not a plain user name")
    try:
        pw = getpwnam(user)
    except KeyError:
        raise ProfileError(f"local-user: {user!r} does not exist") from None
    if pw.pw_uid < 1000 or pw.pw_uid == 65534:
        raise ProfileError(f"local-user: {user!r} is not a regular user")

    perms = [p.rstrip(":") for p in s_con.get("permissions", [])]
    if perms != [f"user:{user}"]:
        raise ProfileError(f"connection.permissions must be exactly user:{user}, got {perms}")
    if not s_vpn.get("persistent", False):
        raise ProfileError("vpn.persistent must be yes")
    method4 = s_ip4.get("method", "auto")
    if method4 != "auto":
        raise ProfileError(f"ipv4.method must be auto, got {method4!r}")

    subnets = _ipv4_networks("subnets", data.get("subnets"))
    if not subnets:
        raise ProfileError("subnets: at least one network is required")
    exclude = _ipv4_networks("exclude", data.get("exclude"))

    dns = data.get("dns", "none")
    if dns not in ("none", "split", "all"):
        raise ProfileError(f"dns: expected none, split or all, got {dns!r}")
    servers = [_ipv4("dns-servers", s) for s in _split(data.get("dns-servers"))]
    domains = _split(data.get("dns-domains"))
    for d in domains:
        if not DOMAIN_RE.match(d):
            raise ProfileError(f"dns-domains: {d!r} is not a domain")
    if dns != "none" and not servers:
        raise ProfileError(f"dns-servers is required for dns={dns}")
    if dns == "split" and not domains:
        # nmss0 gets DefaultRoute=no, so without domains resolved never uses it.
        raise ProfileError("dns-domains is required for dns=split")
    if dns == "all":
        domains = ["~."]
    if dns == "none":
        servers, domains = [], []

    method = data.get("method", "nft")
    if method not in ("nft", "nat"):
        raise ProfileError(f"method: expected nft or nat, got {method!r}")

    gateway = data.get("gateway") or None
    if gateway:
        gateway = _ipv4("gateway", gateway)

    probe = data.get("probe") or None
    if probe:
        host, sep, port = probe.rpartition(":")
        if not sep or not port.isdigit() or not 0 < int(port) < 65536:
            raise ProfileError(f"probe: expected ADDRESS:PORT, got {probe!r}")
        probe = f"{_ipv4('probe', host)}:{int(port)}"

    return Profile(
        id=s_con.get("id", "?"), uuid=s_con.get("uuid", ""), remote=remote, user=user,
        subnets=subnets, exclude=exclude, dns=dns, dns_servers=servers,
        dns_domains=domains, method=method, gateway=gateway, probe=probe,
        fail_closed=_bool("fail-closed", data.get("fail-closed"), True),
        ignored_keys=sorted(set(data) - KNOWN_KEYS))
