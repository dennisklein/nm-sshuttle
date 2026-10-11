# Designs for the "Later" items

The [design](../design.md)'s roadmap (§7) ends with work left out of v1.
Each item has a design here. None is implemented. They build on the
architecture of §4 and change nothing in it unless they say so.

| Design | What it adds | Size |
|---|---|---|
| [IPv6 subnets and guard sets](ipv6.md) | IPv6 networks in `subnets` and `exclude`, tunnelled and failing closed; an IPv6 first hop | about 150 lines of Python |
| [UDP through `method=tproxy`](tproxy.md) | UDP to the tunnelled networks, through sshuttle's `tproxy` method and policy routing the plugin owns | about 120 lines of Python |
| [Several tunnels at once](multiple-tunnels.md) | Up to 8 active profiles, one plugin process and tunnel unit per activation, refusing profiles that conflict | about 400 lines of Python |
| [GTK4 editor plugin for GNOME Settings](gnome-editor.md) | Creating and editing profiles in GNOME Settings | about 600 lines of C, in a separate package |

## Order

The designs share the guard, the profile keys and the names of what an
activation creates, so the order matters:

1. **IPv6** first. It turns the guard and the profile's networks into
   per-family code, which the other two Python designs then extend
   instead of changing twice.
2. **tproxy** next. It adds per-method guard rules, policy routing and a
   per-method health check, and needs IPv6's per-family code for IPv6
   UDP. Its first step is lab test T15 (DNS through `tproxy` with
   `IP_UNICAST_IF`), which decides whether `dns=split` works with it.
3. **Several tunnels** after both, so that slot numbers go into the
   guard, the routing and the health check once. Its first step is the VM
   check that two links can both carry 192.0.0.8.
4. **The editor** is independent of the others, but should follow them so
   that its form has the final set of profile keys (IPv6 networks,
   `method=tproxy`). It can start earlier if the form is extended later.

## What stays out

- An IPv6 address or IPv6 DNS servers on `nmss0` (see the IPv6 design for
  why).
- Nested subnets that follow which tunnel is up at run time (see the
  multiple tunnels design).
- The upstream hook for an externally provided firewall helper (§6,
  §4.8). It is an upstream proposal to sshuttle, not work in this
  repository.
