#!/usr/bin/env python3
# IPv4-only CONNECT proxy: grpc agent stuck on dead IPv6 -> force v4 via local proxy
import socket, threading, sys

LISTEN = ("127.0.0.1", 3128)

def resolve_v4(host, port):
    for fam, typ, pro, cn, sa in socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM):
        return sa
    raise OSError("no A record for " + host)

def pipe(a, b):
    try:
        while True:
            d = a.recv(65536)
            if not d: break
            b.sendall(d)
    except Exception:
        pass
    try: a.close()
    except Exception: pass
    try: b.close()
    except Exception: pass

def handle(c):
    try:
        c.settimeout(30)
        req = b""
        while b"\r\n\r\n" not in req:
            d = c.recv(4096)
            if not d: return
            req += d
            if len(req) > 65536: return
        line = req.split(b"\r\n")[0].decode()
        parts = line.split()
        if parts[0] != "CONNECT":
            c.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n"); return
        host, port = parts[1].rsplit(":", 1)
        port = int(port)
        sa = resolve_v4(host, port)
        up = socket.create_connection(sa, timeout=15)
        c.settimeout(None)
        c.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        t1 = threading.Thread(target=pipe, args=(c, up), daemon=True)
        t2 = threading.Thread(target=pipe, args=(up, c), daemon=True)
        t1.start(); t2.start(); t1.join(); t2.join()
    except Exception as e:
        try: c.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
        except Exception: pass
        try: c.close()
        except Exception: pass

def main():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(LISTEN); s.listen(64)
    print("v4proxy listening", LISTEN, flush=True)
    while True:
        c, _ = s.accept()
        threading.Thread(target=handle, args=(c,), daemon=True).start()

main()
