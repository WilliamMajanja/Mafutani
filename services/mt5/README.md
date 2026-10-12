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

The MQL5 source has **not been compiled** — there is no terminal here, and
`MetaEditor` is not available under WSL2. Everything on the Python side is
covered by tests using a `FakeEA` that polls `/next` and posts to `/result`
exactly as the real EA does. The `OrderSend` shim is the part that needs a
real terminal to confirm.
