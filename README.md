# A calculator that stays on the line

**Name:** Rohan Ranjan  
**Roll No.:** 24BCS10428

An HTTP/1.1 calculator written on a bare TCP socket (Python 3 standard
library only, with no `http.server` and no framework). One TCP connection
carries every request, and pipelined requests are answered in order.

```sh
python3 server.py                  # listens on localhost:8080, 15s idle timeout
python3 grade.py                   # the marking script: 1 socket, 6 requests
python3 grade.py --pipeline        # same six requests sent in one write
python3 tests.py                   # 18 tests; starts its own server with short timeouts
```

| Request | Response |
|---|---|
| `GET /add?a=2&b=3` | 200 `5` |
| `GET /sub?a=10&b=4` | 200 `6` |
| `GET /mul?a=6&b=7` | 200 `42` |
| `GET /div?a=9&b=3` | 200 `3` |
| `GET /div?a=1&b=0` | 400 |
| `GET /add?a=x&b=3` | 400 |
| `GET /pow?a=2&b=8` | 404 |
| `POST /add` | 405 (with `Allow: GET, HEAD`) |
| `GET /add` with no `Host` | 400 |

## Two kinds of 400

| Kind | Example | Connection |
|---|---|---|
| Bad content, good framing | `a=x`, `b=0`, missing `Host` | stays open: the server knows where the next request starts |
| Bad framing | `Content-Length: abc`, both `Content-Length` and `Transfer-Encoding`, garbage request line, `Host : x` | closed: the next request boundary is unknown, and guessing it is how request smuggling happens |

The server reads a request's body even when it rejects that request with
405. Otherwise the unread body would be taken as the start of the next
request.

## Also handled

- **Pipelining:** several requests sent at once are answered in order, and
  the responses usually leave in a single write.
- **`Connection: close`:** the server answers, then closes. HTTP/1.0
  clients close by default unless they send `Connection: keep-alive`.
- **Timeouts:** 15 s of silence between requests closes the connection. A
  request that starts but doesn't finish within 10 s gets `408`, which stops
  slowloris clients. Both are adjustable with `--idle-timeout` and
  `--request-timeout`.
- **Chunked request bodies,** including chunk extensions and trailers.
- **`Expect: 100-continue`:** the server sends `100 Continue` before reading
  the body, so curl doesn't wait.
- **`HEAD`:** returns the same headers as `GET`, with no body.
- **Limits:** 8 KiB for the request line and headers (`431`), 1 MiB body
  (`413`), 64-character operands.
- **Exact arithmetic:** results use `Decimal`, not `float`, so integer
  results stay integers (`9/3` gives `3`, not `3.0`), non-exact division
  gives a decimal (`1/4` gives `0.25`), and `0.1+0.2` gives `0.3`.
- **IPv4 and IPv6:** the server listens on both `127.0.0.1` and `::1`,
  because `localhost` on macOS resolves to `::1` first.
