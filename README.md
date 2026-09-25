# A calculator that stays on the line

**Name:** Rohan Ranjan  
**Roll No.:** 24BCS10428

An HTTP/1.1 calculator server written with nothing but a socket: Python 3
standard library only, no `http.server`, no framework. One TCP connection
carries every request, and requests can be pipelined.

## Run it

```sh
python3 server.py                    # listens on localhost:8080 (IPv4 and IPv6)
python3 grade.py                     # the marking script from the slide
python3 grade.py --pipeline          # same six requests, sent in one write
python3 tests.py                     # 18 framing tests, starts its own server
```

Needs Python 3.8 or newer and nothing else. Flags: `--port`, `--host`,
`--idle-timeout`, `--request-timeout`.

```
$ python3 grade.py
  GET /add?a=2&b=3       -> 200  5                             ok
  GET /sub?a=10&b=4      -> 200  6                             ok
  GET /mul?a=6&b=7       -> 200  42                            ok
  GET /div?a=1&b=0       -> 400  division by zero              ok
  GET /pow?a=2&b=8       -> 404  no such operation: /pow (try  ok
  POST /add              -> 405  POST not allowed on /add      ok

  socket still open: True
  1 TCP handshake, 6 responses
```

The server logs every connection, so you can count the handshakes yourself:

```
conn#1 ::1:63561 OPEN  (one TCP handshake)
conn#1 ::1:63561 #1 GET /add?a=2&b=3 HTTP/1.1 -> 200 '5'
...
conn#1 ::1:63561 #6 POST /add HTTP/1.1 -> 405 'POST not allowed on /add'
conn#1 ::1:63561 CLOSE after 6 response(s) - client closed
```

## The feature set

| request | status | body |
|---|---|---|
| `GET /add?a=2&b=3` | 200 | `5` |
| `GET /sub?a=10&b=4` | 200 | `6` |
| `GET /mul?a=6&b=7` | 200 | `42` |
| `GET /div?a=9&b=3` | 200 | `3` |
| `GET /div?a=1&b=0` | 400 | division by zero |
| `GET /add?a=x&b=3` | 400 | not a number |
| `GET /pow?a=2&b=8` | 404 | no such operation |
| `POST /add` | 405 | with `Allow: GET, HEAD` |
| `GET /add` with no `Host` | 400 | HTTP/1.1 requires Host |

The arithmetic uses `Decimal`, not `float`, so `0.1 + 0.2` returns `0.3`,
`9 / 3` returns `3` (not `3.0`), and large integers stay exact. Division
rounds to 34 significant digits. Operands are plain decimals of at most 64
characters. Exponents, `nan` and `inf` are rejected, so nobody can ask for
`1e999999999` and make the server print a billion zeros.

## Stretch goals (all four done)

- **`Connection: close` is honoured.** The server answers, then closes.
  HTTP/1.0 clients get 1.0 behaviour: close by default, stay open only with
  `Connection: keep-alive`. The close is a *lingering close*: `shutdown(WR)`,
  a short drain, then `close()`. Closing a socket that still has unread input
  makes the kernel send RST, and an RST can destroy the response before the
  client reads it.
- **Idle timeout: 15 s between requests, plus 10 s to finish one request.**
  - Every open connection costs a thread and a file descriptor. A client
    that goes quiet should give them back.
  - 15 s is long enough for a person, a script, or a browser to send the
    next request on the same line, and short enough that idle sockets don't
    pile up. For comparison, Apache defaults to 5 s and nginx to 75 s. The
    value is sent to the client as `Keep-Alive: timeout=15` so it can plan
    around it.
  - Idle time and "halfway through a request" time are timed separately.
    Once the first byte of a request arrives, the whole request has 10 s to
    finish. Without that limit, a slowloris client could send one header
    byte every 14 s and hold the thread forever. A request that stalls gets
    `408` and a close.
  - Both values can be changed with `--idle-timeout` and
    `--request-timeout`.
- **Chunked request bodies** are decoded, with chunk extensions and trailers
  handled. `Expect: 100-continue` gets an interim `100 Continue`, so curl
  doesn't stall for a second before sending a body.
- **Pipelining.** Send all six requests at once and they are answered in
  order. Responses go into an output buffer that is flushed just before the
  server would block on `recv()`, so six pipelined requests usually get six
  responses in a single write.

## Hardening that came along for free

| limit | value | on breach |
|---|---|---|
| request line + headers | 8 KiB | 431, close |
| request body | 1 MiB | 413, close |
| operand length | 64 chars | 400, stay open |
| `Content-Length` + `Transfer-Encoding` together | refused | 400, close |
| duplicate `Host`, conflicting `Content-Length` | refused | 400, close |
| whitespace before `:` in a header, obsolete line folding | refused | 400, close |
| HTTP/2.x or other major versions | refused | 505, close |

## Files

| file | what it is |
|---|---|
| `server.py` | the server |
| `grade.py` | the slide's marking script, with a correct Content-Length-based response reader |
| `tests.py` | 18 tests: the slide table, pipelining, one byte per `send()`, bodies that look like requests, chunked, smuggling, timeouts, slowloris, and concurrency |

## Design choices

- **One thread per connection.** Each thread blocks on its own socket and
  handles its requests strictly in order, which is exactly what HTTP/1.1
  requires. Concurrency comes from running many connections at once, not
  from running requests on one connection at once.
- **Binds every address `localhost` resolves to.** On macOS,
  `create_connection(("localhost", 8080))` tries `::1` first, and the
  server listens there as well as on `127.0.0.1`.
- **Response bodies are just the number** (`5`, not `5\n`), matching the
  slide.
