#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Tiny DNS responder for the lab.

Answers A queries for *.corp.test with 10.99.0.10, other types in corp.test
with an empty NOERROR, and REFUSED for everything else. Logs every query so
tests can see where it came from.

usage: dns_server.py BIND_ADDRESS
"""
import socket
import struct
import sys

ANSWER = socket.inet_aton("10.99.0.10")


def parse_qname(data, off):
    labels = []
    while data[off]:
        n = data[off]
        labels.append(data[off + 1:off + 1 + n].decode())
        off += 1 + n
    return ".".join(labels), off + 1


def main():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind((sys.argv[1], 53))
    while True:
        data, peer = s.recvfrom(4096)
        tid = struct.unpack("!H", data[:2])[0]
        name, off = parse_qname(data, 12)
        qtype = struct.unpack("!H", data[off:off + 2])[0]
        question = data[12:off + 4]
        print(f"query from {peer[0]}: {name} type {qtype}", flush=True)
        if name.endswith("corp.test") and qtype == 1:
            hdr = struct.pack("!HHHHHH", tid, 0x8180, 1, 1, 0, 0)
            ans = b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 60, 4) + ANSWER
            s.sendto(hdr + question + ans, peer)
        elif name.endswith("corp.test"):
            # other types in the zone: NOERROR without answers, like a real server
            hdr = struct.pack("!HHHHHH", tid, 0x8180, 1, 0, 0, 0)
            s.sendto(hdr + question, peer)
        else:
            hdr = struct.pack("!HHHHHH", tid, 0x8185, 1, 0, 0, 0)
            s.sendto(hdr + question, peer)


if __name__ == "__main__":
    main()
