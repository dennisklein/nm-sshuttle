#!/bin/bash
# SPDX-License-Identifier: MIT
#
# Research lab for the nm-sshuttle design (docs/design.md, "Lab results").
# Builds a throwaway topology with network namespaces, runs sshuttle the
# way the design proposes, prints PASS/FAIL per claim, and tears it down.
#
#   host --198.51.100.0/24--> jump (sshd) --10.99.0.0/24--> internal
#                                                          (DNS .53, HTTP .10)
#
# The host has no route to 10.99.0.0/24; only the tunnel reaches it.
#
# Where the kernel has IPv6, the same links also carry 2001:db8:51::/64 and
# 2001:db8:99::/64 (HTTP on ::10). The host has a route to 2001:db8:99::/64
# through the jump host, so that a leak would actually arrive (T11-T13).
# T15-T19 (method=tproxy) add the same route for 10.99.0.0/24, and a UDP echo
# server on 10.99.0.10:7777.
#
# Run as root on a disposable VM or container. It creates network
# namespaces, veth links, nft tables and a local user, and it changes the
# address of its own veth link. Needs: iproute2 (ip, ss), nft, sshd, ssh,
# ssh-agent, setpriv, curl, python3 and sshuttle (set SSHUTTLE=/path if it
# is not on PATH); iptables and ip6tables for T15-T19. The kill tests match processes whose first argument is
# the sshuttle path, so do not run other sshuttle instances meanwhile.
set -uo pipefail

LAB_SRC=$(cd "$(dirname "$0")" && pwd)
SSHUTTLE=${SSHUTTLE:-$(command -v sshuttle || true)}
LAB_USER=${LAB_USER:-nmss-lab}
JUMP_IP=198.51.100.1
HOST_IP=198.51.100.2
ROAM_IP=198.51.100.3
JUMP_IP6=2001:db8:51::1
HOST_IP6=2001:db8:51::2
INTERNAL_IP6=2001:db8:99::10
TUNNEL_LINK=nmss0     # stands in for the per-tunnel dummy link
GUARD_TABLE=nm-sshuttle-guard-0     # slot 0 (docs/later/multiple-tunnels.md)
REPO=$(cd "$(dirname "$0")/.." && pwd)
# T11-T13 need IPv6 in the kernel.
HAVE_IPV6=no
[ -e /proc/net/if_inet6 ] && HAVE_IPV6=yes
LO_IPV6_OFF=$(cat /proc/sys/net/ipv6/conf/lo/disable_ipv6 2>/dev/null || echo 1)

[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 2; }
[ -x "$SSHUTTLE" ] || { echo "sshuttle not found; set SSHUTTLE=" >&2; exit 2; }

WORK=$(mktemp -d /tmp/nmss-lab.XXXXXX)
# sshuttle tells systemd it is ready once its rules are in place. Nothing
# listens here, so with -v that shows in its log as "Error notifying systemd".
export NOTIFY_SOCKET=$WORK/notify
chmod 755 "$WORK"
cp "$LAB_SRC/ssh-as-user" "$LAB_SRC/dns_query.py" "$LAB_SRC/dns_server.py" \
    "$LAB_SRC/udp_echo.py" "$WORK/"
chmod 755 "$WORK"/*
AGENT_SOCK=$WORK/agent/agent.sock

PASS=0
FAIL=0
SKIP=0
pass() { echo "PASS  $*"; PASS=$((PASS + 1)); }
fail() { echo "FAIL  $*"; FAIL=$((FAIL + 1)); }
skip() { echo "SKIP  $*"; SKIP=$((SKIP + 1)); }
check() { local name=$1; shift; if "$@"; then pass "$name"; else fail "$name"; fi; }
# Run Python against the plugin's own code in this checkout.
plugin() { PYTHONPATH=$REPO python3 -c "$1"; }

# PIDs of sshuttle clients and firewall helpers started from $SSHUTTLE
# ("python3 $SSHUTTLE ..."). Exact match on argv[1], so the shell that runs
# this script is never matched.
sshuttle_pids() {
    ps -eo pid=,args= | awk -v p="$SSHUTTLE" '$3 == p {print $1}'
}

teardown() {
    set +e
    sshuttle_pids | xargs -r kill -TERM 2>/dev/null
    sleep 1
    sshuttle_pids | xargs -r kill -KILL 2>/dev/null
    for t in $(nft list tables 2>/dev/null | awk '$3 ~ /^sshuttle-ipv[46]-[0-9]+$/ {print $3}'); do
        nft delete table inet "$t"
    done
    nft delete table inet "$GUARD_TABLE" 2>/dev/null
    nft delete table inet nmss-lab-count 2>/dev/null
    plugin 'from nm_sshuttle import guard, tunnel
tunnel.sweep(); guard.remove_policy_routing(0)' 2>/dev/null
    ip route del 10.99.0.0/24 via $JUMP_IP 2>/dev/null
    pkill -f "$WORK/udp_echo.py" 2>/dev/null
    [ -f "$WORK/sshd.pid" ] && kill "$(cat "$WORK/sshd.pid")" 2>/dev/null
    pkill -f "$WORK/dns_server.py" 2>/dev/null
    pkill -f "http.server --bind" 2>/dev/null
    [ "$HAVE_IPV6" = yes ] && sysctl -qw net.ipv6.conf.lo.disable_ipv6="$LO_IPV6_OFF"
    pkill -KILL -u "$LAB_USER" 2>/dev/null
    ip link del h-j 2>/dev/null
    ip link del "$TUNNEL_LINK" 2>/dev/null
    for n in jump internal void; do ip netns del "$n" 2>/dev/null; done
    rm -rf /etc/netns/jump "$WORK"
    if id "$LAB_USER" >/dev/null 2>&1; then
        for _ in 1 2 3 4 5; do userdel -r "$LAB_USER" 2>/dev/null && break; sleep 1; done
    fi
}
trap teardown EXIT

# ---------------------------------------------------------------- topology
setup() {
    set -e  # any failure here exits; the EXIT trap tears down
    ip netns add jump
    ip netns add internal
    ip netns add void

    ip link add h-j type veth peer name j-h
    ip link set j-h netns jump
    ip addr add $HOST_IP/24 dev h-j
    ip link set h-j up
    ip -n jump addr add $JUMP_IP/24 dev j-h
    ip -n jump link set j-h up
    ip -n jump link set lo up

    ip -n jump link add j-i type veth peer name i-j
    ip -n jump link set i-j netns internal
    ip -n jump addr add 10.99.0.1/24 dev j-i
    ip -n jump link set j-i up
    ip -n internal addr add 10.99.0.10/24 dev i-j
    ip -n internal addr add 10.99.0.53/24 dev i-j
    ip -n internal link set i-j up
    ip -n internal link set lo up
    ip -n internal route add default via 10.99.0.1
    ip netns exec jump sysctl -qw net.ipv4.ip_forward=1
    if [ "$HAVE_IPV6" = yes ]; then
        ip -6 addr add $HOST_IP6/64 dev h-j nodad
        ip -n jump -6 addr add $JUMP_IP6/64 dev j-h nodad
        ip -n jump -6 addr add 2001:db8:99::1/64 dev j-i nodad
        ip -n internal -6 addr add $INTERNAL_IP6/64 dev i-j nodad
        ip -n internal -6 route add default via 2001:db8:99::1
        ip netns exec jump sysctl -qw net.ipv6.conf.all.forwarding=1
        ip -6 route add 2001:db8:99::/64 via $JUMP_IP6
    fi

    # Per-tunnel link. The design uses a dummy link; a veth whose peer sits
    # in an empty namespace behaves the same here (up, carrier, global /32)
    # and also works on kernels built without the dummy module.
    ip link add $TUNNEL_LINK type veth peer name nmss0-peer
    ip link set nmss0-peer netns void
    ip -n void link set nmss0-peer up
    ip addr add 192.0.0.8/32 dev $TUNNEL_LINK
    ip link set $TUNNEL_LINK up

    # The remote side resolves through the internal DNS server. Must exist
    # before sshd starts: "ip netns exec" bind-mounts it at exec time.
    mkdir -p /etc/netns/jump /run/sshd
    echo "nameserver 10.99.0.53" > /etc/netns/jump/resolv.conf

    ssh-keygen -q -t ed25519 -N '' -f "$WORK/host_key"
    cat > "$WORK/sshd_config" <<EOF
ListenAddress $JUMP_IP
HostKey $WORK/host_key
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
UsePAM no
PidFile $WORK/sshd.pid
EOF
    if [ "$HAVE_IPV6" = yes ]; then echo "ListenAddress $JUMP_IP6" >> "$WORK/sshd_config"; fi
    ip netns exec jump /usr/sbin/sshd -f "$WORK/sshd_config" -E "$WORK/sshd.log"

    ip netns exec internal python3 "$WORK/dns_server.py" 10.99.0.53 > "$WORK/dns.log" 2>&1 &
    ip netns exec internal python3 -m http.server --bind 10.99.0.10 8080 \
        --directory "$WORK" > "$WORK/http.log" 2>&1 &
    local i http=1
    if [ "$HAVE_IPV6" = yes ]; then
        ip netns exec internal python3 -m http.server --bind $INTERNAL_IP6 8080 \
            --directory "$WORK" > "$WORK/http6.log" 2>&1 &
        http=2
    fi
    for i in $(seq 1 50); do
        [ "$(ss -N internal -ltnH 'sport = :8080' | wc -l)" -ge $http ] &&
            ss -N internal -lunH 'sport = :53' | grep -q . && break
        sleep 0.2
    done

    # Session user: passphrase-protected key, unlocked into the user's own
    # agent (stands in for gcr-ssh-agent / ssh-agent in the GNOME session).
    useradd -m -s /bin/bash "$LAB_USER"
    usermod -p '*' "$LAB_USER"           # no password, but not locked
    local home
    home=$(getent passwd "$LAB_USER" | cut -d: -f6)
    install -d -m 700 -o "$LAB_USER" -g "$LAB_USER" "$home/.ssh"
    runuser -u "$LAB_USER" -- ssh-keygen -q -t ed25519 -N 'lab-passphrase' -f "$home/.ssh/id_ed25519"
    install -m 600 -o "$LAB_USER" -g "$LAB_USER" "$home/.ssh/id_ed25519.pub" "$home/.ssh/authorized_keys"
    printf '#!/bin/sh\necho lab-passphrase\n' > "$WORK/askpass"
    chmod 755 "$WORK/askpass"
    install -d -m 700 -o "$LAB_USER" -g "$LAB_USER" "$WORK/agent"
    runuser -u "$LAB_USER" -- ssh-agent -a "$AGENT_SOCK" > /dev/null
    runuser -u "$LAB_USER" -- env SSH_AUTH_SOCK="$AGENT_SOCK" SSH_ASKPASS="$WORK/askpass" \
        SSH_ASKPASS_REQUIRE=force ssh-add -q "$home/.ssh/id_ed25519" < /dev/null
    USER_SSH_CONFIG=$home/.ssh/config
    write_ssh_config keepalive
    sleep 1
    set +e
}

# The alias "corp" only exists in the user's ssh config, as in real use.
write_ssh_config() {
    {
        echo "Host corp"
        echo "    HostName $JUMP_IP"
        echo "    User $LAB_USER"
        echo "    StrictHostKeyChecking accept-new"
        if [ "$1" = keepalive ]; then
            echo "    ServerAliveInterval 5"
            echo "    ServerAliveCountMax 2"
        fi
        # The same jump host by its IPv6 address only (T13).
        echo "Host corp6"
        echo "    HostName $JUMP_IP6"
        echo "    User $LAB_USER"
        echo "    StrictHostKeyChecking accept-new"
    } > "$USER_SSH_CONFIG"
    chown "$LAB_USER:" "$USER_SSH_CONFIG"
}

# ---------------------------------------------------------------- helpers
# start_tunnel LOG [extra sshuttle args...]  -> prints PID
start_tunnel() {
    local log=$1
    shift
    "$SSHUTTLE" -v --disable-ipv6 --method nft \
        -e "$WORK/ssh-as-user $LAB_USER $AGENT_SOCK" -r corp \
        -x $JUMP_IP "$@" 10.99.0.0/24 > "$log" 2>&1 &
    echo $!
}

# start_tunnel6 LOG REMOTE [extra sshuttle args...]  -> prints PID
# IPv6 on: no --disable-ipv6, both families of the internal network.
start_tunnel6() {
    local log=$1 remote=$2
    shift 2
    "$SSHUTTLE" -v --method nft \
        -e "$WORK/ssh-as-user $LAB_USER $AGENT_SOCK" -r "$remote" \
        -x $JUMP_IP -x $JUMP_IP6 "$@" 10.99.0.0/24 2001:db8:99::/64 > "$log" 2>&1 &
    echo $!
}

wait_connected() {  # until the tunnel is up and sshuttle's rules are in place
    local i
    for i in $(seq 1 30); do
        grep -q "Connected to server" "$1" && grep -q "Error notifying systemd" "$1" &&
            return 0
        sleep 0.5
    done
    return 1
}

wait_exit() {  # wait_exit PID SECONDS
    local i
    for i in $(seq 1 "$2"); do
        kill -0 "$1" 2>/dev/null || return 0
        sleep 1
    done
    return 1
}

wait_gone() {  # wait_gone SECONDS: until no client or firewall helper is left
    local i
    for i in $(seq 1 "$1"); do
        [ -z "$(sshuttle_pids)" ] && return 0
        sleep 1
    done
    return 1
}

redirect_port() {  # redirect_port LOG -> TCP port sshuttle listened on
    sed -n "s/.*TCP redirector listening on ('127.0.0.1', \([0-9]*\)).*/\1/p" "$1"
}

# Prints the HTTP status, or 000 if no response arrived within the timeout.
http_code() {
    curl -g -s -m "${2:-5}" -o /dev/null -w '%{http_code}' --noproxy '*' "$1"
}

sshuttle_tables() {
    nft list tables | awk '$3 ~ /^sshuttle-ipv[46]-[0-9]+$/ {print $3}'
}

# The sweeper proposed for ExecStartPre/ExecStopPost: an sshuttle table whose
# redirect port has no listener is stale (it can only black-hole traffic).
sweep_stale_tables() {
    local t port
    for t in $(sshuttle_tables); do
        port=${t##*-}
        ss -ltnH "sport = :$port" | grep -q . || nft delete table inet "$t"
    done
}

# The plugin's guard (nm_sshuttle/guard.py), for both families.
install_guard() {
    nft -f - <<EOF
table inet $GUARD_TABLE {
    set subnets4 { type ipv4_addr; flags interval; elements = { 10.99.0.0/24 } }
    set exclude4 { type ipv4_addr; flags interval; elements = { $JUMP_IP } }
    set subnets6 { type ipv6_addr; flags interval; elements = { 2001:db8:99::/64 } }
    set exclude6 { type ipv6_addr; flags interval; elements = { $JUMP_IP6 } }
    chain output {
        type filter hook output priority filter; policy accept;
        ip daddr @exclude4 accept
        ip6 daddr @exclude6 accept
        ip daddr @subnets4 ct status dnat accept
        ip6 daddr @subnets6 ct status dnat accept
        ip daddr @subnets4 meta l4proto tcp reject with tcp reset
        ip6 daddr @subnets6 meta l4proto tcp reject with tcp reset
        ip daddr @subnets4 reject with icmpx admin-prohibited
        ip6 daddr @subnets6 reject with icmpx admin-prohibited
    }
}
EOF
}

# The internal web server logged a request from ADDRESS: log_from LOG ADDRESS
log_from() { grep -q "^$2 " "$1"; }

# ------------------------------------------------------------------ tests
echo "== setting up lab in $WORK"
setup
"$SSHUTTLE" --version | sed 's/^/sshuttle /'

echo "== T1 tunnel: sshuttle as root, ssh as the session user"
PID=$(start_tunnel "$WORK/t1.log" --ns-hosts 10.99.0.53)
check "T1 tunnel comes up (ssh as $LAB_USER, key from user agent)" wait_connected "$WORK/t1.log"
check "T1 TCP to internal host through the tunnel" test "$(http_code http://10.99.0.10:8080/)" = 200

echo "== T2 sshuttle's own remote-IP auto-exclusion when the client is root"
check "T2 auto-exclusion fails for a user-only ssh alias (ssh -G ran as root)" \
    grep -q "Failed to exclude remote IP" "$WORK/t1.log"

echo "== T3 split DNS on the per-tunnel link"
check "T3a DNS to the internal server is captured" \
    test "$(python3 "$WORK/dns_query.py" 10.99.0.53 git.corp.test)" = "ANSWER 10.99.0.10"
check "T3b same query pinned to the tunnel link (IP_UNICAST_IF, as resolved does) is captured" \
    test "$(python3 "$WORK/dns_query.py" 10.99.0.53 git.corp.test $TUNNEL_LINK)" = "ANSWER 10.99.0.10"
check "T3c the query reached the internal server from the jump host" \
    grep -q "query from 10.99.0.1: git.corp.test" "$WORK/dns.log"

echo "== T4 fail-closed guard table"
install_guard
check "T4a guard lets redirected TCP through" test "$(http_code http://10.99.0.10:8080/)" = 200
check "T4b guard lets redirected DNS through" \
    test "$(python3 "$WORK/dns_query.py" 10.99.0.53 git.corp.test)" = "ANSWER 10.99.0.10"
check "T4c excluded jump host stays reachable directly" \
    "$WORK/ssh-as-user" "$LAB_USER" "$AGENT_SOCK" corp true

echo "== T5 SIGTERM (systemctl stop) removes sshuttle's rules"
kill -TERM "$PID"
check "T5a sshuttle exits on SIGTERM" wait_exit "$PID" 10
check "T5b no sshuttle tables left" test -z "$(sshuttle_tables)"
START=$(date +%s%N)
CODE=$(http_code http://10.99.0.10:8080/ 4)
ELAPSED_MS=$(( ($(date +%s%N) - START) / 1000000 ))
check "T4d tunnel down + guard: TCP refused at once (${ELAPSED_MS} ms), not leaked" \
    test "$CODE" = 000 -a "$ELAPSED_MS" -lt 1000
check "T4e tunnel down + guard: DNS to internal server blocked locally" \
    test "$(python3 "$WORK/dns_query.py" 10.99.0.53 git.corp.test)" = "ERROR Operation not permitted"
nft delete table inet $GUARD_TABLE

echo "== T6 quick restart: TIME_WAIT on the old redirect port"
OLD_PORT=$(redirect_port "$WORK/t1.log")
check "T6a old redirect port $OLD_PORT is in TIME_WAIT" \
    test -n "$(ss -tanH state time-wait "( sport = :$OLD_PORT )")"
PID=$(start_tunnel "$WORK/t6.log" --ns-hosts 10.99.0.53)
wait_connected "$WORK/t6.log"
NEW_PORT=$(redirect_port "$WORK/t6.log")
check "T6b automatic port selection moves to $NEW_PORT (rule table names change)" \
    test -n "$NEW_PORT" -a "$NEW_PORT" != "$OLD_PORT"

echo "== T7 SIGKILL (stop timeout) leaves stale rules; the sweeper removes them"
# Kill client and firewall helper at once, like systemd's final SIGKILL.
sshuttle_pids | xargs -r kill -KILL
sleep 1
check "T7a stale sshuttle table left behind" test -n "$(sshuttle_tables)"
check "T7b stale redirect refuses connections (black hole)" test "$(http_code http://10.99.0.10:8080/ 4)" = 000
sweep_stale_tables
check "T7c sweeper deletes tables without a listener" test -z "$(sshuttle_tables)"

echo "== T8 fixed --listen port cannot restart within TIME_WAIT"
FIXED_PORT=$((12400 + RANDOM % 500))
PID=$(start_tunnel "$WORK/t8a.log" --listen 127.0.0.1:$FIXED_PORT)
check "T8a first start on fixed port $FIXED_PORT works" wait_connected "$WORK/t8a.log"
http_code http://10.99.0.10:8080/ > /dev/null
kill -TERM "$PID"
wait_exit "$PID" 10
PID=$(start_tunnel "$WORK/t8b.log" --listen 127.0.0.1:$FIXED_PORT)
wait_exit "$PID" 10
check "T8b restart with the same fixed port fails with EADDRINUSE" \
    grep -q "Address already in use" "$WORK/t8b.log"

echo "== T9 remote resolver unreachable"
# Rewrite in place: the bind mount inside the jump namespace follows the inode.
echo "nameserver 198.18.0.1" > /etc/netns/jump/resolv.conf
PID=$(start_tunnel "$WORK/t9a.log" --ns-hosts 10.99.0.53)
wait_connected "$WORK/t9a.log"
python3 "$WORK/dns_query.py" 10.99.0.53 git.corp.test > /dev/null
check "T9a one DNS query kills the whole tunnel (server-side ENETUNREACH)" wait_exit "$PID" 10
sweep_stale_tables
PID=$(start_tunnel "$WORK/t9b.log" --ns-hosts 10.99.0.53 --to-ns 10.99.0.53)
wait_connected "$WORK/t9b.log"
check "T9b with --to-ns the query is answered" \
    test "$(python3 "$WORK/dns_query.py" 10.99.0.53 git.corp.test)" = "ANSWER 10.99.0.10"
check "T9c with --to-ns the tunnel survives" kill -0 "$PID"
kill -TERM "$PID"
wait_exit "$PID" 10
echo "nameserver 10.99.0.53" > /etc/netns/jump/resolv.conf

echo "== T10 roaming (local address changes) without and with ssh keepalives"
roam() { ip addr del "$1"/24 dev h-j; ip addr add "$2"/24 dev h-j; }
write_ssh_config no-keepalive
PID=$(start_tunnel "$WORK/t10a.log" --ns-hosts 10.99.0.53)
wait_connected "$WORK/t10a.log"
roam $HOST_IP $ROAM_IP
sleep 30
check "T10a no keepalive: sshuttle still running 30 s after roaming (unit would be 'active')" kill -0 "$PID"
check "T10b ... while traffic through it is dead" test "$(http_code http://10.99.0.10:8080/ 5)" = 000
kill -TERM "$PID"
wait_exit "$PID" 10
sleep 2
user_ssh_alive() { pgrep -u "$LAB_USER" -x ssh > /dev/null; }
check "T10e the user's ssh outlives sshuttle's exit (needs a cgroup kill)" user_ssh_alive
pkill -KILL -u "$LAB_USER" -x ssh
roam $ROAM_IP $HOST_IP
write_ssh_config keepalive
PID=$(start_tunnel "$WORK/t10c.log" --ns-hosts 10.99.0.53)
wait_connected "$WORK/t10c.log"
START=$(date +%s)
roam $HOST_IP $ROAM_IP
wait_exit "$PID" 40
ELAPSED=$(( $(date +%s) - START ))
check "T10c ServerAliveInterval=5/CountMax=2: sshuttle exits ${ELAPSED} s after roaming" \
    test "$ELAPSED" -le 25
check "T10d rules cleaned up on that exit" test -z "$(sshuttle_tables)"
roam $ROAM_IP $HOST_IP

echo "== T11 IPv6 subnets through the tunnel"
if [ "$HAVE_IPV6" = yes ]; then
    : > "$WORK/http6.log"
    PID=$(start_tunnel6 "$WORK/t11.log" corp)
    check "T11a tunnel with IPv6 comes up (no --disable-ipv6)" wait_connected "$WORK/t11.log"
    check "T11b sshuttle has an IPv6 table" test -n "$(sshuttle_tables | grep ipv6)"
    check "T11c IPv6 TCP to the internal host through the tunnel" \
        test "$(http_code "http://[$INTERNAL_IP6]:8080/")" = 200
    check "T11d ... arriving from the jump host, not from the host" \
        log_from "$WORK/http6.log" 2001:db8:99::1
    check "T11e IPv4 still goes through the same tunnel" \
        test "$(http_code http://10.99.0.10:8080/)" = 200

    echo "== T12 IPv6 guard sets"
    install_guard
    check "T12a guard lets redirected IPv6 TCP through" \
        test "$(http_code "http://[$INTERNAL_IP6]:8080/")" = 200
    kill -TERM "$PID"
    wait_exit "$PID" 10
    START=$(date +%s%N)
    CODE=$(http_code "http://[$INTERNAL_IP6]:8080/" 4)
    ELAPSED_MS=$(( ($(date +%s%N) - START) / 1000000 ))
    check "T12b tunnel down + guard: IPv6 TCP refused at once (${ELAPSED_MS} ms), not leaked" \
        test "$CODE" = 000 -a "$ELAPSED_MS" -lt 1000
    check "T12c excluded jump host stays reachable over IPv6" \
        "$WORK/ssh-as-user" "$LAB_USER" "$AGENT_SOCK" corp6 true
    nft delete table inet $GUARD_TABLE
    : > "$WORK/http6.log"
    check "T12d without the guard the same request leaks: the route reaches the host" \
        test "$(http_code "http://[$INTERNAL_IP6]:8080/" 4)" = 200
    check "T12e ... arriving from the host's own address" log_from "$WORK/http6.log" $HOST_IP6

    echo "== T13 first hop by IPv6 only, inside a tunnelled IPv6 subnet"
    : > "$WORK/http6.log"
    PID=$(start_tunnel6 "$WORK/t13.log" corp6 2001:db8:51::/64)
    check "T13a ssh's own connection is not captured: the tunnel comes up" \
        wait_connected "$WORK/t13.log"
    check "T13b IPv6 TCP through that tunnel" \
        test "$(http_code "http://[$INTERNAL_IP6]:8080/")" = 200
    check "T13c ... from the jump host" log_from "$WORK/http6.log" 2001:db8:99::1
    kill -TERM "$PID"
    wait_exit "$PID" 10
else
    skip "T11-T13 need IPv6 in the kernel (/proc/net/if_inet6 is missing)"
fi

echo "== T14 IPv6 off on the host"
[ "$HAVE_IPV6" = yes ] && sysctl -qw net.ipv6.conf.lo.disable_ipv6=1
plugin_sees_ipv6() {
    plugin 'import sys; from nm_sshuttle.tunnel import ipv6_available; sys.exit(not ipv6_available())'
}
plugin_finds_ipv6_off() { ! plugin_sees_ipv6; }
check "T14a the plugin's check finds IPv6 off" plugin_finds_ipv6_off
PID=$(start_tunnel6 "$WORK/t14a.log" corp)
check "T14b without --disable-ipv6 sshuttle stops" wait_exit "$PID" 15
grep -m1 -i "ipv6 socket\|address family" "$WORK/t14a.log" | sed 's/^/      /'
PID=$(start_tunnel "$WORK/t14c.log" --ns-hosts 10.99.0.53)
check "T14c with --disable-ipv6 and the IPv6 networks left out, the tunnel comes up" \
    wait_connected "$WORK/t14c.log"
check "T14d ... and carries IPv4" test "$(http_code http://10.99.0.10:8080/)" = 200
kill -TERM "$PID"
wait_exit "$PID" 10
[ "$HAVE_IPV6" = yes ] && sysctl -qw net.ipv6.conf.lo.disable_ipv6="$LO_IPV6_OFF"

# ------------------------------------------------- method=tproxy (T15-T19)
# The plugin's own guard (with the mark rules), policy routing and sweep, from
# this checkout (docs/later/tproxy.md). The host gets a route to the internal
# network through the jump host, so that a leak would actually arrive, and a
# counter on the uplink shows whether anything to it left the host.
TMARK=0x4e530000

start_tproxy() {  # start_tproxy LOG -> prints PID
    "$SSHUTTLE" -v --disable-ipv6 --method tproxy --tmark $TMARK \
        -e "$WORK/ssh-as-user $LAB_USER $AGENT_SOCK" -r corp -x $JUMP_IP \
        --ns-hosts 10.99.0.53 --to-ns 10.99.0.53 10.99.0.0/24 > "$1" 2>&1 &
    echo $!
}

udp_echo() {  # udp_echo TEXT [PAUSE] -> the client's output
    python3 "$WORK/udp_echo.py" send 10.99.0.10 7777 "$@"
}

mangle_chains() { iptables -w -t mangle -S | grep -c '^-N sshuttle-'; }

uplink_leaks() {  # packets to the internal network that left on the uplink
    nft list chain inet nmss-lab-count postrouting | grep -o 'packets [0-9]*' | awk '{print $2}'
}

if command -v iptables > /dev/null && command -v ip6tables > /dev/null; then
    ip netns exec internal python3 "$WORK/udp_echo.py" serve 10.99.0.10 7777 \
        > "$WORK/udp.log" 2>&1 &
    ip route add 10.99.0.0/24 via $JUMP_IP
    nft -f - <<EOF
table inet nmss-lab-count {
    chain postrouting {
        type filter hook postrouting priority 0; policy accept;
        oifname "h-j" ip daddr 10.99.0.0/24 counter
    }
}
EOF
    plugin "from nm_sshuttle import guard
guard.install_guard('$GUARD_TABLE', ['10.99.0.0/24'], ['$JUMP_IP'], $TMARK)
guard.install_policy_routing([4], 0)"
    : > "$WORK/dns.log"

    echo "== T15 DNS through method=tproxy, pinned to the tunnel link"
    PID=$(start_tproxy "$WORK/t15.log")
    check "T15a the tproxy tunnel comes up behind the plugin's guard" wait_connected "$WORK/t15.log"
    check "T15b DNS pinned to the tunnel link (IP_UNICAST_IF) is answered" \
        test "$(python3 "$WORK/dns_query.py" 10.99.0.53 git.corp.test $TUNNEL_LINK)" = \
        "ANSWER 10.99.0.10"
    check "T15c ... by the internal server, asked by the jump host" \
        grep -q "query from 10.99.0.1: git.corp.test" "$WORK/dns.log"

    echo "== T16 UDP and TCP through the tunnel"
    check "T16a UDP echo answers, from the jump host" \
        test "$(udp_echo hello)" = "ECHO hello from 10.99.0.1"
    check "T16b TCP to the internal host" test "$(http_code http://10.99.0.10:8080/)" = 200
    check "T16c nothing to the internal network left on the uplink" test "$(uplink_leaks)" = 0

    echo "== T17 tunnel down: UDP and TCP fail locally"
    kill -TERM "$PID"
    wait_exit "$PID" 10
    wait_gone 10  # the firewall helper removes the chains after the client exits
    check "T17a sshuttle removed its mangle chains" test "$(mangle_chains)" = 0
    check "T17b UDP refused locally" test "$(udp_echo down)" = "ERROR Operation not permitted"
    START=$(date +%s%N)
    CODE=$(http_code http://10.99.0.10:8080/ 4)
    ELAPSED_MS=$(( ($(date +%s%N) - START) / 1000000 ))
    check "T17c TCP refused at once (${ELAPSED_MS} ms)" test "$CODE" = 000 -a "$ELAPSED_MS" -lt 1000
    check "T17d nothing left on the uplink" test "$(uplink_leaks)" = 0

    echo "== T18 SIGKILL leaves the mangle chains; the plugin's sweep removes them"
    PID=$(start_tproxy "$WORK/t18.log")
    wait_connected "$WORK/t18.log"
    sshuttle_pids | xargs -r kill -KILL
    sleep 1
    check "T18a stale mangle chains left behind" test "$(mangle_chains)" -gt 0
    check "T18b UDP gets no answer through them" test "$(udp_echo stale)" != "ECHO stale from 10.99.0.1"
    check "T18c ... and does not leak" test "$(uplink_leaks)" = 0
    plugin 'from nm_sshuttle import tunnel; tunnel.sweep()'
    check "T18d the sweep removes them" test "$(mangle_chains)" = 0
    check "T18e the plugin's rule and route stay" \
        plugin 'import sys; from nm_sshuttle import guard
sys.exit(not guard.policy_routing_present([4], 0))'

    echo "== T19 a UDP flow silent for 40 s"
    PID=$(start_tproxy "$WORK/t19.log")
    wait_connected "$WORK/t19.log"
    OUT=$(udp_echo quiet 40)
    check "T19 the flow answers before and after the pause" \
        test "$OUT" = "ECHO quiet from 10.99.0.1
ECHO quiet-again from 10.99.0.1"
    kill -TERM "$PID"
    wait_exit "$PID" 10
    check "T19b the plugin removes the rule and route by their values" \
        plugin 'import sys; from nm_sshuttle import guard
sys.exit(not guard.remove_policy_routing(0))'
    nft delete table inet $GUARD_TABLE
    nft delete table inet nmss-lab-count
    ip route del 10.99.0.0/24 via $JUMP_IP
else
    skip "T15-T19 need iptables and ip6tables"
fi

echo
echo "== $PASS passed, $FAIL failed, $SKIP skipped"
[ "$FAIL" = 0 ]
