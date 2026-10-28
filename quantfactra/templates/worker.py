"""One stdin JSON packet -> one stdout envelope; diagnostics belong on stderr.

This local fixture only demonstrates a real Host proposal response of
no_candidate. Implement each actual action in the packet's reply_contract
before using this worker in a research session.
"""
import json
import sys


def reply(packet):
    if packet.get("action") != "propose":
        raise ValueError("Implement this action's reply_contract before connecting a real Host")
    return {"work_id": packet["work_id"], "output": {
        "decision": "no_candidate",
        "reason": "Offline transport fixture has no permitted research evidence; no proposal or execution"
    }}


if __name__ == "__main__":
    try:
        print(json.dumps(reply(json.load(sys.stdin)), ensure_ascii=False, allow_nan=False))
    except (ValueError, KeyError, TypeError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
