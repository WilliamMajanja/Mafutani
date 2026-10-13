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

The compiler is discovered, so this works on any broker's terminal without
editing a path:

```bash
python3 scripts/build_mt5_ea.py --list      # show what was found
python3 scripts/build_mt5_ea.py             # compile only
python3 scripts/build_mt5_ea.py --install   # compile + install into terminals
python3 scripts/build_mt5_ea.py --install --terminal <id>   # just one
```

It searches Program Files and the local MetaQuotes tree for
`MetaEditor64.exe`, compiles with the documented `/compile:` and `/log:`
switches, and reports `Result: 0 errors, 0 warnings`. With more than one
MetaEditor installed it prints the ambiguity and lets you choose with
`--metaeditor`.

**Any broker's MetaEditor can build this EA.** It is not broker-specific,
and nothing in the repository references a particular terminal or
installation path.

`.ex5` is a build artifact tied to the MetaEditor build that produced it, so
it is not committed (see `.gitignore`). Rebuild after a terminal update.

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

## A bug that compiled, passed every test, and was fatal

The EA hand-rolls its JSON parsing (MQL5 gives us nothing dependable here).
The original `ExtractCommandValue` found `"command":` and cut the value at
the **first comma**. The bridge sends:

```
{"ok":true,"command":{"id":"cmd-1","op":"submit","order":{"symbol":"EURUSD",...}}}
```

so `"command"` extracted as `{"op": "submit"` — 16 characters, truncated at
the comma inside the nested `order` object. `DoSubmit` then read `symbol`,
`qty` and `price` off that fragment, found them **all empty**, and refused
every order.

A second layer compounded it: `json.dumps` emits `"command": {` *with a
space*, and the parser assumed `"command":{`. The space sent the value down
the bare-scalar branch and truncated it again.

It compiled clean and the whole Python suite passed, because every test only
ever fed the parser flat bodies like `{"op":"ping"}`. The preflight passes
too — `SimEA` speaks the protocol from the Python side, so it never runs the
MQL5 parser at all.

Fixed by brace-matching nested objects with a depth counter, skipping
strings so a brace inside a value cannot end the scan early, and reading
order fields from the unwrapped `order` object.

`tests/test_mt5_mql5_parser.py` ports the parser to Python and runs it
against real bridge output — the truncated case, commas and braces inside
string values, and a drift check that fails if the MQL5 and the port stop
matching. A port that quietly diverges would be worse than no port.

Same shape as two earlier bugs today: it compiled, the tests passed, and the
component did not work.

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

The EA **compiles**: `Result: 0 errors, 0 warnings`, confirmed against a
real MetaEditor via `scripts/build_mt5_ea.py`, and installed into every
terminal this machine has.

Not yet verified: **runtime behaviour inside a live terminal.** Nothing has
confirmed the terminal actually polls, that the WebRequest whitelist accepts
the URL, or that `OrderSend` behaves as expected on a broker's account.
Those need a terminal running with the EA attached.

Order of operations, and it is not optional:

1. `AllowTrading = false`, attach to a chart, confirm the Experts log shows
   `MafutaniBridge: polling ...` and `AllowTrading=false`.
2. Start the bridge, call `connect()` from the app, check `terminal_info()`
   returns a real build number.
3. Only then consider `AllowTrading = true` — and first on a **demo**
   account.
