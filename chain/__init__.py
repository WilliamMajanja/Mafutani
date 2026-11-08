"""Mafutani Chain (MFT) - zero-dependency, SBC-mineable, PQC-signed.

This package implements an independent ledger/coin for Mafutani. It does not
touch the trading app's tables and has no third-party imports.

Consensus highlights (see chain.consensus / chain.ledger):
- Proof-of-Work: SHAKE256(block_header || nonce) meets difficulty target.
  Difficulty adjusts; parameters are SBC-friendly.
- Epoch: 182 days (6 months). Each epoch may include exactly one deterministic
  EPOCH_SEED transaction minting 100,000,000,000 MFT (100B).
- Token expiry & replacement: outputs expire after TOKEN_LIFETIME_EPOCHS
  (creation_epoch + 1). Expired outputs are not spendable. The epoch seed
  replaces expiring supply, so circulation is regenerated without breaking
  consensus (all nodes apply the same deterministic rules).
- Signatures: hash-based post-quantum (WOTS+/Merkle over SHAKE256). See chain.pqc.
"""
from chain.ledger import Chain, ConsensusError
from chain.miner import Miner
from chain.wallet import Wallet

__all__ = ["Chain", "ConsensusError", "Miner", "Wallet"]
