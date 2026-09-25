#!/usr/bin/env python3
import argparse, re, selectors, socket, sys, threading, time
from decimal import Decimal, localcontext
from email.utils import formatdate
from urllib.parse import unquote, unquote_plus

IDLE_TIMEOUT    = 15.0
REQUEST_TIMEOUT = 10.0
MAX_HEAD        = 8 * 1024
MAX_BODY        = 1 << 20
MAX_NUMBER_LEN  = 64
RECV_SIZE       = 64 * 1024

REASON = {100: "Continue", 200: "OK", 400: "Bad Request", 404: "Not Found",
          405: "Method Not Allowed", 408: "Request Timeout",
          413: "Content Too Large", 431: "Request Header Fields Too Large",
          501: "Not Implemented", 505: "HTTP Version Not Supported"}

END_OF_HEAD  = re.compile(rb"\r?\n\r?\n")
REQUEST_LINE = re.compile(r"([!#$%&'*+.^_`|~0-9A-Za-z-]+) (\S+) HTTP/(\d)\.(\d)")
NUMBER       = re.compile(r"[+-]?(\d+(\.\d*)?|\.\d+)")

OPS = {"add": lambda a, b: a + b,
       "sub": lambda a, b: a - b,
       "mul": lambda a, b: a * b,
       "div": lambda a, b: a / b}


def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, file=sys.stderr, flush=True)


class HTTPError(Exception):
    def __init__(self, status, message, close=False, headers=()):
        super().__init__(message)
        self.status, self.message, self.close, self.headers = \
            status, message, close, list(headers)


class ClientGone(Exception):
    pass


class IdleTimeout(Exception):
    pass


class Request:
    def __init__(self, method, target, version, headers, body):
        self.method, self.target, self.version = method, target, version
        self.headers, self.body = headers, body

    def tokens(self, name):
        return {t.strip().lower() for t in self.headers.get(name, "").split(",")
                if t.strip()}


class Connection:
    ids = 0

    def __init__(self, sock, peer):
        Connection.ids += 1
        self.id, self.sock, self.peer = Connection.ids, sock, peer
        self.inbuf, self.outbuf = bytearray(), bytearray()
        self.served = 0
        self.deadline = None

    def tag(self):
        return f"conn#{self.id} {self.peer[0]}:{self.peer[1]}"

    def flush(self):
        if self.outbuf:
            self.sock.sendall(self.outbuf)
            self.outbuf.clear()

    def fill(self):
        self.flush()
        if self.deadline is None:
            self.sock.settimeout(IDLE_TIMEOUT)
        else:
            left = self.deadline - time.monotonic()
            if left <= 0:
                raise HTTPError(408, "request took too long to arrive", close=True)
            self.sock.settimeout(left)
        try:
            data = self.sock.recv(RECV_SIZE)
        except socket.timeout:
            if self.deadline is None:
                raise IdleTimeout()
            raise HTTPError(408, "request took too long to arrive", close=True)
        except (ConnectionResetError, BrokenPipeError):
            raise ClientGone()
        if not data:
            raise ClientGone()
        self.inbuf += data

    def take(self, n):
        chunk = bytes(self.inbuf[:n])
        del self.inbuf[:n]
        return chunk

    def read_exact(self, n):
        while len(self.inbuf) < n:
            self.fill()
        return self.take(n)

    def read_line(self, limit):
        while True:
            i = self.inbuf.find(b"\n")
            if i >= 0:
                return self.take(i + 1).rstrip(b"\r\n")
            if len(self.inbuf) > limit:
                raise HTTPError(400, "line too long", close=True)
            self.fill()

    def read_request(self):
        while True:
            while self.inbuf[:1] in (b"\r", b"\n"):
                del self.inbuf[:1]
            if self.inbuf:
                break
            try:
                self.fill()
            except ClientGone:
                return None

        self.deadline = time.monotonic() + REQUEST_TIMEOUT

        while True:
            m = END_OF_HEAD.search(self.inbuf, 0, MAX_HEAD + 4)
            if m:
                break
            if len(self.inbuf) > MAX_HEAD:
                raise HTTPError(431, "request head larger than %d bytes" % MAX_HEAD,
                                close=True)
            self.fill()
        head = self.take(m.end())[:m.start()].decode("latin-1")
        lines = re.split(r"\r?\n", head)

        rl = REQUEST_LINE.fullmatch(lines[0])
        if not rl:
            raise HTTPError(400, "malformed request line", close=True)
        method, target, major, minor = rl.groups()
        if major != "1":
            raise HTTPError(505, "only HTTP/1.x is spoken here", close=True)
        version = (1, int(minor))

        headers = {}
        for line in lines[1:]:
            if line[:1] in (" ", "\t"):
                raise HTTPError(400, "obsolete header line folding", close=True)
            name, colon, value = line.partition(":")
            if not colon or not name or name != name.strip():
                raise HTTPError(400, "malformed header line", close=True)
            name, value = name.lower(), value.strip(" \t")
            if name in headers:
                if name in ("host", "content-length"):
                    if name == "host" or headers[name] != value:
                        raise HTTPError(400, f"conflicting {name} headers", close=True)
                    continue
                headers[name] += ", " + value
            else:
                headers[name] = value

        te = headers.get("transfer-encoding")
        cl = headers.get("content-length")
        if te is not None and cl is not None:
            raise HTTPError(400, "both Transfer-Encoding and Content-Length",
                            close=True)
        if te is not None:
            if version < (1, 1):
                raise HTTPError(400, "Transfer-Encoding in an HTTP/1.0 request",
                                close=True)
            if te.strip().lower() != "chunked":
                raise HTTPError(501, f"transfer-coding {te!r} not supported",
                                close=True)
            self.maybe_continue(headers, version)
            body = self.read_chunked()
        elif cl is not None:
            if not cl.isdigit():
                raise HTTPError(400, "Content-Length is not a number", close=True)
            n = int(cl)
            if n > MAX_BODY:
                raise HTTPError(413, "body larger than %d bytes" % MAX_BODY,
                                close=True)
            if n:
                self.maybe_continue(headers, version)
            body = self.read_exact(n)
        else:
            body = b""

        self.deadline = None
        return Request(method, target, version, headers, body)

    def read_chunked(self):
        body = bytearray()
        while True:
            size_line = self.read_line(1024).decode("latin-1")
            size_hex = size_line.split(";", 1)[0].strip()
            if not re.fullmatch(r"[0-9A-Fa-f]{1,8}", size_hex):
                raise HTTPError(400, "bad chunk size", close=True)
            size = int(size_hex, 16)
            if size == 0:
                break
            if len(body) + size > MAX_BODY:
                raise HTTPError(413, "body larger than %d bytes" % MAX_BODY,
                                close=True)
            body += self.read_exact(size)
            if self.read_line(2) != b"":
                raise HTTPError(400, "chunk data longer than its size", close=True)
        total = 0
        while True:
            line = self.read_line(MAX_HEAD)
            total += len(line)
            if total > MAX_HEAD:
                raise HTTPError(431, "trailers too large", close=True)
            if not line:
                return bytes(body)

    def maybe_continue(self, headers, version):
        if version >= (1, 1) and headers.get("expect", "").lower() == "100-continue":
            self.outbuf += b"HTTP/1.1 100 Continue\r\n\r\n"

    def respond(self, status, body, keep_alive, version=(1, 1), head_only=False,
                extra=()):
        body = body.encode() if isinstance(body, str) else body
        lines = [f"HTTP/1.1 {status} {REASON[status]}",
                 f"Date: {formatdate(usegmt=True)}",
                 "Server: calc/1.0",
                 "Content-Type: text/plain; charset=utf-8",
                 f"Content-Length: {len(body)}"]
        lines += extra
        if not keep_alive:
            lines.append("Connection: close")
        else:
            if version < (1, 1):
                lines.append("Connection: keep-alive")
            lines.append(f"Keep-Alive: timeout={IDLE_TIMEOUT:g}")
        self.outbuf += ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
        if not head_only:
            self.outbuf += body
        if len(self.outbuf) >= RECV_SIZE:
            self.flush()

    def close(self):
        try:
            self.flush()
            self.sock.shutdown(socket.SHUT_WR)
            self.sock.settimeout(1.0)
            end = time.monotonic() + 2.0
            while time.monotonic() < end and self.sock.recv(RECV_SIZE):
                pass
        except OSError:
            pass
        finally:
            self.sock.close()

    def serve(self):
        log(f"{self.tag()} OPEN  (one TCP handshake)")
        reason = "client closed"
        try:
            while True:
                try:
                    req = self.read_request()
                except HTTPError as e:
                    self.served += 1
                    log(f"{self.tag()} #{self.served} <unparseable> -> "
                        f"{e.status} {e}")
                    self.respond(e.status, str(e) + "\n", keep_alive=False)
                    reason = f"framing error ({e.status})"
                    break
                if req is None:
                    break

                self.served += 1
                keep = wants_keep_alive(req)
                try:
                    status, body, extra = handle(req)
                except HTTPError as e:
                    status, body, extra = e.status, e.message + "\n", e.headers
                log(f"{self.tag()} #{self.served} {req.method} {req.target} "
                    f"HTTP/{req.version[0]}.{req.version[1]} -> {status} "
                    f"{body.strip()!r}" + ("" if keep else "  [close]"))
                self.respond(status, body, keep, req.version,
                             head_only=(req.method == "HEAD"), extra=extra)
                if not keep:
                    reason = "Connection: close"
                    break
        except IdleTimeout:
            reason = f"idle {IDLE_TIMEOUT:g}s"
        except ClientGone:
            reason = "client vanished mid-request"
        except OSError as e:
            reason = f"socket error: {e}"
        finally:
            self.close()
            log(f"{self.tag()} CLOSE after {self.served} response(s) - {reason}")


def wants_keep_alive(req):
    conn = req.tokens("connection")
    if "close" in conn:
        return False
    if req.version >= (1, 1):
        return True
    return "keep-alive" in conn


def handle(req):
    if req.version >= (1, 1) and "host" not in req.headers:
        raise HTTPError(400, "HTTP/1.1 requires a Host header")

    target = req.target
    if target.lower().startswith(("http://", "https://")):
        target = "/" + target.split("://", 1)[1].partition("/")[2]
    if not target.startswith("/"):
        raise HTTPError(400, "request target must start with /")
    path, _, query = target.partition("?")
    op = unquote(path).strip("/") if path.count("/") == 1 else None

    if op not in OPS:
        raise HTTPError(404, f"no such operation: {path} (try add, sub, mul, div)")
    if req.method not in ("GET", "HEAD"):
        raise HTTPError(405, f"{req.method} not allowed on {path}",
                        headers=["Allow: GET, HEAD"])

    args = {}
    for pair in filter(None, query.split("&")):
        k, _, v = pair.partition("=")
        k = unquote_plus(k)
        if k in args:
            raise HTTPError(400, f"parameter {k!r} given twice")
        args[k] = unquote_plus(v)
    a, b = number(args, "a"), number(args, "b")

    if op == "div" and b == 0:
        raise HTTPError(400, "division by zero")
    with localcontext() as ctx:
        ctx.prec = 34 if op == "div" else 200
        return 200, fmt(OPS[op](a, b)), []


def number(args, name):
    if name not in args:
        raise HTTPError(400, f"missing parameter {name!r}")
    raw = args[name].strip()
    if len(raw) > MAX_NUMBER_LEN or not NUMBER.fullmatch(raw):
        raise HTTPError(400, f"{name}={args[name]!r} is not a number")
    return Decimal(raw)


def fmt(d):
    return "0" if d == 0 else format(d.normalize(), "f")


def listen(host, port):
    socks = []
    for fam, typ, proto, _, addr in socket.getaddrinfo(
            host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE):
        s = socket.socket(fam, typ, proto)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if fam == socket.AF_INET6:
            s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        try:
            s.bind(addr)
        except OSError as e:
            log(f"cannot bind {addr[0]}:{addr[1]}: {e}")
            s.close()
            continue
        s.listen(128)
        socks.append(s)
    if not socks:
        sys.exit(f"could not listen on {host}:{port}")
    return socks


def main():
    global IDLE_TIMEOUT, REQUEST_TIMEOUT
    ap = argparse.ArgumentParser(description="HTTP/1.1 calculator on a bare socket")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--idle-timeout", type=float, default=IDLE_TIMEOUT)
    ap.add_argument("--request-timeout", type=float, default=REQUEST_TIMEOUT)
    opts = ap.parse_args()
    IDLE_TIMEOUT, REQUEST_TIMEOUT = opts.idle_timeout, opts.request_timeout

    sel = selectors.DefaultSelector()
    for s in listen(opts.host, opts.port):
        sel.register(s, selectors.EVENT_READ)
        a = s.getsockname()
        log(f"listening on {a[0]}:{a[1]}  idle-timeout={IDLE_TIMEOUT:g}s "
            f"request-timeout={REQUEST_TIMEOUT:g}s")

    try:
        while True:
            for key, _ in sel.select():
                try:
                    sock, peer = key.fileobj.accept()
                except OSError:
                    continue
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                conn = Connection(sock, peer)
                threading.Thread(target=conn.serve, daemon=True).start()
    except KeyboardInterrupt:
        log("bye")


if __name__ == "__main__":
    main()
