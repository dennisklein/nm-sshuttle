# sshuttle 2.0.0: three bug reports

We found these while building nm-sshuttle. It runs the sshuttle client as root from a systemd unit, and `ssh` runs as the desktop user through an `-e` wrapper. Each section below is a separate issue and can be filed on its own.

**Versions.** Line numbers refer to the 2.0.0 sdist from PyPI. We also read master at `2d5b8e9` (2026-10-08). Since 2.0.0, the only commit that touches `sshuttle/` is `3db9616` (#1261), which changes `client.py`. Where that matters, the section says so. Statements about older releases come from reading their code.

**Test setup.** We used an Ubuntu 24.04 container with Linux 6.18, Python 3.11.15 and OpenSSH 9.6p1. sshuttle 2.0.0 ran on both ends with `--method nft --disable-ipv6`. For sections 1 and 2, we ran the reproducers against a real sshd in a private network namespace, once with 2.0.0 and once with the proposed patch. Each section notes where our commands differed from the ones shown.

"Lab Tn" refers to a check in our network-namespace test script (`lab/run.sh` in nm-sshuttle). The script builds a client, a jump host running a real sshd, and an internal network. Its 28 checks assert the 2.0.0 behaviour described below, and all of them passed in the last run. You do not need the lab to reproduce anything here.

Anything marked *(from source)* comes from reading the code; we did not run it.

---

## 1. The server exits when `connect()` to the remote nameserver fails, and the whole tunnel goes down

**Versions:** 2.0.0 and master `2d5b8e9`, where `server.py` is unchanged. *(from source)* Releases 1.3.2, 1.0.0 and 0.78.5 have the same `connect()`-before-`try` ordering, and nothing in their `main()` catches `OSError`.

**Reproduce.** You need root on the remote, but only to make one address unroutable.

```sh
# remote, as root: the remote now has no route to 198.18.0.1
ip route add unreachable 198.18.0.1/32

# client
sshuttle -v --method nft --disable-ipv6 --ns-hosts 192.0.2.53 --to-ns 198.18.0.1 \
    -r user@remote 192.0.2.0/24
# client, second terminal: send one DNS query to the captured address
dig @192.0.2.53 example.com
```

The container had no `dig`, so we sent the query with a few lines of Python. The same crash happens without `--to-ns` when the remote cannot route to the nameserver that the server picks from its `/etc/resolv.conf`. Lab T9 covers this case: the jump host has `nameserver 198.18.0.1` and no default route.

**Actual.** The first forwarded query kills the server, and the client exits with it:

```
c : DNS request from ('198.51.100.2', 34785): 29 bytes
Traceback (most recent call last):
  ...
  File "sshuttle.server", line 427, in main
  File "sshuttle.ssnet", line 707, in runonce
  ...
  File "sshuttle.server", line 383, in dns_req
  File "sshuttle.server", line 195, in __init__
  File "sshuttle.server", line 218, in try_send
OSError: [Errno 113] No route to host
fw: undoing changes.
c : fatal: ssh connection to server (pid 29153) exited with returncode 1
```

Lab T9 gives the same trace, ending in `OSError: [Errno 101] Network is unreachable`.

**Expected.** Only that query fails: the client's resolver times out and the tunnel stays up. The code already retries on these errors when `send()` reports them (in `try_send()`) or `recv()` reports them (in `callback()`).

**Root cause** (`sshuttle/server.py`, 2.0.0):
- In `DnsProxy.try_send()` (lines 204-237), `sock.connect(sockaddr)` at line 218 runs before the `try:` at line 223.
- Only that `try` handles `ssnet.NET_ERRS` (`ssnet.py`:126-130, which includes `ENETUNREACH` and `EHOSTUNREACH`). It handles them by retrying.
- On a UDP socket, `connect()` does the route lookup, so it is the call that fails when the remote has no route to the nameserver. The traceback points at line 218.
- The exception leaves `DnsProxy.__init__` (line 195) and `dns_req` (line 383), passes through `ssnet.runonce()` (`ssnet.py`:707) and reaches the loop in `main()` (line 427).
- `main()` catches only `Fatal` (line 451), so the server process exits.

**Proposed fix:** move `connect()` into the existing `try`.

```diff
--- a/sshuttle/server.py
+++ b/sshuttle/server.py
@@ -215,12 +215,11 @@
 
         family, sockaddr = self._addrinfo(peer, port)
         sock = socket.socket(family, socket.SOCK_DGRAM)
-        sock.connect(sockaddr)
-
         self.peers[sock] = peer
 
         debug2('DNS: sending to %r:%d (try %d)' % (peer, port, self.tries))
         try:
+            sock.connect(sockaddr)
             sock.send(self.request)
             self.socks.append(sock)
         except socket.error:
```

*(from source)* The patch keeps `self.peers[sock] = peer` ahead of `connect()`. So when the handler expires, `DnsProxy.dispose()` (lines 173-177) still closes the sockets whose `connect()` failed.

**With the patch** (reproducer run with `-vv`):
- For each of tries 1 to 3, the server logs `DNS: sending to '198.18.0.1':0 (try N)` and then `DNS send to '198.18.0.1': [Errno 113] No route to host`. After the third try it drops the query, and the client's query times out.
- The tunnel stays up: an HTTP request through it afterwards returned 200.
- In a setup like lab T9 (an unroutable nameserver in `resolv.conf`, no `--to-ns`), the server also stayed up and carried a TCP connection afterwards.

**Regression test**, in the style of `tests/server/test_server.py`. That file also needs `import errno`. The test fails on 2.0.0 because the `OSError` escapes, and it passes with the patch. The existing tests give the same results with and without the patches in this report.

```python
def test_dnsproxy_connect_error_does_not_escape():
    err = OSError(errno.ENETUNREACH, 'Network is unreachable')
    with patch('sshuttle.server.socket.socket') as mock_socket:
        mock_socket.return_value.connect.side_effect = err
        h = sshuttle.server.DnsProxy(Mock(), 1, b'q', '192.0.2.53@53')
    assert h.tries == 3
    assert h.socks == []
```

**Workaround:** pass `--to-ns` with a resolver that the remote can route to (lab T9b, T9c).

---

## 2. The TCP redirector does not set `SO_REUSEADDR`, so a quick restart cannot reuse its port

**Versions:** 2.0.0. On master `2d5b8e9`, `MultiListener.bind()` has changed (#1261, `3db9616`) but still does not set `SO_REUSEADDR` *(from source)*. The option was removed in 0.78.4 (see History below). We observed this on Linux only.

**Reproduce:**

```sh
sshuttle -v --method nft --disable-ipv6 --listen 127.0.0.1:12345 -r user@remote 192.0.2.0/24 &
curl http://192.0.2.10:8080/            # any TCP connection through the tunnel
kill %1; wait                            # SIGTERM
ss -tan '( sport = :12345 )'             # shows TIME-WAIT 127.0.0.1:12345 ...
sshuttle -v --method nft --disable-ipv6 --listen 127.0.0.1:12345 -r user@remote 192.0.2.0/24
```

**Actual.** The second start fails (paths shortened):

```
  File "sshuttle/client.py", line 1097, in main
    raise last_e
  File "sshuttle/client.py", line 1082, in main
    tcp_listener.bind(lv6, lv4)
  File "sshuttle/client.py", line 192, in bind
    self.v4.bind(address_v4)
OSError: [Errno 98] Address already in use
```

Lab T8 shows the same failure. It lasts as long as the TIME_WAIT socket, which on Linux is 60 s by default; we did not time it.

Without `--listen`, the restart moves to the next free port instead: 12300, then 12299 (lab T6). The nft table name contains the port, so it changes too. In a separate two-namespace test, `inet sshuttle-ipv4-12300` became `inet sshuttle-ipv4-12299`. After such a restart, anything that finds sshuttle's rules by port number misses them, and so does a firewall config that allows a fixed port.

**Expected.** A restarted sshuttle binds the same port immediately. TCP servers usually make this possible by setting `SO_REUSEADDR` before `bind()`.

**Root cause** (`sshuttle/client.py`, 2.0.0):
- `MultiListener.bind()` (lines 167-194) creates its sockets (lines 171 and 191) and binds them (lines 173 and 192) without setting `SO_REUSEADDR`. Nothing else in `client.py` sets it.
- When the redirector closes an accepted connection first, a TIME_WAIT socket stays on the listener's port. We saw this with `ss` in lab T6a and in the reproducer.
- On Linux, that TIME_WAIT socket makes a new `bind()` to the port fail with `EADDRINUSE`.
- The port search (lines 1037-1097) then moves to the next port. With a fixed `--listen` port it has nothing to vary, so it ends by re-raising the last `EADDRINUSE` (line 1097).
- The nft table name is built from the port (`methods/nft.py`:21 and 23). `nat.py`:31 and `tproxy.py`:132-134 name their chains the same way.

**History.** Until 0.78.4, `bind()` set `SO_REUSEADDR` on every listener, both TCP and UDP. Commit `f27b27b` ("Stop using SO_REUSEADDR on sockets", 2018-02-15) removed it, and its message gives no reason. An earlier commit, `6cdc4da` (2017-11-06, for #178), describes a UDP problem: with `SO_REUSEADDR` set, the OS does not refuse a second UDP bind to the same address, so the UDP and DNS proxies ended up on the same port. We assume that problem was the reason for the removal, but the commit does not say so. The patch below therefore sets the option on TCP sockets only.

**Proposed fix** (TCP only):

```diff
--- a/sshuttle/client.py
+++ b/sshuttle/client.py
@@ -164,11 +164,19 @@
                 else:
                     raise e
 
+    def _set_reuseaddr(self, sock):
+        # TCP only: lets a restarted sshuttle bind its port while connections
+        # accepted by the previous instance are in TIME_WAIT. Not for UDP,
+        # where SO_REUSEADDR would let two instances bind the same port.
+        if self.type == socket.SOCK_STREAM and sys.platform != 'win32':
+            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
+
     def bind(self, address_v6, address_v4):
         assert not self.bind_called
         self.bind_called = True
         if address_v6 is not None:
             self.v6 = socket.socket(socket.AF_INET6, self.type, self.proto)
+            self._set_reuseaddr(self.v6)
             try:
                 self.v6.bind(address_v6)
             except OSError as e:
@@ -189,6 +197,7 @@
             self.v6 = None
         if address_v4 is not None:
             self.v4 = socket.socket(socket.AF_INET, self.type, self.proto)
+            self._set_reuseaddr(self.v4)
             self.v4.bind(address_v4)
         else:
             self.v4 = None
```

**What we checked** with small socket tests on Linux 6.18:
- **Both the old and the new listener need `SO_REUSEADDR`.** If the old listener did not set it, the new bind still fails. So the first restart after upgrading from an unpatched sshuttle can still fail once.
- **A listening socket still blocks its port.** On a port where another socket is listening, a second TCP socket with the option set still gets `EADDRINUSE`. The port search therefore still skips ports that a running sshuttle is using.
- **UDP behaves differently.** Two UDP sockets with the option set can both bind the same port. Setting it on the UDP redirector or the DNS listener would break the search for a free DNS port (lines 1104-1143), which relies on getting `EADDRINUSE`. That is the #178 problem.
- **The patch opens a narrow race.** Linux also lets two TCP sockets with the option set bind the same address, as long as neither is listening yet. `client.py` binds during the port search but calls `listen()` later, at line 1098, outside the `try`. So if two sshuttle instances pick the same port at the same moment, the second one's `listen()` fails with `EADDRINUSE`, and that instance exits instead of trying the next port. We saw this socket behaviour in the test; the sshuttle part is *(from source)*. Calling `tcp_listener.listen()` inside the `try` at lines 1081-1093 would avoid the race; we have not tested that.

**With the patch**, we repeated the reproducer on a fresh port, 12346, so that TIME_WAIT left by the unpatched run could not interfere. The restart bound the same port at once, and an HTTP request through it returned 200. Without `--listen`, the restart kept port 12300.

**Limits of what we tested:**
- We did not test macOS/BSD or the dual-stack wildcard listener.
- The `win32` exclusion follows the documented meaning of `SO_REUSEADDR` on Windows, where it lets a socket bind a port that another socket is already using. We did not test it.
- The patch applies to 2.0.0 as shown. On master, the first hunk needs rebasing around the new `IPV6_V6ONLY` lines in `bind()`. The second hunk applies with an offset.

---

## 3. Automatic remote exclusion ignores `-e`/`--ssh-cmd`

**Versions:** 2.0.0, and master `2d5b8e9` (`ssh.py` is unchanged; the call in `client.py` is at line 996 there). The feature is new in 2.0.0: CHANGELOG.md lists it as #1191 (`97fe674`).

**Reproduce.** You do not need a server, because sshuttle prints the exclusion list before `ssh` connects.

```sh
cat > /tmp/cfg <<'EOF'
Host myjump
    HostName 192.0.2.1
EOF
sshuttle -v --method nft --disable-ipv6 -e 'ssh -F /tmp/cfg' -r myjump 10.0.0.0/8
```

**Actual.** `ssh` uses the `-e` command and connects to 192.0.2.1. In our run there was no server there, and ssh failed with `ssh: connect to host 192.0.2.1 port 22: Network is unreachable`. The exclusion step, however, looks up `myjump` without that config file, and 192.0.2.1 is missing from the exclusion list:

```
c : Failed to exclude remote IP: [Errno -3] Temporary failure in name resolution
...
c : Subnets to exclude from forwarding:
c :   (<AddressFamily.AF_INET: 2>, '127.0.0.1', 32, 0, 0)
```

The error is `[Errno -3]` here because the test namespace had no DNS. Lab T2 printed `[Errno -2] Name or service not known`. The message appears only with `-v`.

Lab T2 is our own case:
- The client runs as root with `-e 'ssh-as-user USER SOCK'`, a wrapper that runs `ssh` as USER.
- The host alias is defined only in USER's `~/.ssh/config`.
- We pass `-x` ourselves, so our tunnel still works (lab T1).

**Expected.** The docs say "The SSH connection endpoint is automatically excluded from forwarding" (`docs/manpage.rst`:355-356, `docs/usage.rst`:14). They describe `-e` as "The command to use to connect to the remote server" (`manpage.rst`:179-182). So the lookup should use the same command and options as the connection.

**Root cause:**
- `ssh.parse_hostname()` (`sshuttle/ssh.py`:33-67) runs `['ssh', '-G', host]` (lines 48-50). It uses whatever `ssh` is first on PATH and never sees `ssh_cmd`.
- `client.py`:993 calls it without `ssh_cmd`, while `ssh.connect()` does use `ssh_cmd` (`ssh.py`:166-169).
- For an alias that plain `ssh` does not know, `ssh -G` still exits 0 and prints `hostname <alias>` (seen with OpenSSH 9.6p1).
- `getaddrinfo(alias)` then fails (`client.py`:1004-1017), and the failure is logged only at `debug1`.

*(from source)* The same problem affects:
- Other `-e` options that change where ssh connects. With `-J jump`, sshuttle would exclude the target instead of the jump host. With `-o ProxyCommand=...`, the documented skip would not happen. (`ssh -G` does report `proxyjump` and `proxycommand` when these options are passed to it.)
- An `ssh` binary at a different path.
- An alias that happens to resolve in DNS. In that case the wrong address is excluded, with no message.

*(from source)* Without the exclusion, new connections from the client to the SSH endpoint are redirected into the tunnel whenever the endpoint is inside a forwarded subnet. In the reproducer it is not, so there the missing entry has no effect.

**Proposed fix:**

```diff
--- a/sshuttle/ssh.py
+++ b/sshuttle/ssh.py
@@ -30,7 +30,7 @@
     return b'%s\n%d\n%s' % (name.encode("ASCII"), len(content), content)
 
 
-def parse_hostname(remotename):
+def parse_hostname(remotename, ssh_cmd=None):
     """
     Parse and resolve SSH remote hostname.
     Uses 'ssh -G' to query the effective SSH configuration.
@@ -45,8 +45,9 @@
     proxy_command = None
 
     try:
+        sshl = shlex.split(ssh_cmd) if ssh_cmd else ['ssh']
         result = ssubprocess.run(
-            ['ssh', '-G', host],
+            sshl + ['-G', host],
             capture_output=True, text=True, timeout=2)
         if result.returncode == 0:
             for line in result.stdout.split('\n'):
--- a/sshuttle/client.py
+++ b/sshuttle/client.py
@@ -990,7 +990,7 @@
         subnets_exclude.append((socket.AF_INET6, listenip_v6[0], 128, 0, 0))
 
     # Exclude remote server IP
-    host, proxy_host, proxy_command = ssh.parse_hostname(remotename)
+    host, proxy_host, proxy_command = ssh.parse_hostname(remotename, ssh_cmd)
     hosts_to_exclude = []
     if proxy_command:
         debug1(
```

**With the patch**, the reproducer lists `(<AddressFamily.AF_INET: 2>, '192.0.2.1', 32, 0, 0)` under "Subnets to exclude from forwarding" and no longer prints "Failed to exclude remote IP".

**Side effect.** The `-e` command now also runs once as `<cmd> -G host`. A wrapper that does not pass its arguments on to `ssh` may fail at that point. *(from source)* `parse_hostname()` already runs this with a 2 s timeout inside `except Exception`, so such a failure costs at most 2 s and the exclusion. Our `ssh-as-user` wrapper passes `"$@"` to `ssh`, but we have not run lab T2 with the patch.

---

## Also seen, not drafted: a traceback when the firewall helper relays a signal to a client that has already exited

On Fedora 44 with sshuttle 1.3.2 under a systemd unit, 6 of 29 stops logged `ProcessLookupError: [Errno 3] No such process` from `firewall_exit()` (`firewall.py`, line 95, `os.kill(sshuttle_pid, sig)`). The rules had already been removed. Each time, the exception was raised while the helper was flushing the DNS cache, so the helper logged it under "Error trying to flush systemd dns cache." and skipped the flush. *(from source)* 2.0.0 has the same unguarded `os.kill()` (`firewall.py`:77-95). Catching `ProcessLookupError` there would fix it. Apart from the skipped flush this is cosmetic; it is worth a one-line issue.
