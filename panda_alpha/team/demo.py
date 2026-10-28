"""Synthetic protocol replay, NOT research evidence or a PandaAI backtest."""
from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import socket
import subprocess
from unittest.mock import patch

from ..registry import ResearchRegistry
from .budget import activate
from .contracts import INFORMATION, canonical
from .coordinator import Coordinator


def artifact(path):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def synthetic_request(owner, evidence):
    market = owner == "market"
    mechanism = "synthetic_liquidity_absorption" if market else "synthetic_operating_cash_quality"
    return {"task_id": "SYN-" + owner, "owner": owner, "mechanism_id": mechanism,
            "information_family": INFORMATION[owner],
            "primary_fields": ["close", "volume"] if market else ["operating_cash_flow", "total_assets"],
            "sources": ["synthetic_fixture"], "availability_clock": "Synthetic next session; not PIT evidence",
            "hypothesis": "Synthetic liquidity response" if market else "Synthetic operating cash quality",
            "control": "Matched turnover and size exposure", "falsifier": "Control explains the entire synthetic signal",
            "risk_exposures": ["size", "industry"], "artifacts": [evidence],
            "window": {"start": "2024-01-01", "end": "2024-06-30"}, "cycle": 5, "groups": 10,
            "candidate": {"candidate_id": "SYN-" + owner, "direction": 1,
                          "formula": "RANK(close / volume)" if market else "RANK(operating_cash_flow / total_assets)",
                          "budget_mechanism_id": mechanism,
                          "official_task": "native_screen" if market else "source_diagnostic",
                          "research_question": "Does the synthetic protocol reach independent review?",
                          "decision_if_pass": "Validate a real field contract before considering a pilot",
                          "decision_if_fail": "Repair the protocol offline", "official_information_gap": "Synthetic placeholder only"}}


def synthetic_config():
    return {"research": {"cycle": 5, "groups": 10, "sealed_windows": []}, "compute": {
        "official_research_policy": {"study_id": "synthetic_team_rehearsal", "max_total_credits": 60,
            "max_runs": 20, "review_interval_credits": 20, "max_initial_runs_per_mechanism": 2, "max_runs_per_mechanism": 4},
        "daily_credit_limit": 10, "recharge_credit_limit": 0, "planning_credit_estimate": 2,
        "cost_ceiling": None, "estimate_verified_at": "synthetic", "server_enforced_cap": False,
        "planning_cost_source": "Synthetic fixture only; not platform pricing",
        "allow_estimated_billing": True, "billing_mode": "gift_only", "allocation_mode": "shared_priority",
        "source_probe_fraction": .3, "exploration_fraction": .4, "validation_fraction": .3}}


def synthetic_grant(evidence, config):
    instant = datetime.now(timezone.utc)
    return {"grant_id": "synthetic-demo-only", "environment": "synthetic", "status": "authorized",
            "allow_dynamic_candidates": True, "valid_from": (instant - timedelta(hours=1)).isoformat(),
            "valid_until": (instant + timedelta(hours=1)).isoformat(), "approval": evidence,
            "max_runs": 4, "max_total_credits": 8, "max_recharge_credits": 0, "accept_estimated_charge_risk": True,
            "study_policy": config["compute"]["official_research_policy"], "scope": {
                "official_tasks": ["native_screen", "source_diagnostic", "key_confirmation"],
                "window": {"start": "2024-01-01", "end": "2024-06-30"}, "cycle": 5, "groups": 10,
                "sealed_windows": [], "lines": {owner: {"sources": ["synthetic_fixture"],
                    "primary_fields": synthetic_request(owner, evidence)["primary_fields"]} for owner in INFORMATION}}}


def send(board, task_id, actor, kind, *, decision="test", reply_to=None, artifacts=None):
    task = board.task(task_id)
    message = {"event_id": f"{task_id}-{kind}-{task['version']}", "task_id": task_id,
                          "version": task["version"], "request_hash": task["request_hash"], "actor": actor,
                          "kind": kind, "reply_to": reply_to, "claim": "Synthetic protocol claim",
                          "test": "Synthetic discriminating control", "decision": decision, "artifacts": artifacts or []}
    if kind == "result_review":
        message["assessment"] = synthetic_assessment()
    return board.message(message)


def synthetic_assessment():
    return {"scope": "capability", "source_status": "pending", "economic_status": "not_evaluated",
            "claims": ["Synthetic protocol delivered; no native source or economic claim"],
            "pending": ["native_input_and_clock", "native_eligible_support"], "resolved": [],
            "limitations": ["Synthetic evidence only"], "pool_admission": False}


def freeze_synthetic(board, request, priority=50):
    board.submit(request)
    task_id = request["task_id"]
    send(board, task_id, "skeptic", "challenge")
    challenge = board.events(task_id)[-1]["event_id"]
    send(board, task_id, request["owner"], "response", reply_to=challenge)
    response = board.events(task_id)[-1]["event_id"]
    task = send(board, task_id, "skeptic", "review", decision="approve", reply_to=response)
    return board.freeze(task_id, task["version"], {"request_hash": task["request_hash"], "priority": priority,
                        "allocation_reason": "Synthetic information value", "diversity_check": "Distinct primary information families"})


class SyntheticClient:
    simulation = True

    def __init__(self):
        self.creates = self.starts = 0
        self.remaining = 100.

    def balance(self):
        return {"total": self.remaining, "gift": self.remaining, "recharge": 0.}

    def create(self, *args):
        self.creates += 1
        return f"synthetic-factor-{self.creates}"

    def start(self, factor_id):
        self.starts += 1
        self.remaining -= 2.
        return f"synthetic-run-{self.starts}"

    def status(self, run_id):
        return 2

    def result(self, run_id):
        return {"synthetic": True, "run_id": run_id, "economic_evidence": False}

    def logs(self, run_id):
        return {"synthetic": True, "run_id": run_id, "complete": True, "logs": [], "errors": []}


def blocked(*args, **kwargs):
    raise AssertionError("Offline rehearsal forbids network and subprocess execution")


def run_demo(output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    proof = output / "synthetic-proof.txt"
    proof.write_text("Synthetic protocol fixture. Not user funding approval or economic evidence.\n", encoding="utf-8")
    evidence, config = artifact(proof), synthetic_config()
    c = Coordinator(output / "team.sqlite3")
    registry = ResearchRegistry(output / "synthetic-registry.sqlite3")
    client = SyntheticClient()
    try:
        with ExitStack() as guards:
            guards.enter_context(patch.object(socket.socket, "connect", blocked))
            guards.enter_context(patch.object(socket.socket, "connect_ex", blocked))
            guards.enter_context(patch.object(subprocess, "Popen", blocked))
            for owner in INFORMATION:
                request = synthetic_request(owner, evidence)
                (output / f"{owner}-request.json").write_text(canonical(request), encoding="utf-8")
                freeze_synthetic(c.board, request)
            # The draft board is usable with no funding. Only this injected fake
            # client can use the synthetic grant; a real PandaClient is rejected.
            offline_queue = c.plan()["queue"]
            activate(c.board, synthetic_grant(evidence, config))
            for task_id in offline_queue:
                first = c.dispatch(task_id, config, client, registry, execute=True)
                assert c.dispatch(task_id, config, client, registry, execute=True)["run_id"] == first["run_id"]
                result = c.resume(task_id, client, output / "results")
                assert result["state"] == "SETTLED"
                event = c.board.events(task_id)[-1]
                task = send(c.board, task_id, "skeptic", "result_review", decision="confirm",
                            reply_to=event["event_id"], artifacts=event["payload"]["artifacts"])
                review = c.board.events(task_id)[-1]
                c.conclude(task_id, task["version"], {"operation_id": task_id + "-conclusion",
                    "review_event_id": review["event_id"], "decision": "confirm",
                    "reason": "Synthetic rehearsal completed; no economic claim"}, registry)
            c.board.pause("PILOT_READY: offline implementation rehearsal complete; real pilot requires separate authority")
            report = {"status": "PILOT_READY", "scope": "synthetic workflow rehearsal only",
                      "official_runs": 0, "paid_llm_calls": 0, "network_calls": 0, "economic_evidence": False,
                      "synthetic_creates": client.creates, "synthetic_starts": client.starts,
                      "synthetic_registered_definitions": registry.new_tested,
                      "closed_tasks": sum(t["state"] == "closed" for t in c.board.tasks()),
                      "ledger": str(output / "team.sqlite3"), "plan": c.plan()}
            if report["closed_tasks"] != 2 or client.creates != 2 or client.starts != 2:
                raise AssertionError("Two-line rehearsal did not complete without duplicate execution")
            (output / "report.json").write_text(canonical(report), encoding="utf-8")
            return report
    finally:
        registry.close()
        c.close()
