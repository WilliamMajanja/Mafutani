"""Off-ramp and bridge tests (Minima integration removed; CommCoin/Daraja focus).

Covers Daraja phone normalization, CommCoin/M-Pesa off-ramp config/request logic
with a fake Daraja client, and bridge metadata. Stdlib only, no network.
"""

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from minting.daraja import DarajaClient, DarajaError  # noqa: E402
from minting.bridges import OfframpBridge  # noqa: E402
from minting import mpesa_offramp  # noqa: E402


class TestDarajaNormalize(unittest.TestCase):
    def setUp(self):
        self.c = DarajaClient(consumer_key="k", consumer_secret="s", shortcode="123")

    def test_normalizes_local_formats(self):
        self.assertEqual(self.c.normalize_msisdn("0712345678"), "254712345678")
        self.assertEqual(self.c.normalize_msisdn("+254712345678"), "254712345678")
        self.assertEqual(self.c.normalize_msisdn("254712345678"), "254712345678")
        self.assertEqual(self.c.normalize_msisdn("712345678"), "254712345678")

    def test_rejects_invalid(self):
        for bad in ("", "123", "abcd", "255712345678", "25471234567"):
            with self.assertRaises(DarajaError):
                self.c.normalize_msisdn(bad)


class TestBridges(unittest.TestCase):
    def test_lists_commcoin_and_daraja(self):
        bridges = OfframpBridge().list()
        names = [b.get("name") for b in bridges]
        self.assertIn("mafutani", names)
        self.assertIn("mpesa_daraja", names)
        self.assertNotIn("minima", names)


class _FakeDaraja(object):
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    def normalize_msisdn(self, phone):
        return DarajaClient().normalize_msisdn(phone)

    def status(self):
        return {"configured": True, "b2c_ready": True}

    def b2c_payment(self, phone, amount, **kw):
        self.calls.append((phone, amount, kw))
        return {
            "ok": self.ok,
            "provider": "daraja_b2c",
            "phone": phone,
            "amount": int(amount),
            "conversation_id": "CID123",
            "originator_conversation_id": "OID123",
            "response_code": "0" if self.ok else "500",
            "response_description": "ok" if self.ok else "fail",
            "raw": {},
        }


class TestMpesaOfframp(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        os.environ["MAFUTANI_MFT_KES_RATE"] = "130"
        os.environ["MAFUTANI_MPESA_OFFRAMP_AUTO_SUBMIT"] = ""  # default pending
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        try:
            self.conn.close()
        except Exception:
            pass

    def test_config_disabled_without_rate(self):
        os.environ["MAFUTANI_MFT_KES_RATE"] = "0"
        cfg = mpesa_offramp.config()
        self.assertFalse(cfg["enabled"])
        self.assertFalse(cfg["rate_configured"])

    def test_create_request_records_pending_and_kes(self):
        row = mpesa_offramp.create_request(
            self.conn, 7, "0712345678", "2.5", mft_address="mft1abc",
            daraja_client=_FakeDaraja(),
        )
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["phone"], "254712345678")
        self.assertEqual(row["amount_mft"], "2.50000000")
        self.assertEqual(row["amount_kes"], 325)
        self.assertEqual(row["rate_kes_per_mft"], "130")
        self.assertEqual(row["user_id"], 7)
        # no auto-submit by default
        self.assertEqual(len(_FakeDaraja().calls), 0)

    def test_auto_submit_when_enabled(self):
        os.environ["MAFUTANI_MPESA_OFFRAMP_AUTO_SUBMIT"] = "1"
        fake = _FakeDaraja(ok=True)
        row = mpesa_offramp.create_request(
            self.conn, 7, "0712345678", "1", daraja_client=fake,
        )
        self.assertEqual(row["status"], "submitted")
        self.assertEqual(len(fake.calls), 1)

    def test_submit_and_list(self):
        row = mpesa_offramp.create_request(
            self.conn, 7, "0712345678", "1", daraja_client=_FakeDaraja(),
        )
        row = mpesa_offramp.submit_request(self.conn, row["id"], daraja_client=_FakeDaraja(ok=True))
        self.assertEqual(row["status"], "submitted")
        rows = mpesa_offramp.list_requests(self.conn, user_id=7)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], row["id"])

    def test_rejects_invalid_phone_or_amount(self):
        with self.assertRaises(DarajaError):
            mpesa_offramp.create_request(self.conn, 7, "bad", "1", daraja_client=_FakeDaraja())
        with self.assertRaises(mpesa_offramp.OfframpError):
            mpesa_offramp.create_request(self.conn, 7, "0712345678", "0", daraja_client=_FakeDaraja())


if __name__ == "__main__":
    unittest.main(verbosity=2)
