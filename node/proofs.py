"""Merkle proof service for CommCoin (stdlib only)."""
import json
from chain import merkle
from chain.ledger import txid_for

class ProofService(object):
    def __init__(self, chain): self.chain = chain

    def tx_proof(self, block_height, txid):
        b = self.chain.get_block(int(block_height))
        if not b: return None
        txs = json.loads(b.get("body") or "[]")
        txids = [txid_for(t) for t in txs]
        try:
            idx = txids.index(txid)
        except ValueError:
            return None
        tree = merkle.build(txids, domain_leaf="tx", domain_node="txnode")
        proof = merkle.prove(tree, idx)
        proof["block_height"] = int(block_height)
        proof["block_hash"] = b["hash"]
        proof["header_mrkl"] = b["mrkl"]
        proof["valid_against_header"] = (tree["root"] == b["mrkl"] and merkle.verify(proof, domain_node="txnode"))
        return proof

    def utxo_set_commitment(self, limit=50000):
        rows = self.chain.conn.execute(
            "SELECT txid,vout,address,amount,creation_epoch FROM utxos WHERE spent_height IS NULL ORDER BY txid,vout LIMIT ?",
            (int(limit),)).fetchall()
        leaves = []
        for r in rows:
            leaves.append({"txid": r["txid"], "vout": int(r["vout"]), "a": r["address"],
                           "v": str(r["amount"]), "ce": int(r["creation_epoch"])})
        tree = merkle.build(leaves, domain_leaf="utxo", domain_node="utxonode")
        return {"state_root": tree["root"], "utxo_count": len(leaves), "domain": "utxo-set"}

    def block_commitment(self, height):
        b = self.chain.get_block(int(height))
        if not b: return None
        txs = json.loads(b.get("body") or "[]")
        txids = [txid_for(t) for t in txs]
        tx_tree = merkle.build(txids, domain_leaf="tx", domain_node="txnode")
        state = self.utxo_set_commitment() if int(height) == self.chain.height() else {"state_root": None, "utxo_count": None, "note": "state commitment computed at tip"}
        obj = {"height": b["height"], "hash": b["hash"], "prev": b["prev"], "mrkl": b["mrkl"],
               "tx_root": tx_tree["root"], "state_root": state.get("state_root"), "epoch": b["epoch"], "ts": b["ts"]}
        obj["commitment"] = merkle.commitment(obj, domain="block")
        return obj
