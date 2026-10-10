# Research lab

`run.sh` checks the sshuttle and kernel behaviour that
[`docs/design.md`](../docs/design.md) builds on. It sets up this topology with
network namespaces:

```
host --198.51.100.0/24--> jump (sshd) --10.99.0.0/24--> internal (DNS .53, HTTP .10)
```

It then runs sshuttle the way the design proposes, as root with `ssh` running
as an unprivileged user via [`ssh-as-user`](ssh-as-user). It prints PASS/FAIL
for each claim and tears everything down again.

## Running it

Run it **only in a throwaway VM or container, as root**. It creates network
namespaces, veth links, nft tables and a local user (`nmss-lab`), and it
changes the address of its own veth link.

```console
# apt install iproute2 nftables openssh-server openssh-client curl python3
# pip install sshuttle            # or the distro package
# SSHUTTLE=$(command -v sshuttle) ./lab/run.sh
```

It takes about two minutes, most of it in the roaming test (T10). It exits
non-zero if any check fails.

## What it checks

| Test | Claim |
|---|---|
| T1 | Root sshuttle with ssh as the user carries TCP. The host alias exists only in the user's `~/.ssh/config`, and the passphrase-protected key is in the user's agent. |
| T2 | sshuttle 2.0.0's automatic remote exclusion fails when the client is root (`ssh -G` reads root's config). |
| T3 | DNS to the internal server is captured, also when pinned to the per-tunnel link with `IP_UNICAST_IF` as systemd-resolved does. |
| T4 | The nft guard table lets redirected traffic through. With the tunnel down, it refuses traffic to the tunnelled networks at once instead of leaking it. |
| T5 | SIGTERM makes sshuttle remove its rules. |
| T6 | A quick restart finds the old redirect port in TIME_WAIT and picks a new port, so rule table names change. |
| T7 | SIGKILL leaves a stale table that refuses connections; a sweeper keyed on "no listener on the port" removes it. |
| T8 | A fixed `--listen` port cannot be reused within TIME_WAIT (`EADDRINUSE`). |
| T9 | An unreachable resolver on the remote side kills the tunnel on the first forwarded DNS query; `--to-ns` avoids it. |
| T10 | After the local address changes, the tunnel stays "running" but dead without ssh keepalives, and exits in about 17 s with `ServerAliveInterval=5`/`CountMax=2`. The user's `ssh` outlives sshuttle's exit unless something kills the whole process group. |

The per-tunnel dummy link is replaced by a veth whose peer sits in an empty
namespace, so the lab also runs on kernels built without the `dummy` module.
For these tests the two behave the same.
