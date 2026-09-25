#!/usr/bin/env python3
import os, socket, subprocess, sys, time
from grade import read_response, still_open

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = None
TESTS = []


def start_server():
    global PORT
    s = socket.socket(); s.bind(("127.0.0.1", 0)); PORT = s.getsockname()[1]; s.close()
    proc = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "server.py"), "--host", "127.0.0.1",
         "--port", str(PORT), "--idle-timeout", "1.5", "--request-timeout", "1.5"],
        stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", PORT), timeout=0.2).close()
            return proc
        except OSError:
            time.sleep(0.1)
    proc.kill()
    sys.exit("server did not start")


def conn():
    s = socket.create_connection(("127.0.0.1", PORT))
    s.settimeout(5)
    return s


def get(target, extra=""):
    return f"GET {target} HTTP/1.1\r\nHost: x\r\n{extra}\r\n".encode()


def test(name):
    def register(fn):
        TESTS.append((name, fn))
        return fn
    return register


def expect(sock, buf, status, body=None):
    got, headers, b = read_response(sock, buf)
    assert got == status, f"status {got}, wanted {status} (body {b!r})"
    if body is not None:
        assert b == body, f"body {b!r}, wanted {body!r}"
    return headers


def eof(sock):
    try:
        return sock.recv(1) == b""
    except (ConnectionResetError, socket.timeout):
        return False


def slide_table():
    s, buf = conn(), bytearray()
    cases = [("/add?a=2&b=3", 200, b"5"), ("/sub?a=10&b=4", 200, b"6"),
             ("/mul?a=6&b=7", 200, b"42"), ("/div?a=9&b=3", 200, b"3"),
             ("/div?a=1&b=0", 400, None), ("/add?a=x&b=3", 400, None),
             ("/pow?a=2&b=8", 404, None)]
    for t, st, body in cases:
        s.sendall(get(t)); expect(s, buf, st, body)
    s.sendall(b"POST /add HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n")
    h = expect(s, buf, 405)
    assert "GET" in h.get("allow", ""), "405 must carry Allow"
    s.sendall(b"GET /add?a=1&b=1 HTTP/1.1\r\n\r\n")
    expect(s, buf, 400)
    s.sendall(get("/add?a=1&b=1")); expect(s, buf, 200, b"2")
    assert still_open(s), "socket died"


test("every row of the slide on ONE socket, still open after")(slide_table)


@test("pipelining: 6 requests in one write, 6 answers in order")
def _():
    s, buf = conn(), bytearray()
    s.sendall(b"".join(get(f"/add?a={i}&b=100") for i in range(6)))
    for i in range(6):
        expect(s, buf, 200, str(100 + i).encode())
    assert still_open(s)


@test("one byte per send(): request split across many packets")
def _():
    s, buf = conn(), bytearray()
    for b in get("/mul?a=12&b=12"):
        s.send(bytes([b])); time.sleep(0.001)
    expect(s, buf, 200, b"144")


@test("POST body is consumed exactly: byte n+1 is the next request")
def _():
    s, buf = conn(), bytearray()
    body = b"GET /pow HTTP/1.1\r\n\r\n"
    s.sendall(b"POST /add HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\n\r\n" % len(body)
              + body + get("/sub?a=10&b=4"))
    expect(s, buf, 405)
    expect(s, buf, 200, b"6")
    assert still_open(s)


@test("GET with a body: body skipped, next request intact")
def _():
    s, buf = conn(), bytearray()
    s.sendall(get("/add?a=2&b=3", "Content-Length: 5\r\n") + b"hello"
              + get("/div?a=9&b=3"))
    expect(s, buf, 200, b"5")
    expect(s, buf, 200, b"3")


@test("chunked request body, then another request on the same socket")
def _():
    s, buf = conn(), bytearray()
    s.sendall(b"POST /add HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
              b"4\r\nwiki\r\n5;ext=1\r\npedia\r\n0\r\nX-Trailer: y\r\n\r\n"
              + get("/add?a=2&b=2"))
    expect(s, buf, 405)
    expect(s, buf, 200, b"4")
    assert still_open(s)


@test("Expect: 100-continue gets an interim 100, then the answer")
def _():
    s = conn()
    s.sendall(b"POST /add HTTP/1.1\r\nHost: x\r\nContent-Length: 3\r\n"
              b"Expect: 100-continue\r\n\r\n")
    time.sleep(0.2)
    assert s.recv(100).startswith(b"HTTP/1.1 100 Continue\r\n\r\n")
    s.sendall(b"abc")
    expect(s, bytearray(), 405)


@test("Content-Length AND Transfer-Encoding: 400 and close (smuggling)")
def _():
    s, buf = conn(), bytearray()
    s.sendall(b"POST /add HTTP/1.1\r\nHost: x\r\nContent-Length: 3\r\n"
              b"Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n")
    h = expect(s, buf, 400)
    assert h.get("connection") == "close" and eof(s)


@test("garbage request line: 400 and close")
def _():
    s, buf = conn(), bytearray()
    s.sendall(b"HELLO THERE\r\n\r\n")
    expect(s, buf, 400)
    assert eof(s)


@test("bad query values keep the connection open (framing was fine)")
def _():
    s, buf = conn(), bytearray()
    for t in ["/add?a=1", "/add?a=1&b=", "/add?a=nan&b=1", "/add?a=1e9&b=1",
              "/add?a=1&a=2&b=3"]:
        s.sendall(get(t)); expect(s, buf, 400)
    s.sendall(get("/add?a=1&b=1")); expect(s, buf, 200, b"2")


@test("arithmetic: negatives, decimals, big ints, 1/3, 10/4, percent-encoding")
def _():
    s, buf = conn(), bytearray()
    for t, want in [("/sub?a=3&b=10", b"-7"), ("/add?a=0.1&b=0.2", b"0.3"),
                    ("/div?a=10&b=4", b"2.5"), ("/div?a=-9&b=3", b"-3"),
                    ("/mul?a=99999999999999999999&b=99999999999999999999",
                     b"9999999999999999999800000000000000000001"),
                    ("/div?a=1&b=3", b"0.3333333333333333333333333333333333"),
                    ("/add?a=%2B5&b=-5", b"0"), ("/add?b=3&a=2", b"5"),
                    ("http://x/add?a=2&b=3", b"5")]:
        s.sendall(get(t)); expect(s, buf, 200, want)


@test("HEAD: headers only, no body, connection stays usable")
def _():
    s, buf = conn(), bytearray()
    s.sendall(b"HEAD /add?a=2&b=3 HTTP/1.1\r\nHost: x\r\n\r\n" + get("/add?a=1&b=1"))
    time.sleep(0.2)
    data = s.recv(4096)
    first, _, rest = data.partition(b"\r\n\r\n")
    assert b"Content-Length: 1" in first and rest.startswith(b"HTTP/1.1 200"), data


@test("Connection: close is honoured: answer, then EOF")
def _():
    s, buf = conn(), bytearray()
    s.sendall(get("/add?a=2&b=3", "Connection: close\r\n"))
    h = expect(s, buf, 200, b"5")
    assert h.get("connection") == "close" and eof(s)


@test("HTTP/1.0 closes by default, stays with Connection: keep-alive")
def _():
    s, buf = conn(), bytearray()
    s.sendall(b"GET /add?a=1&b=1 HTTP/1.0\r\n\r\n")
    expect(s, buf, 200, b"2"); assert eof(s)
    s, buf = conn(), bytearray()
    s.sendall(b"GET /add?a=1&b=1 HTTP/1.0\r\nConnection: keep-alive\r\n\r\n")
    h = expect(s, buf, 200, b"2")
    assert h.get("connection") == "keep-alive" and still_open(s)


@test("idle timeout: quiet connection is closed (server run with 1.5s)")
def _():
    s, buf = conn(), bytearray()
    s.sendall(get("/add?a=1&b=1")); expect(s, buf, 200)
    time.sleep(1.0); assert still_open(s), "closed too early"
    s.sendall(get("/add?a=1&b=1")); expect(s, buf, 200)
    time.sleep(2.2)
    assert eof(s), "should have closed after idle timeout"


@test("slowloris: half a request, then silence -> 408 and close")
def _():
    s, buf = conn(), bytearray()
    s.sendall(b"GET /add?a=1&b=1 HTTP/1.1\r\nHo")
    s.settimeout(4)
    expect(s, buf, 408); assert eof(s)


@test("oversized header block -> 431 and close")
def _():
    s, buf = conn(), bytearray()
    s.sendall(b"GET /add?a=1&b=1 HTTP/1.1\r\nHost: x\r\nX: " + b"a" * 10000 + b"\r\n\r\n")
    expect(s, buf, 431)


@test("many concurrent connections, each on its own line")
def _():
    socks = [conn() for _ in range(20)]
    for i, s in enumerate(socks):
        s.sendall(get(f"/mul?a={i}&b=2"))
    for i, s in enumerate(socks):
        expect(s, bytearray(), 200, str(i * 2).encode())
        s.close()


if __name__ == "__main__":
    proc = start_server()
    failed = 0
    try:
        for name, fn in TESTS:
            try:
                fn()
                print(f"  PASS  {name}")
            except Exception as e:
                failed += 1
                print(f"  FAIL  {name}\n        {type(e).__name__}: {e}")
    finally:
        proc.terminate()
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} passed")
    sys.exit(1 if failed else 0)
