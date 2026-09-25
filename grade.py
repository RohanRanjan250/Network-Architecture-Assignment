#!/usr/bin/env python3
import argparse, socket, sys


def read_response(sock, buf):
    while b"\r\n\r\n" not in buf:
        data = sock.recv(65536)
        if not data:
            raise ConnectionError("server closed the connection mid-response")
        buf += data
    end = buf.index(b"\r\n\r\n")
    head = buf[:end].decode("latin-1").split("\r\n")
    del buf[:end + 4]
    status = int(head[0].split()[1])
    headers = {}
    for line in head[1:]:
        k, _, v = line.partition(":")
        headers[k.strip().lower()] = v.strip()
    if status == 100:
        return read_response(sock, buf)
    n = int(headers.get("content-length", 0))
    while len(buf) < n:
        data = sock.recv(65536)
        if not data:
            raise ConnectionError("server closed the connection mid-body")
        buf += data
    body = bytes(buf[:n])
    del buf[:n]
    return status, headers, body


def still_open(sock):
    sock.setblocking(False)
    try:
        return sock.recv(1, socket.MSG_PEEK) != b""
    except BlockingIOError:
        return True
    except OSError:
        return False
    finally:
        sock.setblocking(True)


CASES = [
    ("GET",  "/add?a=2&b=3",  200, b"5"),
    ("GET",  "/sub?a=10&b=4", 200, b"6"),
    ("GET",  "/mul?a=6&b=7",  200, b"42"),
    ("GET",  "/div?a=1&b=0",  400, None),
    ("GET",  "/pow?a=2&b=8",  404, None),
    ("POST", "/add",          405, None),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--pipeline", action="store_true",
                    help="send all six requests in one write before reading")
    opts = ap.parse_args()

    s = socket.create_connection((opts.host, opts.port))
    s.settimeout(5)
    host = f"{opts.host}:{opts.port}"
    reqs = [f"{m} {t} HTTP/1.1\r\nHost: {host}\r\n"
            + ("Content-Length: 0\r\n" if m == "POST" else "") + "\r\n"
            for m, t, _, _ in CASES]

    buf, ok = bytearray(), True
    if opts.pipeline:
        s.sendall("".join(reqs).encode())
    for (m, t, want, want_body), raw in zip(CASES, reqs):
        if not opts.pipeline:
            s.sendall(raw.encode())
        status, _, body = read_response(s, buf)
        good = status == want and (want_body is None or body.strip() == want_body)
        ok &= good
        print(f"  {m + ' ' + t:<22} -> {status:<4} {body.decode(errors='replace').strip()[:28]:<30}"
              f"{'ok' if good else f'FAIL (wanted {want})'}")

    alive = still_open(s)
    print(f"\n  socket still open: {alive}")
    print(f"  1 TCP handshake, {len(CASES)} responses"
          + ("  (pipelined)" if opts.pipeline else ""))
    s.close()
    sys.exit(0 if ok and alive else 1)


if __name__ == "__main__":
    main()
