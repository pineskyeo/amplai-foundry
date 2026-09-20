#!/usr/bin/env python3
"""Allowlist-only HTTP CONNECT proxy: the single route out of the agent's internal network.

Runs inside the pinned worker image (Debian python3, stdlib only). Every CONNECT target that
is not an exact ``host:port`` on the allowlist is refused with 403 and logged; plain HTTP
methods are refused with 405. One JSON decision line per request on stdout; no request body
is ever logged. Deny wins over allow (design 10_AUTHORITY_SECURITY).
"""

import argparse
import contextlib
import json
import socket
import socketserver
import threading
import time


class Handler(socketserver.BaseRequestHandler):
    allow: frozenset = frozenset()

    def handle(self):
        sock = self.request
        sock.settimeout(5)
        deadline = time.monotonic() + 30  # absolute budget for the request head (slow-loris)
        head = b""
        while b"\r\n\r\n" not in head:
            if time.monotonic() > deadline:
                self.log("-", "deny", "header_timeout")
                return
            try:
                chunk = sock.recv(4096)
            except TimeoutError:
                continue
            if not chunk:
                return
            head += chunk
            if len(head) > 16384:
                self.log("-", "deny", "header_too_large")
                sock.sendall(b"HTTP/1.1 431 Request Header Fields Too Large\r\n\r\n")
                return
        parts = head.split(b"\r\n", 1)[0].decode("latin-1").split()
        if len(parts) < 2 or parts[0] != "CONNECT":
            self.log(" ".join(parts[:2]), "deny", "method")
            sock.sendall(b"HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\n\r\n")
            return
        target = parts[1].lower()
        if ":" in target:
            host_part, _, port_part = target.rpartition(":")
            target = host_part.rstrip(".") + ":" + port_part
        if target not in self.allow:
            self.log(target, "deny", "allowlist")
            sock.sendall(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
            return
        host, port = target.rsplit(":", 1)
        try:
            upstream = socket.create_connection((host, int(port)), timeout=15)
        except OSError as exc:
            self.log(target, "deny", "upstream:" + type(exc).__name__)
            sock.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            return
        self.log(target, "allow", "connect")
        sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        # The tunnel phase is idle-tolerant: a model turn can sit silent for minutes.
        sock.settimeout(None)
        upstream.settimeout(None)
        worker = threading.Thread(target=pump, args=(upstream, sock), daemon=True)
        worker.start()
        pump(sock, upstream)
        worker.join()
        upstream.close()

    def log(self, target, decision, reason):
        line = {
            "ts": round(time.time(), 3),
            "target": target,
            "decision": decision,
            "reason": reason,
        }
        print(json.dumps(line), flush=True)


def pump(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            dst.shutdown(socket.SHUT_WR)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen", default="0.0.0.0:3128")
    parser.add_argument("--allow", action="append", default=[], help="exact host:port")
    args = parser.parse_args()
    Handler.allow = frozenset(a.lower() for a in args.allow)
    host, port = args.listen.rsplit(":", 1)
    print(
        json.dumps({"event": "listening", "listen": args.listen, "allow": sorted(Handler.allow)}),
        flush=True,
    )
    Server((host, int(port)), Handler).serve_forever()


if __name__ == "__main__":
    main()
