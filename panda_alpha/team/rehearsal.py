"""Three scripted offline rounds. This is lifecycle evidence, not agent research."""
from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import socket
import subprocess
from unittest.mock import patch

from ..registry import ResearchRegistry
from .budget import activate
from .contracts import INFORMATION, canonical
from .coordinator import Coordinator
from .demo import SyntheticClient, artifact, blocked, synthetic_assessment, synthetic_config, synthetic_grant, synthetic_request
from .host import ExistingExecutor, Host


class PersistentSyntheticClient(SyntheticClient):
    def __init__(self, path):
        super().__init__()
        self.path = Path(path)
        if self.path.exists():
            self.__dict__.update(json.loads(self.path.read_text(encoding="utf-8")))

    def save(self):
        self.path.write_text(canonical({k: getattr(self, k) for k in ("creates", "starts", "remaining")}), encoding="utf-8")

    def create(self, *args):
        value = super().create(*args)
        self.save()
        return value

    def start(self, factor_id):
        value = super().start(factor_id)
        self.save()
        return value


class ScriptedRoles:
    """Clearly labelled fixture backend; production supplies real role responses."""

    def __init__(self, proof, repair):
        self.proof, self.repair = proof, repair

    def __call__(self, p):
        action = p["action"]
        if action == "plan_round":
            out = {"decision": "research", "reason": "Bounded scripted lifecycle round; no economic hypothesis acceptance",
                   "assignments": [{"owner": r, "brief": f"Synthetic lifecycle round {p['next_round']}; {family}"}
                                   for r, family in INFORMATION.items()]}
            if p["next_round"] == 2:
                task = next(t for t in p["role_packet"]["tasks"] if t["owner"] == "market")
                prior = [e for e in task["events"] if e["kind"] == "conclusion"][-1]
                out["reopen"] = [{"task_id": task["task_id"], "version": task["version"], "request": {
                    "operation_id": p["work_id"] + ":repair", "request_hash": task["request_hash"],
                    "prior_decision_event_id": prior["event_id"],
                    "target_event_id": task["review_targets"]["result"]["event_id"],
                    "claim_delta": "Local receipt now archived; native clock/support stay unverified",
                    "artifacts": [self.repair]}}]
        elif action == "propose":
            request = synthetic_request(p["role"], self.proof)
            request["task_id"] = request["candidate"]["candidate_id"] = p["task_id"]
            if p["round"] == 2:
                request["candidate"]["direction"] = 0  # Predeclared fixture, not return-driven reversal.
            elif p["round"] == 3:
                fields = request["primary_fields"]
                request["candidate"]["formula"] = f"RANK({fields[0]} * {fields[1]})"
            out = {"request": request}
        elif action == "freeze":
            cross = [e for e in p["task"]["events"] if e["kind"] == "cross_review"]
            dispositions = [{"event_id": e["event_id"], "decision": "accept",
                             "reason": "Retain source limitations; third round has no additional information"} for e in cross]
            out = ({"decision": "park", "reason": "Cross-line control shows no new information; stop before execution",
                    "cross_line_decisions": dispositions} if p["round"] == 3 else
                   {"priority": 50, "allocation_reason": "Exercise synthetic lifecycle only",
                    "diversity_check": "Different declared information families", "cross_line_decisions": dispositions})
        elif action == "conclude":
            review = p["task"]["review_targets"]["result_review"]["payload"]
            out = {"decision": review["decision"], "reason": "Preserve scoped fixture result and all unresolved native qualifications"}
        else:
            out = {"claim": "Scripted protocol fixture; no native or economic conclusion",
                   "test": "Preserve identity, qualifications and independent role handoff", "artifacts": [], "decision": "test"}
            if action == "review":
                out["decision"] = "approve"
            if action == "cross_review":
                out["decision"] = "recommend_park" if p["round"] == 3 else "retain_control"
                out["claim"] = "Shared size exposure needs explicit control; another information family is not automatic diversification"
            if action == "result_review":
                result = p["task"]["review_targets"]["result"]["payload"]
                out.update(artifacts=list(result["artifacts"]), evidence_contract=result["evidence_contract"],
                           decision="confirm", assessment=synthetic_assessment())
                reopened = p["task"]["review_targets"]["evidence_review_requested"]
                if reopened:
                    out["artifacts"] += reopened["payload"]["artifacts"]
                    out["decision"] = "source_pending"
                    out["assessment"]["resolved"] = ["local_receipt_missing"]
                    state = p["task"]["qualifications"]
                    old = next(r for r in state["items"] if r["text"] == "local_receipt_missing")
                    out["qualification_changes"] = {"base_head": state["head"], "replacements": [{
                        "supersedes": [{"id": old["id"], "revision": old["revision"]}],
                        "items": [{"id": old["id"], "text": old["text"], "status": "resolved"}],
                        "reason": "Fixture receipt was archived; native qualifications stay pending", "evidence": [self.repair]}]}
                else:
                    out["assessment"]["pending"].append("local_receipt_missing")
        return {"work_id": p["work_id"], "output": out}


class InterruptedRehearsal(RuntimeError):
    pass


def run_rehearsal(output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    proof, repair = output / "fixture.txt", output / "receipt-repair.txt"
    proof.write_text("Synthetic lifecycle input; no account or economic evidence.\n", encoding="utf-8")
    repair.write_text("Additional local archive receipt. Native source remains pending.\n", encoding="utf-8")
    config = synthetic_config()
    c = Coordinator(output / "team.sqlite3")
    registry = ResearchRegistry(output / "registry.sqlite3")
    activate(c.board, synthetic_grant(artifact(proof), config))
    spec = {"objective": "Three offline lifecycle rounds; distinguish pipeline from research evidence", "max_rounds": 3,
            "max_actions": 100, "deadline": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}
    Host(c, registry, "offline", spec)
    roles = ScriptedRoles(artifact(proof), artifact(repair))
    interruptions = []

    def restart():
        nonlocal c, registry
        c.close()
        registry.close()
        c = Coordinator(output / "team.sqlite3")
        registry = ResearchRegistry(output / "registry.sqlite3")

    def run():
        client = PersistentSyntheticClient(output / "synthetic-client.json")
        return Host(c, registry, "offline").run(roles, ExistingExecutor(c, registry, client, config, output / "results"))

    try:
        with ExitStack() as guards:
            guards.enter_context(patch.object(socket.socket, "connect", blocked))
            guards.enter_context(patch.object(socket.socket, "connect_ex", blocked))
            guards.enter_context(patch.object(subprocess, "Popen", blocked))
            original = Host._apply

            def crash_after_answer(host, packet, response):
                if packet["action"] == "response":
                    raise InterruptedRehearsal("role answer persisted before board application")
                return original(host, packet, response)

            try:
                with patch.object(Host, "_apply", crash_after_answer):
                    run()
            except InterruptedRehearsal as exc:
                interruptions.append(str(exc))
            restart()
            from . import conclusions
            try:
                with patch.object(conclusions, "_commit_board", side_effect=InterruptedRehearsal("registry committed before board close")):
                    run()
            except InterruptedRehearsal as exc:
                interruptions.append(str(exc))
            restart()
            result = run()
            registry.verify_integrity()
            state = Host(c, registry, "offline").state()
            tasks = c.board.tasks()
            events = [e for t in tasks for e in c.board.events(t["task_id"])]
            client = PersistentSyntheticClient(output / "synthetic-client.json")
            memory = registry.reconciled_memory()
            assert result["reason"] == "round_limit" and state["round"] == 3
            assert len(interruptions) == 2 and client.creates == client.starts == registry.new_tested == 4
            assert len(tasks) == 6 and sum(e["kind"] == "evidence_review_requested" for e in events) == 1
            assert all(t["state"] == "parked" for t in tasks if ".r3." in t["task_id"])
            assert len(memory["registry_constraints"]["source_pending"]) == 4
            repaired = next(r for r in memory["registry_constraints"]["source_pending"] if r["task_id"] == "offline.r1.market")
            assert "local_receipt_missing" not in repaired["pending"] and "native_input_and_clock" in repaired["pending"]
            c.board.pause("Three offline lifecycle rounds complete; real source gates unchanged")
            report = {"status": "OFFLINE_LIFECYCLE_VERIFIED", "agent_backend": "scripted fixtures, not real research agents",
                      "rounds": state["round"], "actions": state["actions"], "interruptions_recovered": interruptions,
                      "same_identity_reopens": 1, "synthetic_creates": client.creates, "synthetic_starts": client.starts,
                      "registered_definitions": registry.new_tested, "tasks": len(tasks),
                      "cross_line_reviews": sum(e["kind"] == "cross_review" for e in events),
                      "cross_line_stops": 2, "source_pending": len(memory["registry_constraints"]["source_pending"]),
                      "official_runs": 0, "official_credits": 0, "network_calls": 0, "paid_llm_calls": 0,
                      "native_source_verified": False, "pool_admission": False, "plan": c.plan()}
            (output / "report.json").write_text(canonical(report), encoding="utf-8")
            return report
    finally:
        c.close()
        registry.close()
