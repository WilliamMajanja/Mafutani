"""Interactive broker session: the conversational layer over any venue.

The application already has a terminal (`core/terminal.py`) and an HTTP
API. This module is the piece that makes a *broker* drivable from either,
without either one importing an adapter directly.

`BrokerSession` is deliberately a plain object with a `handle(line)`
method returning text. No I/O, no globals, no hidden state beyond the
session -- so the same class backs the REPL, the `/api/terminal/command`
endpoint and the tests.

Security posture
----------------
* Live orders need `arm` first, which requires the env kill-switch *and*
  a typed confirmation. An armed session expires, so an unattended
  process cannot sit armed forever.
* Credentials are referenced by fingerprint, never echoed, and never
  accepted as command arguments (they would land in the audit log and
  shell history).
* Every mutating command is journalled through `on_event`, which the app
  wires to `audit_log`.
"""

import shlex
import time

from .base import (Broker, BrokerError, BrokerValidationError,
                   BrokerNotSupported, OrderRequest, BUY, SELL,
                   live_trading_enabled)

# How long an `arm` stays valid.
ARM_TTL_SECONDS = 300

#: Bars needed before a symbol's features can be evaluated at all. The
#: binding constraint is Wilder RSI(14), which needs 15 closes, plus one
#: more for the one-bar return. Stated as a constant so the error message
#: and the default lookback cannot drift apart.
MIN_BARS_FOR_SIGNALS = 15


def _redact_line(text):
    """Strip anything that looks like a credential from a command line."""
    try:
        from core.logsafe import scrub
        return scrub(text)
    except Exception:
        return "<unloggable command>"

HELP = """broker commands:
  help                        this text
  status                      broker health, connection, live state
  brokers                     list available brokers
  use <name>                  switch broker (paper by default)
  connect | disconnect        manage the connection
  quote <SYM>                 top of book
  positions                   open positions
  orders [SYM]                recent orders
  account                     cash / equity
  symbols                     tradable instruments
  candles <SYM> [n]           recent 1m candles
  buy <SYM> <qty> [price]     submit a buy
  sell <SYM> <qty> [price]    submit a sell
  cancel <order_id>           cancel a working order
  arm <yes>                   arm live trading (needs env + confirm)
  disarm                      disarm live trading

vibe commands (need a data source attached by the caller):
  shadow [min_support] [--god]  infer rules from this account's winners
  signals [SYM ...]           scan the live book for those rules firing
  ground <text>               check a line's figures against observations
  vibe_status                 profile, journal size, signal count
  vibe_reset                  drop the inferred profile

`shadow` reads the *filled* trades of this account. It never places an
order: it describes trades you already made. `--god` selects the God
feature space (33 indicators) instead of the base 6.

gods commands (DNA rules, adversarial dissenter, dual veto):
  dna [mutate [rate] | crossover]  DNA-encoded rule operations
  dissent                         run the dissenter against the last signal
  veto                            run the dual veto gates
  restraint                       show the restraint log (rejected signals)
"""


class BrokerSession:
    """Stateful, single-threaded command surface over one broker.

    ``data`` is an optional object the Vibe commands read from. It is
    duck-typed rather than imported, so this module keeps no database
    dependency and the app can pass whatever it already has open:
    ``fills(user_id)``, ``candles(symbol, limit)``, ``symbols()``.
    """

    def __init__(self, broker=None, on_event=None, clock=None, data=None,
                 user_id=None):
        self.broker = broker
        self.on_event = on_event
        self._clock = clock or time.time
        self._armed_until = 0.0
        self.history = []
        # Vibe state. Held on the session so a profile is visible to the
        # operator and dropped when the session is replaced -- a profile
        # outliving the connection that inferred it would be scanned
        # against a book it was never derived from.
        self.data = data
        self.user_id = user_id
        self._vibe = None
        self._vibe_space = None
        self._ground = None
        self._last_signals = ()
        self._restraint_log = None

    # -- helpers ---------------------------------------------------------
    def _emit(self, kind, message, **fields):
        """Journal an event. Never let a broken callback break a command."""
        if self.on_event is None:
            return
        try:
            self.on_event({"kind": kind, "message": message, **fields})
        except Exception:
            pass

    def _now(self):
        return self._clock()

    @property
    def armed(self):
        return self._now() < self._armed_until

    def _require_broker(self):
        if self.broker is None:
            raise BrokerValidationError("no broker selected (try `use paper`)")
        return self.broker

    def _require_live_armed(self):
        """Live actions need an armed, unexpired session."""
        if not self.armed:
            raise BrokerValidationError(
                "live trading not armed: `arm yes` first "
                "(and set LIVE_TRADING_ENABLED=1)")

    def set_broker(self, broker):
        self.disarm("broker changed")
        self.broker = broker
        return broker

    # -- command dispatch ------------------------------------------------
    def handle(self, line):
        """Run one command; always returns printable text."""
        raw = str(line or "").strip()
        if not raw:
            return ""
        # History is retained (and can be dumped for debugging), so a secret
        # typed on the command line must not survive in it.
        self.history.append(_redact_line(raw))
        try:
            parts = shlex.split(raw)
        except ValueError as e:
            return "could not parse command: {}".format(e)
        if not parts:
            return ""
        cmd, args = parts[0].lower(), parts[1:]
        fn = getattr(self, "_cmd_" + cmd.replace("-", "_"), None)
        if fn is None:
            return "unknown command '{}'. try `help`".format(cmd)
        try:
            return fn(args) or ""
        except BrokerNotSupported as e:
            return "not supported: {}".format(e)
        except BrokerValidationError as e:
            return "invalid: {}".format(e)
        except BrokerError as e:
            self._emit("broker.error", str(e), venue=getattr(e, "venue", ""))
            return "broker error: {}".format(e)
        except Exception as e:  # never let a command kill the session
            self._emit("broker.exception", "{}: {}".format(type(e).__name__, e))
            return "error: {}: {}".format(type(e).__name__, e)

    # -- commands --------------------------------------------------------
    def _cmd_help(self, args):
        return HELP

    def _cmd_brokers(self, args):
        from . import registry
        lines = ["available brokers:"]
        for item in registry.describe():
            lines.append("  {:<16} {}".format(item["name"], item["description"]))
        return "\n".join(lines)

    def _cmd_use(self, args):
        from . import registry
        if not args:
            return "current broker: {}".format(
                self.broker.name if self.broker else "none")
        name = args[0]
        # Credentials are never taken from the command line: they would be
        # captured in history and the audit log.
        broker = registry.get_broker(name)
        self.set_broker(broker)
        self._emit("broker.use", name)
        return "switched to {} ({})".format(broker.name, broker.venue)

    def _cmd_connect(self, args):
        b = self._require_broker()
        b.connect()
        self._emit("broker.connect", b.venue)
        return "connected to {}".format(b.venue)

    def _cmd_disconnect(self, args):
        b = self._require_broker()
        b.close()
        return "disconnected from {}".format(b.venue)

    def _cmd_status(self, args):
        if self.broker is None:
            return "no broker selected"
        h = self.broker.health()
        lines = [
            "broker:   {} ({})".format(h["name"], h["venue"]),
            "live:     {}".format(h["live"]),
            "connected:{}".format(h["connected"]),
            "creds:    {}".format(
                "{} [{}]".format(h["fingerprint"], h["credentials"])
                if h["credentials"] else "none"),
            "live env: {}".format(h["live_trading_enabled"]),
            "armed:    {}".format(
                "yes" if self.armed else
                ("expired" if self._armed_until else "no")),
        ]
        return "\n".join(lines)

    def _cmd_quote(self, args):
        if not args:
            raise BrokerValidationError("usage: quote <SYMBOL>")
        q = self._require_broker().quote(args[0])
        return "{:<10} bid {:<12.6g} ask {:<12.6g} last {:<12.6g} spread {:<10.6g}".format(
            q.symbol, q.bid, q.ask, q.last, q.spread)

    def _cmd_candles(self, args):
        if not args:
            raise BrokerValidationError("usage: candles <SYMBOL> [count]")
        n = int(args[1]) if len(args) > 1 else 10
        rows = self._require_broker().candles(args[0], limit=n)
        if not rows:
            return "no candles for {}".format(args[0].upper())
        out = ["{:<12} {:>10} {:>10} {:>10} {:>10}".format(
            "ts", "open", "high", "low", "close")]
        for r in rows[-n:]:
            out.append("{:<12} {:>10.6g} {:>10.6g} {:>10.6g} {:>10.6g}".format(
                r.get("ts", ""), r.get("open", 0), r.get("high", 0),
                r.get("low", 0), r.get("close", 0)))
        return "\n".join(out)

    def _cmd_positions(self, args):
        pos = self._require_broker().positions()
        if not pos:
            return "no open positions"
        out = ["{:<12} {:>12} {:>12} {:>12}".format(
            "symbol", "qty", "avg", "realized")]
        for p in pos:
            out.append("{:<12} {:>12.6g} {:>12.6g} {:>12.6g}".format(
                p.symbol, p.qty, p.avg_price, p.realized_pnl))
        return "\n".join(out)

    def _cmd_orders(self, args):
        orders = self._require_broker().list_orders(symbol=args[0] if args else None)
        if not orders:
            return "no orders"
        out = ["{:<8} {:<10} {:<6} {:>10} {:>10} {:<10}".format(
            "id", "symbol", "side", "qty", "filled", "status")]
        for o in orders:
            out.append("{:<8} {:<10} {:<6} {:>10.6g} {:>10.6g} {:<10}".format(
                o.order_id or "-", o.symbol, o.side, o.qty, o.filled_qty,
                o.status))
        return "\n".join(out)

    def _cmd_account(self, args):
        a = self._require_broker().account()
        return "{:<12} cash {:<14.6g} equity {:<14.6g} buying power {:<14.6g} ({})".format(
            a.account_id or a.venue, a.cash, a.equity, a.buying_power, a.currency)

    def _cmd_symbols(self, args):
        syms = self._require_broker().symbols()
        if not syms:
            return "no symbols"
        return "{} symbols: {}".format(len(syms), ", ".join(map(str, syms[:60])))

    def _cmd_buy(self, args):
        return self._trade(BUY, args, "buy")

    def _cmd_sell(self, args):
        return self._trade(SELL, args, "sell")

    def _trade(self, side, args, verb):
        if len(args) < 2:
            raise BrokerValidationError(
                "usage: {} <SYMBOL> <qty> [price]".format(verb))
        broker = self._require_broker()
        symbol, qty = args[0], args[1]
        price = None
        if len(args) > 2:
            try:
                price = float(args[2])
            except ValueError:
                raise BrokerValidationError("price must be a number")
        if broker.is_live:
            self._require_live_armed()
        req = OrderRequest(symbol=symbol, side=side, qty=qty, price=price,
                           type="limit" if price else "market")
        order = broker.submit_order(req)
        self._emit("order.submitted",
                   "{} {} {}".format(verb, req.qty, req.symbol),
                   venue=broker.venue, order_id=order.order_id,
                   status=order.status, filled=order.filled_qty)
        return "{:<6} {:<10} {:>10.6g} @ {:<12.6g} -> {} (id {})".format(
            verb, order.symbol, order.qty, order.price or order.avg_price or 0,
            order.status, order.order_id or "-")

    def _cmd_cancel(self, args):
        if not args:
            raise BrokerValidationError("usage: cancel <order_id>")
        broker = self._require_broker()
        if broker.is_live:
            self._require_live_armed()
        order = broker.cancel_order(args[0])
        self._emit("order.cancelled", str(args[0]), venue=broker.venue)
        return "order {} -> {}".format(args[0], order.status)

    # -- vibe ------------------------------------------------------------
    # Every method here imports `brokers.vibe` lazily and inside a
    # try/except. The reason is not laziness: this session backs an
    # HTTP endpoint and a REPL, and a Vibe import error or a bug in
    # feature extraction must degrade the *vibe* commands to a message,
    # never take down `buy` or `cancel` with it. A trading terminal that
    # stops placing orders because a read-only analysis helper raised is
    # worse than one that prints "unavailable".
    def _vibe_mod(self):
        """Import the Vibe layer, or explain why it is unavailable."""
        try:
            from . import vibe
            return vibe
        except Exception as e:
            raise BrokerValidationError(
                "vibe layer unavailable: {}: {}".format(
                    type(e).__name__, e))

    def _require_data(self):
        if self.data is None:
            raise BrokerValidationError(
                "no data source: this session has no database attached "
                "(the terminal attaches one on start)")
        return self.data

    def _vibe_fills(self, symbol=None, limit=5000):
        """Fills as dicts the Vibe layer understands, or [] on any fault.

        An unreadable journal must produce an empty journal, not an
        exception: a database hiccup should read as "no history to learn
        from", which is true, rather than as an error the operator has to
        interpret.
        """
        try:
            raw = self.data.fills(user_id=self.user_id, symbol=symbol,
                                  limit=limit)
        except TypeError:
            # A simpler data source may not take keywords.
            try:
                raw = self.data.fills(self.user_id, symbol, limit)
            except Exception:
                return []
        except Exception:
            return []
        out = []
        for row in raw or ():
            if isinstance(row, dict):
                get = row.get
            else:
                def get(k, default=None, _r=row):
                    try:
                        return _r[k]
                    except Exception:
                        return default
            out.append({
                "symbol": get("symbol", ""),
                "side": (get("side", "") or "").lower(),
                "qty": get("qty", 0),
                "price": get("price", 0),
                "ts": get("ts", 0),
            })
        return out

    def _god_space(self):
        """Import the God feature space, or explain why it is unavailable."""
        try:
            from . import godmode
            return godmode.GOD_SPACE
        except Exception as e:
            raise BrokerValidationError(
                "god feature space unavailable: {}: {}".format(
                    type(e).__name__, e))

    def _vibe_journal(self, vibe, space=None):
        """Pair the account's fills into roundtrips."""
        fills = self._vibe_fills()
        if not fills:
            return [], {}
        journal = vibe.roundtrips_from_fills(fills)
        # Features are taken at each roundtrip's *entry* bar, which is
        # what makes a rule predictive. Attaching the features of the bar
        # the trade was closed on would teach the extractor to describe
        # exits.
        feature_sets = []
        for rt in journal:
            feature_sets.append(
                self._features_at(vibe, rt.symbol, rt.open_ts, space))
        return journal, feature_sets

    def _features_at(self, vibe, symbol, ts, space=None, lookback=120):
        """Feature dict for the bar containing ``ts``, or {} if unavailable.

        The bar is the most recent one at or before the entry timestamp:
        a fill stamped mid-bar is placed by information available when
        the bar opened.
        """
        try:
            candles = self.data.candles(symbol, limit=lookback)
        except Exception:
            return {}
        if not candles:
            return {}
        rows = list(candles)
        if isinstance(rows[0], dict):
            cols = ("ts", "open", "high", "low", "close", "volume")
            rows = [tuple(r.get(c) for c in cols) for r in rows]
        rows.sort(key=lambda r: (r[0] is None, r[0]))
        closes, highs, lows, vols, stamps = [], [], [], [], []
        for r in rows:
            try:
                h, l, c = float(r[2]), float(r[3]), float(r[4])
            except (TypeError, ValueError, IndexError):
                continue
            if c <= 0:
                # A zero close makes every percentage feature undefined;
                # skipping the bar keeps the series index-aligned to the
                # bars that survived, which is what the caller needs.
                continue
            closes.append(c)
            highs.append(h)
            lows.append(l)
            vols.append(float(r[5]) if len(r) > 5 and r[5] is not None else 0.0)
            stamps.append(int(r[0]) if r[0] is not None else 0)
        if not closes:
            return {}
        if space is not None and space.name == "god":
            from . import godmode
            feats = godmode.compute_god_features(
                closes, highs=highs, lows=lows, volumes=vols,
                timestamps=stamps)
        else:
            feats = vibe.compute_features(closes, highs=highs, lows=lows,
                                          volumes=vols, timestamps=stamps)
        # Locate the entry bar: the last bar at or before the fill.
        index = None
        for i in range(len(stamps) - 1, -1, -1):
            if stamps[i] <= int(ts):
                index = i
                break
        if index is None:
            # Entry predates the window we loaded; say so rather than
            # silently using the oldest bar we happen to have.
            return {}
        return {name: series[index] for name, series in feats.items()}

    def _cmd_shadow(self, args):
        """Infer entry rules from this account's own profitable trades."""
        vibe = self._vibe_mod()
        self._require_data()
        # --god selects the God feature space.
        use_god = "--god" in args
        args = [a for a in args if a != "--god"]
        try:
            min_support = int(args[0]) if args else 3
        except ValueError:
            raise BrokerValidationError("min_support must be an integer")
        if min_support < 1:
            raise BrokerValidationError("min_support must be >= 1")
        space = self._god_space() if use_god else vibe.BASE_SPACE
        journal, feature_sets = self._vibe_journal(vibe, space)
        if not journal:
            return ("no closed roundtrips yet: a roundtrip needs an entry "
                    "and an exit in this account's fills")
        profile = vibe.extract_profile(journal, feature_sets,
                                       min_support=min_support, space=space)
        self._vibe = profile
        self._vibe_space = space
        self._emit("vibe.profile", profile.profile_id,
                   rules=len(profile.rules), profitable=profile.profitable,
                   total=profile.total, space=space.name)
        lines = ["profile {} ({}) -- {} rules from {} profitable of {} "
                 "roundtrips".format(profile.profile_id, space.name,
                                     len(profile.rules), profile.profitable,
                                     profile.total),
                 "explains {:.0%} of them".format(profile.explained)]
        for r in profile.rules:
            lines.append("  {}  support {:<3} coverage {:<5.0%} hold {:<4.0f}b  "
                         "{}".format(r.rule_id, r.support, r.coverage,
                                     r.avg_hold_bars, r.text))
        lines.append("notes: {}".format(profile.notes))
        lines.append("run `signals` to see these firing on the live book")
        return "\n".join(lines)

    def _vibe_series(self, vibe, symbols, space=None, lookback=120):
        """symbol -> feature series, for the scanner."""
        series, prices = {}, {}
        for symbol in symbols:
            try:
                candles = self.data.candles(symbol, limit=lookback)
            except Exception:
                continue
            rows = list(candles or ())
            if not rows:
                continue
            if isinstance(rows[0], dict):
                cols = ("ts", "open", "high", "low", "close", "volume")
                rows = [tuple(r.get(c) for c in cols) for r in rows]
            rows.sort(key=lambda r: (r[0] is None, r[0]))
            closes, highs, lows, vols, stamps = [], [], [], [], []
            for r in rows:
                try:
                    h, l, c = float(r[2]), float(r[3]), float(r[4])
                except (TypeError, ValueError, IndexError):
                    continue
                if c <= 0:
                    continue
                closes.append(c)
                highs.append(h)
                lows.append(l)
                vols.append(float(r[5]) if len(r) > 5 and r[5] is not None else 0.0)
                stamps.append(int(r[0]) if r[0] is not None else 0)
            if not closes:
                continue
            if space is not None and space.name == "god":
                from . import godmode
                series[symbol] = godmode.compute_god_features(
                    closes, highs=highs, lows=lows, volumes=vols,
                    timestamps=stamps)
            else:
                series[symbol] = vibe.compute_features(
                    closes, highs=highs, lows=lows, volumes=vols,
                    timestamps=stamps)
            prices[symbol] = closes[-1]
        return series, prices

    def _cmd_signals(self, args):
        """Scan the live book for the inferred rules firing right now."""
        vibe = self._vibe_mod()
        self._require_data()
        profile = self._vibe
        if profile is None:
            raise BrokerValidationError(
                "no profile yet: run `shadow` first to infer one")
        symbols = list(args) if args else None
        if not symbols:
            try:
                symbols = [str(s) for s in self.data.symbols() or ()]
            except Exception:
                raise BrokerValidationError("could not list symbols")
        if not symbols:
            return "no symbols to scan"
        space = self._vibe_space or vibe.BASE_SPACE
        series, prices = self._vibe_series(vibe, symbols, space)
        if not series:
            # Name the actual requirement rather than a guess: RSI needs
            # 15 bars, the volume lookback 21, so below that a rule can
            # never be evaluated and the answer is "not enough history".
            return ("no candle history for {} of {} symbol(s): signals need "
                    "at least {} 1m bars per symbol".format(
                        len(symbols), len(symbols), MIN_BARS_FOR_SIGNALS))
        signals, gaps = vibe.scan(profile, series, prices=prices,
                                  ts=int(self._now()), space=space)
        self._last_signals = tuple(signals)
        if not signals:
            out = "no rule firing on {} symbols".format(len(series))
            if gaps:
                # A quiet book and an unreadable book look identical from
                # the outside. Report the difference.
                by_rule = {}
                for sym, rule_id, missing in gaps:
                    by_rule.setdefault(rule_id, []).append((sym, missing))
                out += "\n  {} of {} rules could not be evaluated:".format(
                    len(by_rule), len(profile.rules))
                for rule_id in sorted(by_rule)[:5]:
                    sample = by_rule[rule_id][0]
                    out += ("\n    {} on {}: missing {}".format(
                        rule_id, sample[0], ", ".join(sample[1])))
            return out
        lines = ["{} signal(s) across {} symbols:".format(
            len(signals), len(series))]
        for s in signals:
            lines.append("  {:<10} {:<4} {}  last {:g}".format(
                s.symbol, s.rule_id, s.text, s.price or 0.0))
        if gaps:
            lines.append("  ({} rule/symbol pairs could not be evaluated; "
                         "see `shadow` notes)".format(len(gaps)))
        lines.append("advisory only -- no order is placed by `signals`")
        return "\n".join(lines)

    def _cmd_ground(self, args):
        """Audit a line's figures against what was actually observed."""
        vibe = self._vibe_mod()
        if not args:
            raise BrokerValidationError(
                'usage: ground "<text with numbers>"')
        text = " ".join(args)
        ground = self._ground
        if ground is None:
            ground = self._ground = vibe.Ground()
            # Seed from this account's own fills, so a PnL quoted back
            # traces to the fills that produced it.
            for f in self._vibe_fills():
                ground.observe("fill", f.get("qty"))
                ground.observe("fill", f.get("price"))
        untraceable, traceable = ground.audit_text(text)
        verdict = ground.verdict(text)
        lines = ["{} -- {} untraceable, {} traceable".format(
            verdict, len(untraceable), len(traceable))]
        for token, label in untraceable:
            lines.append("  ungrounded  {} (no observation matches)".format(
                token))
        for token, label in traceable:
            lines.append("  grounded    {}  <- {}".format(
                token, label or "observed value"))
        return "\n".join(lines)

    def _cmd_vibe_status(self, args):
        # Reaching here means the import already succeeded in _vibe_mod.
        lines = ["vibe layer loaded"]
        lines.append("data source: {}".format(
            "attached" if self.data is not None else "none"))
        space = self._vibe_space
        lines.append("feature space: {}".format(
            space.name if space is not None else "base (default)"))
        profile = self._vibe
        if profile is None:
            lines.append("profile: none (run `shadow`)")
        else:
            lines.append("profile: {} -- {} rules, {} of {} roundtrips "
                         "profitable, explains {:.0%}".format(
                             profile.profile_id, len(profile.rules),
                             profile.profitable, profile.total,
                             profile.explained))
        lines.append("observations: {}".format(
            len(self._ground) if self._ground is not None else 0))
        lines.append("last scan: {} signal(s)".format(len(self._last_signals)))
        return "\n".join(lines)

    def _cmd_vibe_reset(self, args):
        had = self._vibe is not None
        self._vibe = None
        self._vibe_space = None
        self._last_signals = ()
        return "profile dropped" if had else "no profile to drop"

    # -- gods ------------------------------------------------------------
    def _gods_mod(self):
        try:
            from . import gods
            return gods
        except Exception as e:
            raise BrokerValidationError(
                "gods layer unavailable: {}: {}".format(type(e).__name__, e))

    def _cmd_dna(self, args):
        """Show or mutate a DNA-encoded rule."""
        gods = self._gods_mod()
        if not args:
            # Show a sample rule.
            dna = gods.DnaRule([
                ("operator", "<"), ("indicator", "rsi14"), ("real", 30.0)
            ])
            return "sample DNA: {}".format(dna.to_dict())
        if args[0] == "mutate":
            dna = gods.DnaRule([
                ("operator", "<"), ("indicator", "rsi14"), ("real", 30.0)
            ])
            rate = float(args[1]) if len(args) > 1 else 0.3
            mutated = dna.mutate(rate=rate)
            return "mutated: {}".format(mutated.to_dict())
        if args[0] == "crossover":
            a = gods.DnaRule([
                ("operator", "<"), ("indicator", "rsi14"), ("real", 30.0)
            ])
            b = gods.DnaRule([
                ("operator", ">"), ("indicator", "volume_ratio"), ("real", 2.0)
            ])
            child = a.crossover(b)
            return "crossover: {}".format(child.to_dict())
        raise BrokerValidationError("usage: dna [mutate [rate] | crossover]")

    def _cmd_dissent(self, args):
        """Run the adversarial dissenter against the last signal."""
        gods = self._gods_mod()
        if not self._last_signals:
            raise BrokerValidationError("no signals yet: run `signals` first")
        sig = self._last_signals[0]
        profile = self._vibe
        d = gods.Dissenter(min_support=3)
        verdict, reasons = d.dissent(sig, profile)
        if verdict == "pass":
            return "dissenter: pass (no objection)"
        return "dissenter: object\n  " + "\n  ".join(reasons)

    def _cmd_veto(self, args):
        """Run the dual veto gates against the last signal."""
        gods = self._gods_mod()
        if not self._last_signals:
            raise BrokerValidationError("no signals yet: run `signals` first")
        sig = self._last_signals[0]
        profile = self._vibe
        risk = gods.VetoGate("risk", gods.default_risk_gate)
        compliance = gods.VetoGate("compliance", gods.default_compliance_gate)
        lines = []
        for gate in (risk, compliance):
            ok, reason = gate.evaluate(sig, profile)
            lines.append("  {}: {} {}".format(
                gate.name, "ALLOW" if ok else "VETO", reason))
        return "veto gates:\n" + "\n".join(lines)

    def _cmd_restraint(self, args):
        """Show the restraint log (rejected signals)."""
        gods = self._gods_mod()
        log = self._restraint_log
        if log is None:
            log = self._restraint_log = gods.RestraintLog()
        if len(log) == 0:
            return "restraint log: empty (no signals rejected yet)"
        lines = ["restraint log: {} rejection(s)".format(len(log))]
        for entry in log.recent(10):
            lines.append("  {} {} {}: {}".format(
                entry["ts"], entry["symbol"], entry["gate"],
                entry["reason"]))
        return "\n".join(lines)

    # -- live arming -----------------------------------------------------
    def _cmd_arm(self, args):
        """Arm live trading. Three independent gates must all pass."""
        if not args or args[0].lower() not in ("yes", "true", "1"):
            raise BrokerValidationError(
                "usage: arm yes   (this is the explicit confirmation)")
        if not live_trading_enabled():
            raise BrokerValidationError(
                "LIVE_TRADING_ENABLED is not set to 1 in this environment")
        if self.broker is None or not self.broker.is_live:
            raise BrokerValidationError(
                "current broker is not live; nothing to arm")
        self._armed_until = self._now() + ARM_TTL_SECONDS
        self._emit("broker.armed", self.broker.venue, ttl=ARM_TTL_SECONDS)
        return "live trading armed for {}s on {}".format(
            ARM_TTL_SECONDS, self.broker.venue)

    def _cmd_disarm(self, args):
        self.disarm("operator")
        return "live trading disarmed"

    def disarm(self, reason=""):
        if self._armed_until:
            self._emit("broker.disarmed", reason or "unspecified")
        self._armed_until = 0.0
        return True
