#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Tiny DNS responder for the lab.

Answers A queries for *.ZONE with ADDRESS, other types in ZONE with an empty
NOERROR, and REFUSED for everything else. Logs every query so tests can see
where it came from.

With NAME=ADDRESS arguments it answers A queries for exactly those names
instead, and REFUSED for everything else: a resolver on the local network that
knows a few names of the far side (test-vm M2-29).

usage: dns_server.py BIND_ADDRESS [ZONE [ADDRESS]]   (corp.test, 10.99.0.10)
       dns_server.py BIND_ADDRESS NAME=ADDRESS...
"""
import socket
import struct
import sys


def parse_qname(data, off):
    labels = []
    while data[off]:
        n = data[off]
        labels.append(data[off + 1:off + 1 + n].decode())
        off += 1 + n
    return ".".join(labels), off + 1


def main():
    args = sys.argv[2:]
    fixed = dict(arg.split("=", 1) for arg in args if "=" in arg)
    zone = args[0] if args and not fixed else "corp.test"
    answer = socket.inet_aton(args[1] if len(args) > 1 and not fixed else "10.99.0.10")
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind((sys.argv[1], 53))
    while True:
        data, peer = s.recvfrom(4096)
        tid = struct.unpack("!H", data[:2])[0]
        name, off = parse_qname(data, 12)
        qtype = struct.unpack("!H", data[off:off + 2])[0]
        question = data[12:off + 4]
        print(f"query from {peer[0]}: {name} type {qtype}", flush=True)
        if fixed:
            if name in fixed and qtype == 1:
                hdr = struct.pack("!HHHHHH", tid, 0x8180, 1, 1, 0, 0)
                ans = (b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 60, 4)
                       + socket.inet_aton(fixed[name]))
                s.sendto(hdr + question + ans, peer)
            elif name in fixed:
                s.sendto(struct.pack("!HHHHHH", tid, 0x8180, 1, 0, 0, 0) + question, peer)
            else:
                s.sendto(struct.pack("!HHHHHH", tid, 0x8185, 1, 0, 0, 0) + question, peer)
        elif name.endswith(zone) and qtype == 1:
            hdr = struct.pack("!HHHHHH", tid, 0x8180, 1, 1, 0, 0)
            ans = b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 60, 4) + answer
            s.sendto(hdr + question + ans, peer)
        elif name.endswith(zone):
            # other types in the zone: NOERROR without answers, like a real server
            hdr = struct.pack("!HHHHHH", tid, 0x8180, 1, 0, 0, 0)
            s.sendto(hdr + question, peer)
        else:
            hdr = struct.pack("!HHHHHH", tid, 0x8185, 1, 0, 0, 0)
            s.sendto(hdr + question, peer)


if __name__ == "__main__":
    main()
