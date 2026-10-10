#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Send one DNS A query, optionally the way systemd-resolved sends a query
to a per-link DNS server: a UDP socket with IP_UNICAST_IF set to the link.

usage: dns_query.py SERVER NAME [IFNAME]

Prints "ANSWER <ip>", "NOANSWER rcode=<n>", "TIMEOUT" or "ERROR <reason>".
"""
import socket
import struct
import sys

IP_UNICAST_IF = 50  # linux/in.h

server, name = sys.argv[1], sys.argv[2]
ifname = sys.argv[3] if len(sys.argv) > 3 else None

query = struct.pack("!HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0)
for label in name.split("."):
    query += bytes([len(label)]) + label.encode()
query += b"\x00" + struct.pack("!HH", 1, 1)

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.settimeout(3)
if ifname:
    # resolved passes the ifindex in network byte order for IPv4
    idx = socket.if_nametoindex(ifname)
    s.setsockopt(socket.IPPROTO_IP, IP_UNICAST_IF, struct.pack("!I", idx))
try:
    s.sendto(query, (server, 53))
    data, _ = s.recvfrom(4096)
except socket.timeout:
    print("TIMEOUT")
    sys.exit(1)
except OSError as e:
    print(f"ERROR {e.strerror}")
    sys.exit(1)
rcode = struct.unpack("!H", data[2:4])[0] & 0xF
if struct.unpack("!H", data[6:8])[0]:
    print(f"ANSWER {socket.inet_ntoa(data[-4:])}")
else:
    print(f"NOANSWER rcode={rcode}")
    sys.exit(1)
