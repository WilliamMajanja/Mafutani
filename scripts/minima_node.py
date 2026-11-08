#!/usr/bin/env python3
"""Inspect a configured Minima node. Stdlib only.

This used to be called `minima_seed.py` and it did not seed anything: it
printed `{'ok': True}` from a function that only wrote an audit row, which
read exactly like a successful mint. The name is the first thing that was
wrong with it.

What it does now is only read: chain state, the node wallet's spendable
balance, and whether a destination address is valid. `--dryrun` on a send
resolves the amount and checks it against the wallet without broadcasting.

Credentials come from the environment so they never reach argv or a shell
history entry:
  MINIMA_HOST, MINIMA_RPC_PORT / MINIMA_MDS_PORT, MINIMA_SECURE,
  MINIMA_RPC_PASSWORD, MINIMA_MDS_PASSWORD, MINIMA_CA_FILE
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from minting import MinimaError, MinimaNode, encode_miniaddress, from_raw  # noqa: E402
from minting.minima import decode_miniaddress, to_raw, validate_address  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Inspect a Minima node (read-only by default).")
    ap.add_argument("--address", help="validate a destination address")
    ap.add_argument("--send", metavar="AMOUNT",
                    help="check a send of AMOUNT MINIMA to --address "
                         "(never broadcasts unless --broadcast)")
    ap.add_argument("--broadcast", action="store_true",
                    help="actually send. Requires --send and --address.")
    args = ap.parse_args(argv)

    if args.address and not args.address.lower().startswith("0x"):
        try:
            print("destination 0x%s" % decode_miniaddress(args.address))
        except MinimaError as exc:
            print("address INVALID: %s" % exc)
            return 2
    if args.address:
        validate_address(args.address)
        print("address ok: %s" % args.address)
        if args.address.lower().startswith("0x"):
            print("            %s" % encode_miniaddress(args.address))
    node = MinimaNode.from_env()
    print("node: %s" % getattr(node.transport, "base_url", "?"))
    try:
        chain = node.network_state()
        print("chain: height=%s supply=%s"
              % (chain.get("block"), chain.get("minima")))
        decimals = node.token_decimals()
        print("token decimals: %d  (1 MINIMA = 10**%d base units)"
              % (decimals, decimals))
        bal = node.balance()
        sendable = int(bal.get("sendable") or 0)
        print("wallet: coins=%s sendable=%s MINIMA"
              % (bal.get("coins"), from_raw(sendable, decimals)))
        print("address: %s" % node.default_address())
        if int(bal.get("coins") or 0) == 0 or sendable == 0:
            print("note: this wallet holds nothing spendable, so no transfer "
                  "can be broadcast from it.")
    except MinimaError as exc:
        print("node error: %s" % exc)
        return 1

    if args.send:
        if not args.address:
            print("--send needs --address")
            return 2
        try:
            target, raw, dec = node.preflight(args.address, args.send)
        except MinimaError as exc:
            print("send refused: %s" % exc)
            return 1
        print("send preflight ok: %s MINIMA -> %s (%d base units)"
              % (from_raw(raw, dec), target, raw))
        if args.broadcast:
            if os.environ.get("MAFUTANI_ALLOW_BROADCAST") != "1":
                print("refusing to broadcast: set MAFUTANI_ALLOW_BROADCAST=1")
                return 3
            res = node.send(args.address, args.send)
            print("broadcast txpowid: %s" % res.get("txpowid"))
        else:
            print("(dry run; nothing was broadcast)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
