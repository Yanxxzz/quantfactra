"""Explicit provider bridge for CommandAgents, or one existing packet file."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .providers import JSONProvider, ModelAgents


def main(argv=None):
    if callable(getattr(sys.stdout, "reconfigure", None)):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument("--packet", type=Path, help="Otherwise read the original packet on stdin")
    inputs.add_argument("--case", type=Path, help="Read-only discussion of a completed public case; sends aggregate context")
    parser.add_argument("--check", action="store_true", help="Read configuration only; no network or receipt writes")
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        root = args.config.resolve().parent
        provider = JSONProvider(config, root / config.get("receipts_dir", "runs/model-receipts"))
        if args.check:
            value = provider.readiness()
        else:
            if args.case:
                from .public_case import review_packet
                packet = review_packet(args.case)
            else:
                packet = json.loads(args.packet.read_text(encoding="utf-8") if args.packet else sys.stdin.read())
            value = ModelAgents(provider)(packet)
    except (ValueError, RuntimeError, OSError) as error:
        parser.error(str(error))
    print(json.dumps(value, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
