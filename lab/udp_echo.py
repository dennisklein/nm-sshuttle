#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""UDP echo for the lab: the server answers every datagram with its text and
the address it came from, so tests can see whether it went through the jump
host.

usage: udp_echo.py serve ADDRESS PORT
       udp_echo.py send ADDRESS PORT TEXT [PAUSE]

send prints "ECHO <text> from <ip>", "TIMEOUT" or "ERROR <reason>". With
PAUSE it sends twice from the same socket, PAUSE seconds apart, and prints
both answers.
"""
import socket
import sys
import time


def serve(addr, port):
    s = socket.socket(socket.AF_INET6 if ":" in addr else socket.AF_INET, socket.SOCK_DGRAM)
    s.bind((addr, port))
    while True:
        data, peer = s.recvfrom(2048)
        print(f"datagram from {peer[0]}: {data.decode(errors='replace')}", flush=True)
        s.sendto(data + f" from {peer[0]}".encode(), peer)


def ask(s, addr, port, text):
    try:
        s.sendto(text.encode(), (addr, port))
        data, _ = s.recvfrom(2048)
        return "ECHO " + data.decode(errors="replace")
    except socket.timeout:
        return "TIMEOUT"
    except OSError as e:
        return f"ERROR {e.strerror or e}"


def main():
    if sys.argv[1] == "serve":
        serve(sys.argv[2], int(sys.argv[3]))
        return
    addr, port, text = sys.argv[2], int(sys.argv[3]), sys.argv[4]
    s = socket.socket(socket.AF_INET6 if ":" in addr else socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(3)
    print(ask(s, addr, port, text))
    if len(sys.argv) > 5:
        time.sleep(float(sys.argv[5]))
        print(ask(s, addr, port, text + "-again"))


if __name__ == "__main__":
    main()
