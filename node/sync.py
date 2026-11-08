"""Chain sync/export/import helpers for CommCoin PiNet (stdlib only).

Designed for SBC/Raspberry Pi nodes: transfer blocks/headers as JSON over HTTP
or files. Validation uses the chain's consensus rules; never trust remote data.
"""
import json
import time
import urllib.request
import urllib.error

from chain.ledger import block_hash, canonical_tx_bytes, txid_for


def export_blocks(chain, start_height, end_height=None, limit=500):
    tip = chain.height()
    if end_height is None:
        end_height = tip
    start = max(1, int(start_height))
    end = min(int(end_height), tip)
    if limit and (end - start + 1) > int(limit):
        end = start + int(limit) - 1
    blocks = []
    for h in range(start, end + 1):
        b = chain.get_block(h)
        if not b:
            continue
        # include parsed txs for transfer; body is stored as JSON text
        try:
            txs = json.loads(b.get("body") or "[]")
        except Exception:
            txs = []
        blocks.append({
            "v": 2, "h": int(b["height"]), "hash": b["hash"], "prev": b["prev"],
            "ts": int(b["ts"]), "bits": int(b["bits"]), "nonce": int(b["nonce"]),
            "epoch": int(b["epoch"]), "mrkl": b["mrkl"],
            "state_root": b.get("state_root") or "",
            "txs": txs,
        })
    return {"ok": True, "start": start, "end": end, "tip": tip, "count": len(blocks), "blocks": blocks}


def export_headers(chain, start_height=0, limit=2000):
    tip = chain.height()
    start = max(0, int(start_height))
    end = min(tip, start + int(limit) - 1)
    headers = []
    for h in range(start, end + 1):
        b = chain.get_block(h)
        if b:
            headers.append({"h": b["height"], "hash": b["hash"], "prev": b["prev"],
                            "ts": b["ts"], "bits": b["bits"], "epoch": b["epoch"],
                            "mrkl": b["mrkl"], "state_root": b.get("state_root") or ""})
    return {"ok": True, "tip": tip, "headers": headers}


def import_blocks(chain, blocks, max_blocks=200):
    """Validate and append blocks in order. Returns per-block results; stops on first hard failure."""
    results = []
    count = 0
    for blk in (blocks or [])[:int(max_blocks)]:
        h = int(blk.get("h", blk.get("height", -1)))
        # skip if already known
        existing = chain.get_block_by_hash(str(blk.get("hash") or ""))
        if existing:
            results.append({"h": h, "hash": blk.get("hash"), "ok": True, "skipped": "already_known"})
            continue
        # only extend tip for this simple sync model
        if h != chain.height() + 1:
            results.append({"h": h, "ok": False, "error": "can only append at tip (height %s, tip %s); use full sync/import from genesis snapshot if needed" % (h, chain.height())})
            break
        block = {
            "v": int(blk.get("v", 2)), "h": h, "prev": blk.get("prev"), "ts": int(blk.get("ts")),
            "bits": int(blk.get("bits")), "nonce": int(blk.get("nonce")), "epoch": int(blk.get("epoch")),
            "txs": blk.get("txs") or [],
        }
        # compute mrkl/hash fields expected by validate/add
        from chain.ledger import merkle_root
        txids = [txid_for(tx) for tx in block["txs"]]
        block["mrkl"] = merkle_root(txids)
        block["hash"] = block_hash(block)
        if blk.get("hash") and blk["hash"] != block["hash"]:
            results.append({"h": h, "ok": False, "error": "block hash mismatch"})
            break
        try:
            bh = chain.add_block(block)
            results.append({"h": h, "hash": bh, "ok": True})
            count += 1
        except Exception as e:
            results.append({"h": h, "ok": False, "error": str(e)})
            break
    return {"ok": all(r.get("ok") for r in results) if results else True, "imported": count, "results": results, "tip": chain.height()}


def fetch_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "CommCoin-PiNet/1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw.decode("utf-8", "replace"))


def pull_from_peer(chain, peer_base_url, max_blocks=100):
    """Pull blocks from a peer's /api/chain/export endpoint starting after our tip. Stdlib HTTP only."""
    base = str(peer_base_url or "").rstrip("/")
    if not (base.startswith("http://") or base.startswith("https://")):
        return {"ok": False, "error": "peer URL must be http(s)"}
    start = chain.height() + 1
    url = "%s/api/chain/export?start=%s&limit=%s" % (base, start, int(max_blocks))
    data = fetch_json(url)
    blocks = data.get("blocks") or []
    if not blocks:
        return {"ok": True, "imported": 0, "note": "no new blocks", "tip": chain.height()}
    return import_blocks(chain, blocks, max_blocks=max_blocks)
