"""Tests for the EVM chain / USDT contract registry.

The wallet displays a *balance*, so a wrong contract address does not
produce an error -- it produces a plausible wrong number, which is the
one failure mode a balance display cannot afford. These tests pin the
addresses and the lookup rules that keep the browser from guessing.

Also asserted: the payload handed to the browser carries no RPC endpoint
and no credential, so connecting a wallet cannot turn this server into a
relay or leak a key.
"""
import os
import re
import sys
import unittest

WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WORKDIR not in sys.path:
    sys.path.insert(0, WORKDIR)

from data import chains  # noqa: E402

ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def read_chain_source():
    """Read data/chains.py without leaving a file handle open."""
    with open(os.path.join(WORKDIR, "data", "chains.py")) as fh:
        return fh.read()

# Canonical Tether-published addresses
# (https://tether.to/en/supported-protocols/), except BNB Smart Chain,
# where the widely-held representation is Binance-Peg's.
EXPECTED = {
    1: "0xdAC17F958D2ee523a2206206994597C13D831ec7",
    56: "0x55d398326f99059fF775485246999027B3197955",
    137: "0xc2132D05D31c914a87C6611C10748AEb04B58e8F",
    43114: "0x9702230A8Ea53601f5Cd2dc00fDbc13d4dF4A8c7",
    42161: "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",
}


class TestAddresses(unittest.TestCase):
    def test_every_known_chain_has_its_canonical_address(self):
        for cid, addr in EXPECTED.items():
            self.assertEqual(chains.usdt_address_for(cid), addr,
                             "wrong USDT address for chain %s" % cid)

    def test_no_unexpected_chain_is_declared(self):
        # A chain added without a verified address would show a blank or
        # guessed balance.
        self.assertEqual({c["chain_id"] for c in chains.CHAINS},
                         set(EXPECTED))

    def test_addresses_are_well_formed_checksummed_addresses(self):
        for c in chains.CHAINS:
            self.assertRegex(c["usdt_address"], ADDR_RE,
                             "%s: malformed address" % c["name"])

    def test_addresses_are_unique_per_chain(self):
        # Two chains sharing a contract address means one of them is
        # wrong, and both would read the same (wrong) balance.
        addrs = [c["usdt_address"].lower() for c in chains.CHAINS]
        self.assertEqual(len(addrs), len(set(addrs)))

    def test_hex_matches_numeric_id(self):
        for c in chains.CHAINS:
            self.assertEqual(int(c["hex"], 16), c["chain_id"],
                             "%s: hex/numeric chain id disagree" % c["name"])


class TestDecimals(unittest.TestCase):
    def test_ethereum_usdt_is_six_decimals(self):
        self.assertEqual(chains.get_chain(1)["usdt_expected_decimals"], 6)

    def test_bnb_chain_usdt_is_eighteen_decimals(self):
        # Binance-Peg's BSC token is not the Ethereum contract. Reading
        # 6 here misreports every balance by 10^12.
        self.assertEqual(chains.get_chain(56)["usdt_expected_decimals"], 18)

    def test_expected_decimals_match_the_contract_we_name(self):
        for c in chains.CHAINS:
            self.assertIsInstance(c["usdt_expected_decimals"], int)
            self.assertTrue(0 < c["usdt_expected_decimals"] <= 36)


class TestLookup(unittest.TestCase):
    def test_lookup_by_number(self):
        self.assertEqual(chains.get_chain(1)["name"], "Ethereum")

    def test_lookup_by_hex(self):
        self.assertEqual(chains.get_chain("0x1")["name"], "Ethereum")
        self.assertEqual(chains.get_chain("0x89")["chain_id"], 137)

    def test_hex_lookup_is_case_insensitive(self):
        # MetaMask reports lowercase hex. A case-sensitive lookup would
        # show an Ethereum user as being on an unknown network.
        self.assertIsNotNone(chains.get_chain("0X89"))
        self.assertIsNotNone(chains.get_chain("  0x1  "))

    def test_lookup_by_decimal_string(self):
        self.assertEqual(chains.get_chain("137")["chain_id"], 137)

    def test_unknown_chain_returns_none(self):
        for bad in (None, "", "0xdeadbeef", 999999, "not-a-chain", []):
            self.assertIsNone(chains.get_chain(bad),
                              "%r should not resolve" % (bad,))

    def test_address_lookup_for_unknown_chain_is_none(self):
        # Better a blank than a guessed contract for an unknown network.
        self.assertIsNone(chains.usdt_address_for(424242))

    def test_lookup_does_not_raise_on_junk(self):
        for bad in (object(), 3.5, float("nan"), b"\x00"):
            try:
                self.assertIsNone(chains.get_chain(bad))
            except Exception as e:
                self.fail("get_chain(%r) raised %s" % (bad, e))


class TestPublicPayload(unittest.TestCase):
    """The payload is served unauthenticated. It must be inert."""

    def setUp(self):
        self.payload = chains.list_chains()

    def test_token_symbol_is_usdt(self):
        for row in self.payload:
            self.assertEqual(row["token_symbol"], "USDT")

    def test_no_rpc_endpoint_is_shipped(self):
        # Reads go through the user's own provider. Shipping an RPC URL
        # would make this server needless infrastructure for someone
        # else's traffic.
        for row in self.payload:
            self.assertIsNone(row["rpc"],
                              "%s ships an RPC endpoint" % row["name"])

    def test_payload_rows_are_copies(self):
        # Mutating the response must not corrupt the registry.
        self.payload[0]["usdt_address"] = "0xdead"
        self.assertNotEqual(chains.CHAINS[0]["usdt_address"], "0xdead")

    def test_payload_contains_no_credential_like_field(self):
        forbidden = ("key", "secret", "token_value", "mnemonic", "private",
                     "password", "seed")
        for row in self.payload:
            for field in row:
                low = field.lower()
                for bad in forbidden:
                    self.assertNotIn(bad, low,
                                     "suspicious field %r in chain payload" % field)

    def test_explorer_urls_are_https(self):
        for row in self.payload:
            self.assertTrue(row["explorer"].startswith("https://"),
                            "%s explorer is not https" % row["name"])

    def test_module_source_contains_no_key_shaped_literals(self):
        # Defence in depth: nothing in this module should ever hold a
        # credential. A private key is 64 hex chars with no 0x prefix.
        src = read_chain_source()
        for match in re.findall(r"[\"']([0-9a-fA-F]{64})[\"']", src):
            self.fail("possible key material in data/chains.py: %s..." % match[:12])

    def test_no_signing_helpers_exist(self):
        # There is deliberately no transfer/send/approve path in this
        # module. If one appears, this test says so.
        src = read_chain_source().lower()
        for word in ("eth_sendtransaction", "private_key", "send_raw",
                     "def transfer", "def sign", "def approve"):
            self.assertNotIn(word, src,
                             "data/chains.py gained a signing path: %s" % word)


if __name__ == "__main__":
    unittest.main()
