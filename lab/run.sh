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
# Run as root on a disposable VM or container. It creates network
# namespaces, veth links, nft tables and a local user, and it changes the
# address of its own veth link. Needs: iproute2 (ip, ss), nft, sshd, ssh,
# ssh-agent, setpriv, curl, python3 and sshuttle (set SSHUTTLE=/path if it
# is not on PATH). The kill tests match processes whose first argument is
# the sshuttle path, so do not run other sshuttle instances meanwhile.
set -uo pipefail

LAB_SRC=$(cd "$(dirname "$0")" && pwd)
SSHUTTLE=${SSHUTTLE:-$(command -v sshuttle || true)}
LAB_USER=${LAB_USER:-nmss-lab}
JUMP_IP=198.51.100.1
HOST_IP=198.51.100.2
ROAM_IP=198.51.100.3
TUNNEL_LINK=nmss0     # stands in for the per-tunnel dummy link
GUARD_TABLE=nm-sshuttle-guard

[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 2; }
[ -x "$SSHUTTLE" ] || { echo "sshuttle not found; set SSHUTTLE=" >&2; exit 2; }

WORK=$(mktemp -d /tmp/nmss-lab.XXXXXX)
chmod 755 "$WORK"
cp "$LAB_SRC/ssh-as-user" "$LAB_SRC/dns_query.py" "$LAB_SRC/dns_server.py" "$WORK/"
chmod 755 "$WORK"/*
AGENT_SOCK=$WORK/agent/agent.sock

PASS=0
FAIL=0
pass() { echo "PASS  $*"; PASS=$((PASS + 1)); }
fail() { echo "FAIL  $*"; FAIL=$((FAIL + 1)); }
check() { local name=$1; shift; if "$@"; then pass "$name"; else fail "$name"; fi; }

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
    [ -f "$WORK/sshd.pid" ] && kill "$(cat "$WORK/sshd.pid")" 2>/dev/null
    pkill -f "$WORK/dns_server.py" 2>/dev/null
    pkill -f "http.server --bind 10.99.0.10" 2>/dev/null
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
    ip netns exec jump /usr/sbin/sshd -f "$WORK/sshd_config" -E "$WORK/sshd.log"

    ip netns exec internal python3 "$WORK/dns_server.py" 10.99.0.53 > "$WORK/dns.log" 2>&1 &
    ip netns exec internal python3 -m http.server --bind 10.99.0.10 8080 \
        --directory "$WORK" > "$WORK/http.log" 2>&1 &
    local i
    for i in $(seq 1 50); do
        ss -N internal -ltnH 'sport = :8080' | grep -q . &&
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

wait_connected() {
    local i
    for i in $(seq 1 30); do
        grep -q "Connected to server" "$1" && return 0
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

redirect_port() {  # redirect_port LOG -> TCP port sshuttle listened on
    sed -n "s/.*TCP redirector listening on ('127.0.0.1', \([0-9]*\)).*/\1/p" "$1"
}

# Prints the HTTP status, or 000 if no response arrived within the timeout.
http_code() {
    curl -s -m "${2:-5}" -o /dev/null -w '%{http_code}' --noproxy '*' "$1"
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

install_guard() {
    nft -f - <<EOF
table inet $GUARD_TABLE {
    set subnets4 { type ipv4_addr; flags interval; elements = { 10.99.0.0/24 } }
    set exclude4 { type ipv4_addr; flags interval; elements = { $JUMP_IP } }
    chain output {
        type filter hook output priority filter; policy accept;
        ip daddr @exclude4 accept
        ip daddr @subnets4 ct status dnat accept
        ip daddr @subnets4 meta l4proto tcp reject with tcp reset
        ip daddr @subnets4 reject with icmp admin-prohibited
    }
}
EOF
}

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

echo
echo "== $PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
