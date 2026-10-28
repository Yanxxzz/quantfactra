"""Exercise planner, command and mailbox APIs locally, without a model call."""
import json
from pathlib import Path
import sys

from quantfactra.ai import ResearchEngine
from quantfactra.workflow import CommandAgents, MailboxAgents
from my_planner import MyPlanner
from worker import reply


def main():
    root = Path(__file__).resolve().parent
    output = root / "runs" / "ai"
    output.mkdir(parents=True, exist_ok=True)
    planned = ResearchEngine(backend=MyPlanner(), memory={}).propose(count=1)
    packet = {"work_id": "starter-market-propose-v1", "role": "market", "action": "propose",
              "task_id": "STARTER-NO-RESEARCH", "reply_contract": {
                  "envelope": "{work_id: exact work_id, output: action-specific JSON}",
                  "propose": "request with the assigned task_id/owner, or decision=no_candidate and reason"}}
    commands = {role: [sys.executable, str(root / "worker.py")]
                for role in ("lead", "market", "fundamental", "skeptic")}
    command = CommandAgents(commands, output / "command-cache", timeout=30)
    response = command(packet)
    mailbox = MailboxAgents(output / "mailbox")
    waiting = mailbox(packet) is None
    if waiting:
        # Simulate the other process's atomic response, using the same envelope.
        target = output / "mailbox" / "market" / "responses" / (packet["work_id"] + ".json")
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(reply(packet)), encoding="utf-8")
        temporary.replace(target)
    received = mailbox(packet)
    if received != response:
        raise ValueError("Command and mailbox responses differ")
    result = {"mode": "integration interface rehearsal", "planner_candidates": len(planned),
              "candidate_source": planned[0].source, "economic_status": planned[0].economic_status,
              "command": response, "mailbox_wait_observed": waiting, "mailbox": received,
              "provider_usage": "not_metered", "provider_calls": None,
              "scope": "Default templates use local fixed replies; edited providers must report their own actual usage"}
    (output / "summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
