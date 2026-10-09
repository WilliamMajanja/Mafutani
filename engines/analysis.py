"""Cross-asset analysis engine. Stdlib only, import-safe (no db/server).

Turns raw candles + quotes into actionable intelligence: trend state,
momentum, RSI, volatility, liquidity, a 0-100 setup score, a directional
bias with a human reason, support/resistance levels, and — for commodities —
a research card (macro drivers + leverage notes).

Every function is pure: lists/dicts in, dicts out. No I/O.

COT positioning overlay
-----------------------
`analyze_symbol` accepts an optional `cot` block and renders it beside
the technicals. It is **display only** and deliberately changes nothing:
`score`, `bias`, `reason` and `summary` are computed before the overlay
is attached, and no COT value is read by any of them. That is the whole
point of this stage — it puts a slow, lagged, weekly variable on the same
screen as the tape without letting it into a decision, so it can be
*looked at* before anyone builds a hypothesis about it.

`cot` is passed in rather than fetched, because this module does no I/O.
The caller resolves symbol -> CFTC contract and reads the row; see
`data.cotstore.match_symbol`.
"""

import math

from engines.indicators import rsi, ema_series, atr, sma
from engines.anomaly import classify_regime, tf_consensus, detect_anomalies

# How the COT overlay is allowed to modify the summary line. It may only
# append a positioning note; it may never change the bias word or the
# score. A future change that lets COT move the score is a change of
# hypothesis, and it has to go through the backtester and the
# significance tests rather than arriving here.
COT_INFLUENCES_SCORE = False

# static research cards per commodity (usable intelligence without a feed)
COMMODITY_CARDS = {
    "XAUUSD": {"class": "Precious Metals",
               "drivers": ["Inverse to real yields & USD (DXY)",
                            "Liquidity / safe-haven bid in stress",
                            "Central-bank reserve buying is structural support"]},
    "XAGUSD": {"class": "Precious Metals",
               "drivers": ["Higher beta than gold: ~2x gold move per % of DXY",
                            "Industrial demand (solar, electronics) + monetary bid",
                            "Smaller market → sharper demand shocks"]},
    "XPTUSD": {"class": "Precious Metals",
               "drivers": ["Autocatalyst demand (ICE/EV mix shifts sentiment)",
                            "Supply concentrated in RSA; mine disruptions spike price",
                            "Trades with a structural discount to gold/S&P"]},
    "XPDUSD": {"class": "Precious Metals",
               "drivers": ["Highest autocatalyst exposure of the PGMs",
                            "Very illiquid → wide spreads, violent gaps",
                            "Long-run EV substitution pressure vs palladium use"]},
    "WTI":    {"class": "Energy",
               "drivers": ["OPEC+ supply decisions & spare capacity",
                            "US EIA weekly crude inventory surprises move price",
                            "USD + global manufacturing cycle correlation"]},
    "BRENT":  {"class": "Energy",
               "drivers": ["Global benchmark: geopolitical supply risk (routing)",
                            "Physical market (TAPS, North Sea) sets differential",
                            "Spreads vs WTI signal freight/dislocation"]},
    "NGAS":   {"class": "Energy",
               "drivers": ["Weather-driven demand (LNG exports add structural floor)",
                            "Storage injections/withdrawals: EIA weekly report",
                            "Extreme seasonality → widest ATR in commodities"]},
    "COPPER": {"class": "Industrial",
               "drivers": ["'Dr. Copper': leading indicator of global growth",
                            "Electrification demand (grid, EV, renewables)",
                            "Chile/Peru supply shocks and inventory draws"]},
    "WHEAT":  {"class": "Grains",
               "drivers": ["Black Sea corridor & export-license risk",
                            "USDA WASDE supply/demand surprises",
                            "Weather windows (winterkill, spring dryness)"]},
    "CORN":   {"class": "Grains",
               "drivers": ["US planting/condition ratings vs trend yield",
                            "Ethanol + export book drive tightness",
                            "Acreage-swap dynamics with soybeans"]},
    "COFFEE": {"class": "Softs",
               "drivers": ["Brazil crop weather (arabica) is the price driver",
                            "ICES/Brazilian real swings alter export economics",
                            "Inventory (green bean) draws accelerate rallies"]},
    "COCOA":  {"class": "Softs",
               "drivers": ["West African crop shortfalls are structural",
                            "Grindings data proxy for demand strength",
                            "Historically tight stocks → wild backwardation"]},
    # -- base / industrial metals (LME)
    "ALUM":   {"class": "Industrial Metals",
               "drivers": ["Energy cost share is the fixed-cost anchor (smelting)",
                            "China capacity swaps + alumina tightness drive the tape",
                            "LME warehouses: net inflows signal demand softness"]},
    "NICKEL": {"class": "Industrial Metals",
               "drivers": ["Indonesian supply growth is structural headwind",
                            "EV battery demand upside vs stainless flat demand",
                            "LME inventories and UK/Russia sanctions shocks"]},
    "ZINC":   {"class": "Industrial Metals",
               "drivers": ["Galvanising demand tracks construction/steel cycle",
                            "China smelter margins dictate concentrate tightness",
                            "Low warehouse stocks amplify short squeezes"]},
    "TIN":    {"class": "Industrial Metals",
               "drivers": ["Soldering demand = electronics cycle beta",
                            "Myanmar/Indonesia supply disruptions are frequent",
                            "Illiquid: thin books, violent moves, wide spreads"]},
    "LEAD":   {"class": "Industrial Metals",
               "drivers": ["Battery replacement cycle + winter cranking demand",
                            "Scrap/refined supply spread drives the basis",
                            "China export dynamics dominate the marginal barrel"]},
    # -- refined energy (NYMEX)
    "GASOLINE": {"class": "Energy",
               "drivers": ["Crack spread = direct refining margin signal",
                            "Driving season + inventory builds (EIA weekly)",
                            "Blendstock supply (RBOB spec) can separate from WTI"]},
    "HEATOIL": {"class": "Energy",
               "drivers": ["Heating/cold-weather demand + diesel link",
                            "ULSD crack spread as distillate tightness barometer",
                            "Middle-distillate inventory surveys move the tape"]},
    # -- grains & oilseeds (CBOT)
    "SOYBEANS": {"class": "Grains",
               "drivers": ["South America weather windows (Brazil, Argentina)",
                            "USDA WASDE: yield, ending stocks surprises",
                            "Acreage competition with corn in spring planting"]},
    "SOYMEAL": {"class": "Grains",
               "drivers": ["Crusher margins convert bean crush into meal supply",
                            "Livestock feed demand + protein import appetite",
                            "Beans minus oil = meal residual: residuals move sharply"]},
    "SOYOIL":  {"class": "Grains",
               "drivers": ["Biodiesel (RVO mandates) adds structural demand",
                            "Vegetable-oil complex: palm/sunflower substitution",
                            "Crush economics flip oil vs meal allocation"]},
    "OATS":   {"class": "Grains",
               "drivers": ["Thin market: spec flow can dominate fundamentals",
                            "Canada/Prairie weather + cereal demand",
                            "Spring vs winter acreage data surprises"]},
    "RRICE":  {"class": "Grains",
               "drivers": ["Global rice stockpile reports (USDA)",
                            "Asian export policy swings (India, Thailand)",
                            "Slowly traded: gaps on govt program announcements"]},
    # -- softs (ICE)
    "SUGAR":  {"class": "Softs",
               "drivers": ["Brazil Centre-South crush + ethanol mix switch",
                            "Monsoon/El Nino damage in India/Thailand",
                            "Global deficit/surplus swing narrows carry"]},
    "COTTON": {"class": "Softs",
               "drivers": ["US crop condition ratings + wind/harvest losses",
                            "China reserve buying / import quota policy",
                            "USDA export sales pace vs mill demand"]},
    "OJ":     {"class": "Softs",
               "drivers": ["Florida citrus greening cuts supply structurally",
                            "Brazil crop + freeze events set the floor",
                            "Illiquid winter months: sharp gap risk"]},
    # -- livestock (CME)
    "LCATTLE": {"class": "Livestock",
               "drivers": ["Cattle-on-feed report (placements/on-feed)",
                            "Feed-corn/soymeal cost swings flip margins",
                            "Pack demand + slaughter pace weekly data"]},
    "LHOGS":  {"class": "Livestock",
               "drivers": ["Hog/corn ratio sets producer economics",
                            "Holiday + export (China) demand spikes",
                            "Herd expansion/contraction cycles are long"]},
    "FCATTLE": {"class": "Livestock",
               "drivers": ["Feeder supply tightness (cow-herd drawdown)",
                            "Corn price moves drive feedlot margins",
                            "Cash feeder auctions set the contract basis"]},

}

# spread thresholds in bps beyond which liquidity is penalised per asset
SPREAD_CAPS = {"fx": 2.0, "crypto": 10.0, "equity": 5.0, "commodity": 30.0}

BAR_MINUTES = 1
BARS_PER_DAY = 24 * 60 // BAR_MINUTES


def _f(x, default=0.0):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    if math.isnan(v) or math.isinf(v):
        return default
    return v


def analyze_symbol(symbol, closes, quote=None, meta=None, candles=None,
                   cot=None):
    """Return full analysis dict. Never raises; missing data degrades.

    Args:
      symbol:  str
      closes:  ascending list of close prices (floats)
      quote:   {bid, ask, last, ...} or None
      meta:    symbol metadata {asset_type, exchange, currency, sector,
               tick_size, lot_size, name} or None
      candles: optional ascending candle dicts (for swing levels, ATR)
      cot:     optional COT positioning block (see `_cot_overlay`). Rendered
               only; it cannot change score, bias, reason or trend.
    """
    out = {"symbol": str(symbol or ""), "ok": True}
    meta = meta or {}
    out["asset_type"] = meta.get("asset_type", "equity")
    out["exchange"] = meta.get("exchange", "")
    out["currency"] = meta.get("currency", "USD")
    out["sector"] = meta.get("sector", "")
    out["name"] = meta.get("name", "")

    vals = [_f(c) for c in (closes or []) if c is not None]
    vals = [v for v in vals if v > 0]
    n = len(vals)

    # -- levels from candles (true swings) else closes
    support = resistance = None
    if candles:
        hs = [_f(c["high"]) if isinstance(c, dict) else 0.0 for c in candles]
        ls = [_f(c["low"]) if isinstance(c, dict) else 0.0 for c in candles]
        hs = [h for h in hs if h > 0]
        ls = [l for l in ls if l > 0]
        if hs and ls:
            support = min(ls[-60:])
            resistance = max(hs[-60:])
    elif n:
        support = min(vals)
        resistance = max(vals)

    spread = out.get("spread_bps")
    q = {}
    try:
        q = {k: _f(v) for k, v in (quote or {}).items() if k in ("bid", "ask", "last")}
    except Exception:
        q = {}
    bid, ask = q.get("bid", 0.0), q.get("ask", 0.0)
    last = q.get("last", 0.0) or ask or bid or (vals[-1] if vals else 0.0)
    if bid > 0 and ask > 0:
        mid = (bid + ask) / 2.0
        spread = (ask - bid) / mid * 10000.0 if mid > 0 else 0.0
    out["last"] = round(last, 6)
    out["spread_bps"] = round(spread or 0.0, 2)

    # -- technicals (need >= 22 bars for ema21)
    rsi14 = None
    trend = "flat"
    trend_strength = 0.0
    if n >= 22:
        rsi14 = round(rsi(vals, 14)[-1], 1)
        e9 = ema_series(vals, 9)[-1]
        e21 = ema_series(vals, 21)[-1]
        base = max(abs(e21), 1e-9)
        gap = (e9 - e21) / base * 100.0
        trend_strength = abs(gap)
        if gap > 0.05:
            trend = "up"
        elif gap < -0.05:
            trend = "down"
    out["rsi14"] = rsi14
    out["trend"] = trend
    out["trend_strength"] = round(trend_strength, 3)

    # -- momentum & volatility
    momentum_pct = None
    session_pct = None
    vol_ann_pct = None
    atr_pct = None
    if n >= 2:
        look = min(30, n)
        a = vals[-look]
        b = vals[-1]
        if a > 0:
            momentum_pct = (b - a) / a * 100.0
        if vals[0] > 0:
            session_pct = (b - vals[0]) / vals[0] * 100.0
        rets = [vals[i] / vals[i - 1] - 1.0 for i in range(1, n)]
        if rets:
            m = sum(rets) / len(rets)
            var = sum((x - m) ** 2 for x in rets) / len(rets)
            sd = math.sqrt(var)
            vol_ann_pct = sd * math.sqrt(BARS_PER_DAY) * 100.0
        if candles:
            atr_vals = atr(candles, 14)
            if atr_vals and last > 0:
                atr_pct = atr_vals[-1] / last * 100.0
    out["momentum_pct"] = round(momentum_pct, 3) if momentum_pct is not None else None
    out["session_pct"] = round(session_pct, 3) if session_pct is not None else None
    out["vol_ann_pct"] = round(vol_ann_pct, 2) if vol_ann_pct is not None else None
    out["atr_pct"] = round(atr_pct, 3) if atr_pct is not None else None

    # -- relative volume
    rel_volume = None
    vols = []
    if candles:
        vols = [_f(c.get("volume") if isinstance(c, dict) else 0.0) for c in candles]
    if not vols and n:
        vols = [1.0] * n
    pos = [v for v in vols if v > 0]
    if pos:
        mean_v = sum(pos) / len(pos)
        if mean_v > 0:
            rel_volume = round(vols[-1] / mean_v, 2)
    out["rel_volume"] = rel_volume

    # -- composite score + bias
    score = 50.0
    bias = "flat"
    reason = "insufficient data"
    if n >= 22 and momentum_pct is not None:
        if trend == "up" and momentum_pct >= 0:
            score += 12
        elif trend == "down" and momentum_pct <= 0:
            score += 12
        elif trend != "flat":
            score += 6
        mag = min(abs(momentum_pct) / 2.0, 8.0)
        score += mag
        if rsi14 is not None:
            if 45 <= rsi14 <= 65:
                score += 6
            elif rsi14 >= 75 or rsi14 <= 25:
                score -= 10
        if rel_volume is not None:
            if rel_volume > 1.0:
                score += 4
            elif rel_volume < 0.6:
                score -= 4
        cap = SPREAD_CAPS.get(out["asset_type"], 30.0)
        if spread and spread > cap:
            score -= 8
        score = max(0.0, min(100.0, score))

        if trend == "up" and momentum_pct >= 0:
            bias = "long"
            reason = "EMA9>EMA21, momentum positive" + (
                (", RSI not extended" if rsi14 is not None and rsi14 < 65 else "") )
        elif trend == "down" and momentum_pct <= 0:
            bias = "short"
            reason = "EMA9<EMA21, momentum negative" + (
                (", RSI not washed out" if rsi14 is not None and rsi14 > 35 else "") )
        elif trend != "flat":
            reason = "trend {} but momentum {} — wait for confirmation".format(
                trend, "fading" if abs(momentum_pct) < 0.1 else ("up" if momentum_pct > 0 else "down"))
        else:
            reason = "EMAs tangled — range, prefer mean-reversion or stand aside"
        if rsi14 is not None and (rsi14 >= 75 or rsi14 <= 25):
            reason += " · RSI {} (extended)".format(rsi14)
    out["score"] = round(score, 1)
    out["bias"] = bias
    out["reason"] = reason

    # -- commodity card + leverage notes
    card = COMMODITY_CARDS.get(str(symbol or "").upper())
    if card:
        out["research_class"] = card["class"]
        out["drivers"] = card["drivers"]
    tick = _f(meta.get("tick_size", 0.01))
    lot = _f(meta.get("lot_size", 1.0))
    if tick > 0 and lot > 0 and last > 0:
        out["leverage"] = {
            "tick_size": tick,
            "lot_size": lot,
            "notional_per_lot": round(last * lot, 2),
            "tick_value_per_lot": round(tick * lot, 6),
            "contract_note": "1 cash lot ≈ {} units · tick {} ≈ {} per lot".format(
                lot, tick, round(tick * lot, 6)),
        }

    # -- COT positioning overlay (display only; see module docstring)
    #
    # Attached after score/bias/reason are settled so it cannot reach
    # them even by accident. `_cot_overlay` returns a refusal-shaped dict
    # when there is nothing to say, which is what keeps a symbol with no
    # COT match from looking like a symbol with neutral positioning.
    out["cot"] = _cot_overlay(cot)

    # -- human summary line
    out["summary"] = _summary_line(out)

    # -- computational intelligence: regime + multi-timeframe consensus
    reg = classify_regime(vals)
    out["hurst"] = reg["hurst"]
    out["regime"] = reg["regime"]
    out["regime_description"] = reg["description"]
    if n >= 60:
        tfc = tf_consensus(vals)
        out["timeframes"] = tfc["timeframes"]
        out["tf_alignment"] = tfc["alignment"]
        out["tf_verdict"] = tfc["verdict"]
    else:
        out["timeframes"] = []
        out["tf_alignment"] = "insufficient"
        out["tf_verdict"] = "need >= 60 bars for multi-timeframe consensus"

    # -- anomaly detection (price/volume/vol-regime/spread/levels)
    det = detect_anomalies(symbol, vals, candles, quote, meta)
    out["anomaly_score"] = det["anomaly_score"]
    out["anomalies"] = det["anomalies"]

    # flag anomalies in the summary line
    if det["anomalies"]:
        out["summary"] += " · ⚠ {} anomaly flagged".format(len(det["anomalies"]))
    return out


def _cot_overlay(cot):
    """Normalise a COT block for the analysis payload.

    Always returns a dict with `available`. Three states, kept distinct
    on purpose:

      available=False, reason="no COT data stored"
      available=False, reason="no CFTC contract matches <symbol>"
      available=True,  with the measures

    The middle case is the one that matters. Returning
    `{"available": False}` alone would make "we have no positioning data
    for gold" indistinguishable from "gold's positioning is neutral" --
    and those are opposite statements about how much is known.
    """
    out = {
        "available": False,
        "reason": "",
        "contract_code": "",
        "market": "",
        "report": "",
        "report_date": "",
        "age_days": None,
        "window_years": None,
        "band": None,
        "percentile": None,
        "cot_index": None,
        "zscore": None,
        "net": None,
        "weekly_change": None,
        "categories": [],
        "note": "",
    }
    if not cot:
        out["reason"] = "no COT data stored"
        return out
    if cot.get("available") is False:
        out["reason"] = str(cot.get("reason") or "no COT data stored")
        return out

    staleness = cot.get("staleness") or {}
    cons = cot.get("consensus") or {}
    cats = cot.get("categories") or []
    out.update({
        "available": True,
        "contract_code": str(cot.get("contract_code") or ""),
        "market": str(cot.get("market") or ""),
        "report": str(cot.get("report") or ""),
        "report_date": str(cot.get("report_date") or ""),
        "age_days": staleness.get("age_days"),
        "window_years": cot.get("window_years"),
        "band": cons.get("band"),
        "percentile": cons.get("percentile"),
        "cot_index": cons.get("cot_index"),
        "zscore": cons.get("zscore"),
        "net": (cats[0].get("net") if cats else None),
        "weekly_change": (cats[0].get("weekly_change") if cats else None),
        "categories": [
            {"category": c.get("category"), "net": c.get("net"),
             "percentile": c.get("percentile"),
             "cot_index": c.get("cot_index"), "zscore": c.get("zscore"),
             "crowding": c.get("crowding")}
            for c in cats
        ],
    })
    if cons.get("percentile") is None:
        out["note"] = ("stored but too little history to rank positioning")
    return out


def _cot_note(a):
    """One positioning clause for the summary line, or '' if unusable.

    Says what the number is and how old it is. It deliberately does not
    say "bullish" or "bearish": COT measures who is positioned, which is
    not the same as where the price goes next, and the summary line is
    the part an operator reads in a hurry.
    """
    c = a.get("cot") or {}
    if not c.get("available"):
        return ""
    band = c.get("band")
    age = c.get("age_days")
    if band is None:
        return ""
    label = {
        "crowded-long": "specs crowded long",
        "crowded-short": "specs crowded short",
        "extended-long": "specs extended long",
        "extended-short": "specs extended short",
        "neutral": "positioning neutral",
    }.get(band, band)
    age_txt = " · COT {} ({}d old, not a decision input)".format(label, age) \
        if age is not None else " · COT {}".format(label)
    return age_txt


def _summary_line(a):
    bits = []
    bias = a.get("bias", "flat")
    if bias == "long":
        bits.append("LONG")
    elif bias == "short":
        bits.append("SHORT")
    else:
        bits.append("WATCH")
    bits.append("score {}".format(a.get("score")))
    bits.append("trend {}".format(a.get("trend")))
    mp = a.get("momentum_pct")
    if mp is not None:
        bits.append("mom {}{}%".format("+" if mp >= 0 else "", round(mp, 2)))
    r = a.get("rsi14")
    if r is not None:
        bits.append("RSI {}".format(r))
    note = _cot_note(a)
    if note:
        bits.append(note.strip(" ·"))
    return " · ".join(bits)


def aggregate_exchanges(rows):
    """rows: list of per-symbol analysis dicts (must include exchange).
    Returns list of exchange-level analysis sorted by total volume desc."""
    by = {}
    for r in rows:
        code = r.get("exchange") or "?"
        d = by.setdefault(code, {"codes": [code], "symbols": [], "volume": 0.0,
                                 "spreads": [], "scores": []})
        d["symbols"].append(r.get("symbol"))
        d["volume"] += _f(r.get("volume"))
        if r.get("spread_bps") is not None:
            d["spreads"].append(r.get("spread_bps"))
        if r.get("score") is not None:
            d["scores"].append(r.get("score"))
    out = []
    for code, d in by.items():
        avg_spread = sum(d["spreads"]) / len(d["spreads"]) if d["spreads"] else None
        avg_score = sum(d["scores"]) / len(d["scores"]) if d["scores"] else None
        top = None
        if d["scores"] and d["symbols"]:
            paired = sorted(zip(d["symbols"], d["scores"]), key=lambda pair: pair[1], reverse=True)
            top = paired[0][0]
        out.append({"exchange": code, "symbols": sorted(d["symbols"]),
                    "symbol_count": len(d["symbols"]), "total_volume": round(d["volume"], 2),
                    "avg_spread_bps": round(avg_spread, 2) if avg_spread is not None else None,
                    "avg_score": round(avg_score, 1) if avg_score is not None else None,
                    "top_symbol": top})
    out.sort(key=lambda d: d["total_volume"], reverse=True)
    return out