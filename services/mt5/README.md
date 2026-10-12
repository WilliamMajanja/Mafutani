# MetaTrader 5 bridge

Wires MetaTrader 5 into Mafutani without adding a dependency to the app.

```
Mafutani app (WSL2)  --HTTP-->  bridge_server.py (Windows, stdlib)
                                          ^
                                          | WebRequest() long-poll
                                    MafutaniBridge.mq5 (in the MT5 terminal)
```

## Why the EA is a client, not a server

MQL5 has no server primitive. `WebRequest()` is strictly outbound, and
every URL it must be whitelisted in **Tools → Options → Expert Advisors →
Allow WebRequest**. An EA cannot listen on a port.

So the usual "expose an API" arrangement is inverted: a Python server runs
beside the terminal and the EA polls it. That is not a workaround — it puts
everything testable (protocol, queue, retcode mapping, live gate) on the
Python side, and leaves only the thin `OrderSend` shim in MQL5.

## The rule that matters

**`retcode 10009` (TRADE_RETCODE_DONE) does not mean the order filled.**

It means the terminal accepted the request for processing. The order may
still be rejected a moment later, may sit unfilled, or may fill partially.
This is the easiest way to lose real money silently, because the response
looks like success.

`submit_order` therefore reports `status="open"`, never `"filled"`, and
sets `raw["filled"] = False`. A fill is only ever learned from an actual
`OnTradeTransaction` in the EA. `tests/test_mt5_bridge.py` pins this, and
mutating `status` back to `"filled"` fails the suite.

Unmapped retcodes fail **closed** — MT5 adds codes over time, and a
permissive default would hide them.

## Setup

**1. Bridge server**, on the Windows host beside the terminal:

```bash
python services/mt5/bridge_server.py --port 8031 --token "$MAFUTANI_MT5_TOKEN"
```

It refuses to start without a token unless you pass `--insecure-no-token`,
and refuses to bind a non-loopback host without the same override —
anything that can reach that port can place orders.

**2. EA**: compile `services/mt5/MafutaniBridge.mq5` in MetaEditor, attach
to a chart, and add the bridge URL to the WebRequest whitelist:

```
http://127.0.0.1:8031
```

Start with **`AllowTrading = false`**. Reads (account, quotes, orders)
work; order submission is refused. Flip it only after the read path is
verified against your broker.

**3. App side**:

```bash
export MAFUTANI_MT5_HOST=127.0.0.1
export MAFUTANI_MT5_PORT=8031
export MAFUTANI_MT5_TOKEN=...      # must match the bridge
```

Then `broker: "mt5"` in the normal order path.

## Compiling the EA

Already done on this machine with the terminal's own MetaEditor:

```bash
"/mnt/c/Program Files/NAGA Capital MT5 Terminal/MetaEditor64.exe" \
  /compile:"C:\\path\\to\\MafutaniBridge.mq5" /log:"C:\\path\\build.log"
```

Writes `MafutaniBridge.ex5` beside the source and reports
`Result: 0 errors, 0 warnings` in the log.

## Three things only the compiler could tell us

The first draft of this EA was written from documentation and **did not
compile — 9 errors.** All three below are fixed. They are exactly the kind
of thing that reading docs does not reveal.

### 1. MQL5 cannot set headers on a POST

`WebRequest` offers only:

```
(method, url, cookie, headers, timeout, data, data_size, result, result_headers)
(method, url, timeout, data, result, result_headers)
```

Neither accepts headers *and* a body together. So the EA physically cannot
send `X-Mafutani-Token` on its `POST /result`, and **the token travels as a
query parameter** (`/result?token=...`). The bridge accepts either carrier;
the Python side still uses the header.

A token in a URL is weaker than one in a header — it can land in access
logs — which is part of why the listener stays on loopback.

### 2. `data` is `const char&[]`, so a dynamic `char[]` does not fit

Passing a resized dynamic array fails with "no one of the overloads can be
applied". It must be passed by reference with an explicit size, via the
9-argument overload.

### 3. `IsTradeContextBusy()` and `MQL_TRADE_CONTEXT_BUSY` do not exist

Neither resolves in this build. The EA no longer pre-checks for a busy
trade context; the retcode is the mechanism — a second concurrent
`OrderSend` returns 10025, which `brokers/mt5.py` already maps.

Also: `CTrade.SetExpertMagicNumber` returns **void**, so it cannot be used
in an `if` statement.

## Safety properties

| Property | Where |
|---|---|
| Live gate (env + tier + confirm) runs **before** any command is queued | `assert_live_allowed` in `submit_order` |
| Accepted ≠ filled | `status="open"`, `raw["filled"]=False` |
| Unknown retcodes fail closed | `map_retcode` |
| A lost result is never retried automatically | 504 → `retryable=False` |
| Commands older than 120s are dropped, not executed | `BridgeState.take` |
| Plaintext refused to non-loopback hosts | `mt5.build` |
| Server refuses to start unauthenticated | `bridge_server.serve` |
| Token compared with `hmac.compare_digest` | `_Handler._authorized` |

The last one deserves a note: a bridge that stalls because the EA is
misconfigured looks exactly like a dead terminal. Both produce a timeout,
but they need different operator responses, and only the token can tell
you which it is.

## If the result is lost

A timeout means the command **may have executed** at the terminal with only
the reply lost. The adapter says so explicitly and does not retry. Reconcile
before retrying:

```
GET /exec {"op": "open_orders"}
```

## WSL2 note

The app runs in WSL2 and the terminal on Windows, so `127.0.0.1` is not the
same host on both sides. Use the Windows host address
(`MAFUTANI_MT5_HOST`), and keep the bridge bound to loopback on Windows.

## Not verified

The EA **compiles**: `Result: 0 errors, 0 warnings`, confirmed against the
real MetaEditor on this machine, and the `.ex5` is installed at
`AppData/Roaming/MetaQuotes/Terminal/<hash>/MQL5/Experts/`.

Not yet verified: **runtime behaviour inside a live terminal.** Nothing has
confirmed the terminal actually polls, that the WebRequest whitelist accepts
the URL, or that `OrderSend` behaves as expected on your broker's account.
Those need the terminal running with the EA attached.

Order of operations, and it is not optional:

1. `AllowTrading = false`, attach to a chart, confirm the Experts log shows
   `MafutaniBridge: polling ...` and `AllowTrading=false`.
2. Start the bridge, call `connect()` from the app, check `terminal_info()`
   returns a real build number.
3. Only then consider `AllowTrading = true` — and first on a **demo**
   account.
