"""Simple mempool for CommCoin node (stdlib only).

Admission policy: structural checks; full consensus (UTXO/signature/expiry) is
performed against chain state when building/validating blocks. The mempool is a
staging area, not authority.
"""
import time

class Mempool(object):
    def __init__(self, max_txs=5000):
        self.max_txs = max_txs
        self._txs = {}   # txid -> {"tx", "seen", "fee_hint"}
        self._order = []

    def add(self, txid, tx, fee_hint=0):
        if txid in self._txs: return {"ok": True, "duplicate": True}
        if len(self._txs) >= self.max_txs:
            # evict oldest
            old = self._order.pop(0); self._txs.pop(old, None)
        self._txs[txid] = {"tx": tx, "seen": time.time(), "fee_hint": fee_hint}
        self._order.append(txid)
        return {"ok": True, "txid": txid}

    def remove(self, txid): self._txs.pop(txid, None); self._order = [t for t in self._order if t!=txid]
    def get(self, txid): return self._txs.get(txid)
    def snapshot(self, limit=100):
        items = [dict(self._txs[t], txid=t) for t in self._order[-limit:]]
        return {"count": len(self._txs), "txs": items}
    def drain_for_block(self, limit=500):
        out = []
        for t in list(self._order)[:limit]:
            v = self._txs.get(t)
            if v: out.append(v["tx"])
        return out
