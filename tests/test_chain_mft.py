import json
import os
import tempfile
import unittest

from chain import consensus, pqc
from chain.ledger import Chain, block_hash, block_header_bytes
from chain.wallet import Wallet
from chain.miner import Miner


class TestMFTConsensus(unittest.TestCase):
    def test_epoch_and_reward_constants(self):
        self.assertEqual(consensus.EPOCH_SECONDS, 182 * 24 * 3600)
        self.assertEqual(consensus.EPOCH_REWARD, 100_000_000_000 * consensus.COIN)
        self.assertEqual(consensus.TOKEN_LIFETIME_EPOCHS, 1)
        self.assertTrue(consensus.is_expired(0, 1))
        self.assertFalse(consensus.is_expired(0, 0))

    def test_pqc_sign_verify(self):
        pub, priv = pqc.generate_keypair(height=4)
        msg = b"mafutani-pqc-test"
        sig = pqc.sign_message(priv, msg)
        self.assertTrue(pqc.verify_signature(pub, msg, sig))
        self.assertFalse(pqc.verify_signature(pub, msg + b"x", sig))


class TestMFTChain(unittest.TestCase):
    def test_mine_seed_and_transfer(self):
        with tempfile.TemporaryDirectory() as td:
            c = Chain(os.path.join(td, "c.db"))
            w = Wallet.create(os.path.join(td, "w.json"), height=4)
            w2 = Wallet.create(os.path.join(td, "w2.json"), height=4)
            m = Miner(c, address=w.address)
            cand = m.build_candidate()
            for n in range(500000):
                if consensus.meets_target(block_header_bytes(cand), n, int(cand["bits"])):
                    cand["nonce"] = n
                    cand["hash"] = block_hash(cand)
                    break
            else:
                self.skipTest("no PoW solution in bounded rounds")
            c.add_block(cand)
            b = c.get_block(1)
            txs = json.loads(b["body"])
            seeds = [t for t in txs if t.get("type") == "epoch_seed"]
            self.assertTrue(seeds)
            self.assertEqual(int(seeds[0]["outputs"][0]["v"]), consensus.EPOCH_REWARD)
            self.assertEqual(c.balance(w.address), consensus.EPOCH_REWARD)
            tx = w.build_transfer(c, w2.address, 10)
            cand2 = m.build_candidate(extra_txs=[tx])
            for n in range(1000000):
                if consensus.meets_target(block_header_bytes(cand2), n, int(cand2["bits"])):
                    cand2["nonce"] = n
                    cand2["hash"] = block_hash(cand2)
                    break
            else:
                self.skipTest("no PoW solution for transfer block")
            c.add_block(cand2)
            self.assertEqual(c.balance(w2.address), 10 * consensus.COIN)
            c.close()


if __name__ == "__main__":
    unittest.main()
