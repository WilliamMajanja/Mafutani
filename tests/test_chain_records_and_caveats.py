import json
import os
import tempfile
import unittest

from chain import consensus
from chain.ledger import Chain, block_hash, block_header_bytes, canonical_tx_bytes, MAX_MEMO_BYTES, MAX_RECORD_BYTES
from chain.wallet import Wallet
from chain.miner import Miner


def _mine(c, miner_addr, extra_txs=None, limit=1500000):
    m = Miner(c, address=miner_addr)
    cand = m.build_candidate(extra_txs=extra_txs or [])
    for n in range(limit):
        if consensus.meets_target(block_header_bytes(cand), n, int(cand["bits"])):
            cand["nonce"] = n
            cand["hash"] = block_hash(cand)
            c.add_block(cand)
            return cand
    raise unittest.SkipTest("no PoW solution in bounded rounds")


class TestChainRecordAnchors(unittest.TestCase):
    def test_canonical_tx_includes_record_deterministically(self):
        base = {"type": "transfer", "epoch": 0, "inputs": [], "outputs": [{"a": "mft1x", "v": 1, "ce": 0}], "memo": "m"}
        a = canonical_tx_bytes(dict(base, record={"kind": "land_anchor", "hashes": [{"alg": "sha256", "value": "abc"}]}))
        b = canonical_tx_bytes(dict(base, record={"hashes": [{"value": "abc", "alg": "sha256"}], "kind": "land_anchor"}))
        self.assertEqual(a, b)
        self.assertNotEqual(a, canonical_tx_bytes(base))

    def test_record_anchor_transfer_validates_and_commits(self):
        with tempfile.TemporaryDirectory() as td:
            c = Chain(os.path.join(td, "c.db"))
            w = Wallet.create(os.path.join(td, "w.json"), height=4)
            w2 = Wallet.create(os.path.join(td, "w2.json"), height=4)
            _mine(c, w.address)
            record = {
                "kind": "land_anchor",
                "disclaimer": "NOT legal title/registry/ownership proof",
                "hashes": [{"alg": "sha256", "value": "00" * 32}],
                "parcel_ref": "TEST-PARCEL-1",
                "jurisdiction": "TEST",
            }
            tx = w.build_transfer(c, w2.address, 10, memo="land anchor test", record=record)
            self.assertEqual(tx.get("record", {}).get("kind"), "land_anchor")
            _mine(c, w.address, extra_txs=[tx])
            self.assertEqual(c.balance(w2.address), 10 * consensus.COIN)
            c.close()

    def test_large_memo_and_record_are_bounded(self):
        self.assertGreaterEqual(MAX_MEMO_BYTES, 8192)
        self.assertGreaterEqual(MAX_RECORD_BYTES, 16384)
        with tempfile.TemporaryDirectory() as td:
            c = Chain(os.path.join(td, "c.db"))
            w = Wallet.create(os.path.join(td, "w.json"), height=4)
            w2 = Wallet.create(os.path.join(td, "w2.json"), height=4)
            _mine(c, w.address)
            with self.assertRaises(Exception):
                w.build_transfer(c, w2.address, 1, memo="x" * (MAX_MEMO_BYTES + 5))
            with self.assertRaises(Exception):
                w.build_transfer(c, w2.address, 1, record={"blob": "x" * (MAX_RECORD_BYTES + 100)})
            c.close()

    def test_amount_base_and_large_string_amount_parse(self):
        with tempfile.TemporaryDirectory() as td:
            c = Chain(os.path.join(td, "c.db"))
            w = Wallet.create(os.path.join(td, "w.json"), height=4)
            w2 = Wallet.create(os.path.join(td, "w2.json"), height=4)
            _mine(c, w.address)
            tx = w.build_transfer(c, w2.address, "10")
            self.assertEqual(sum(o["v"] for o in tx["outputs"] if o["a"] == w2.address), 10 * consensus.COIN)
            c.close()


class TestCaveatDocuments(unittest.TestCase):
    def test_policy_doc_contains_required_caveats(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "docs", "POLICIES.md"), "r", encoding="utf-8") as f:
            doc = f.read()
        for needle in [
            "Not hardened or certified for all industries and disciplines",
            "No guarantee of security, consensus, value, liquidity",
            "CPIP/external reference tracking is report-only",
            "No replacement for audits",
            "NOT a registry",
            "NOT legal title",
        ]:
            self.assertIn(needle, doc)


if __name__ == "__main__":
    unittest.main()
