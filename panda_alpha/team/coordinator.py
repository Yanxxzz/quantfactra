"""连接任务板和既有执行器，确定性检查冻结定义、来源及授权。

模型提交假说与审查；资金预留和原 Run 恢复走原 ExperimentLedger。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

from ..platform import ExperimentLedger, dispatch, resume
from ..backends import check_backend_config, check_job_backend
from .board import Board
from .budget import prepare
from .contracts import TASKS, canonical


class Coordinator:
    def __init__(self, ledger_path, *, read_only=False):
        self.ledger = ExperimentLedger(ledger_path, read_only=read_only)
        try:
            self.board = Board(self.ledger, initialize=not read_only)
        except BaseException:
            self.ledger.db.close()
            raise

    def close(self):
        self.ledger.db.close()

    def plan(self):
        """Offline opportunity queue. A queue position is never spending authority."""
        tasks = self.board.tasks()
        counts = {role: sum(self.ledger.get(t["fingerprint"]) is not None for t in tasks if t["owner"] == role)
                  for role in ("market", "fundamental")}
        ready = [t for t in tasks if t["state"] == "ready"]
        # First eligible opportunity to each information line, then information
        # value (lead priority); stable task order is the final tie breaker.
        ready.sort(key=lambda t: (counts[t["owner"]] > 0, -t["freeze"]["priority"]))
        role_action = {"proposed": ("skeptic", "challenge"), "challenged": (None, "response"),
                       "answered": ("skeptic", "review"), "reviewed": ("lead", "freeze"),
                       "ready": ("lead", "await_grant_or_dispatch"), "submitted": ("lead", "resume_original_run"),
                       "result_ready": ("skeptic", "result_review"), "result_reviewed": ("lead", "conclude"),
                       "finalizing": ("lead", "recover_conclusion")}
        return {"control": self.board.mode(), "funding_authorization": False,
                "queue": [t["task_id"] for t in ready], "attempts_by_line": counts,
                "unsettled": [{"fingerprint": j["fingerprint"], "state": j["state"], "run_id": j["run_id"]}
                              for j in self.ledger.jobs() if j["state"] not in {"SETTLED", "CANCELLED"}],
                "work": [{"task_id": t["task_id"], "version": t["version"],
                          "role": role_action[t["state"]][0] or t["owner"], "action": role_action[t["state"]][1]}
                         for t in tasks if t["state"] in role_action]}

    def dispatch(self, task_id, config, client, registry, *, execute=False, native_fixture_reader=None, before_remote=None):
        task = self.board.task(task_id)
        existing = self.ledger.get(task["fingerprint"])
        if existing:
            check_backend_config(client, config.get("platform", {}))
            self.adopt_existing(task, existing, client)
            return existing  # Recover even if grant expired or mode was paused.
        if not execute:
            raise ValueError("Explicit execute is required; plan remains offline")
        configured = prepare(self.board, task, config, simulation=getattr(client, "simulation", False) is True)
        request, candidate = task["request"], task["request"]["candidate"]
        self.check_research(task, config, registry)
        from .local_screen import local_first
        local_first(self.board, task, config, registry)
        return dispatch(candidate, request["window"], configured, self.ledger, client,
                        category=TASKS[candidate["official_task"]], native_fixture_reader=native_fixture_reader,
                        before_remote=before_remote,
                        before_dispatch=lambda: registry.record(candidate, request["window"], request["cycle"], request["groups"]))

    def check_research(self, task, config, registry):
        """Use the same historical constraints before local or remote labels."""
        request, candidate = task["request"], task["request"]["candidate"]
        from ..evolution import Candidate, ResearchEngine
        memory = {}
        for name in ("memory", "live_memory"):
            if config["research"].get(name):
                memory.update(json.loads(Path(config["research"][name]).read_text(encoding="utf-8-sig")))
        memory = registry.reconciled_memory(memory)
        engine = ResearchEngine(registry=registry, memory=memory,
                                research_context={k: request[k] for k in ("window", "cycle", "groups")})
        view = Candidate.from_dict(dict(candidate, hypothesis=request["hypothesis"], mechanism=request["mechanism_id"]))
        if not engine.allows_research(view):
            raise ValueError("Existing research memory blocks this definition or retired direction")

    def adopt_existing(self, task, job, client):
        """Reuse old evidence without registering, charging, or claiming fresh OOS."""
        check_job_backend(job, client)
        with self.board.transaction():
            if self.board.task(task["task_id"])["state"] == "ready":
                self.board.event(task["task_id"], "steward", "reuse_existing", {
                    "fingerprint": job["fingerprint"], "run_id": job["run_id"], "state": job["state"],
                    "prior_evidence": True, "new_independent_oos": False})
                self.board.advance(task["task_id"], "submitted")

    def resume(self, task_id, client, output_dir):
        task = self.board.task(task_id)
        existing = self.ledger.get(task["fingerprint"])
        if existing:
            definition = json.loads(existing["definition"] or "{}")
            simulation = definition.get("team_permit", {}).get("environment") == "synthetic"
            if simulation != (getattr(client, "simulation", False) is True):
                raise ValueError("Resume client must match the persisted execution environment")
            self.adopt_existing(task, existing, client)
        result = resume(task["fingerprint"], self.ledger, client, Path(output_dir))
        job = self.ledger.get(task["fingerprint"])
        if job and job["state"] in {"SETTLED", "CANCELLED"}:
            # A successful run needs logs too. Failure to read them is a source
            # limitation, never grounds for repeating or delaying settlement.
            if job["run_id"] and self.board.task(task_id)["state"] == "submitted" and hasattr(client, "logs"):
                path = Path(output_dir) / (str(job["run_id"]) + ".logs.json")
                try:
                    if not path.exists():
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(canonical(client.logs(job["run_id"])), encoding="utf-8")
                except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired):
                    pass
            with self.board.transaction():
                current = self.board.task(task_id)
                if current["state"] == "submitted":
                    artifacts = []
                    for suffix in (".json", ".logs.json"):
                        path = Path(output_dir) / (str(job["run_id"]) + suffix)
                        if path.is_file():
                            artifacts.append({"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
                    payload = {"fingerprint": task["fingerprint"],
                                     "run_id": job["run_id"], "state": job["state"], "actual": job["actual"],
                                     "environment": json.loads(job["definition"] or "{}").get("team_permit", {}).get("environment", "unknown"),
                                     "receipt": json.loads(job["receipt"]) if job["receipt"] else None,
                                     "artifacts": artifacts}
                    definition = json.loads(job["definition"] or "{}")
                    if definition.get("backend_contract") == "quantfactra-v1":
                        payload["backend_identity"] = definition["backend_identity"]
                    manifest = Path(output_dir) / (task["fingerprint"] + ".team-result.json")
                    manifest.parent.mkdir(parents=True, exist_ok=True)
                    manifest.write_text(canonical(payload), encoding="utf-8")
                    binding = {"path": str(manifest.resolve()), "sha256": hashlib.sha256(manifest.read_bytes()).hexdigest()}
                    from .evidence import artifact, inspect_report
                    contract_path = Path(output_dir) / (task["fingerprint"] + ".evidence.json")
                    contract_path.write_text(canonical(inspect_report(task, payload)), encoding="utf-8")
                    contract = artifact(contract_path)
                    self.board.event(task_id, "steward", "result", dict(payload,
                        evidence_contract=contract, artifacts=[binding] + artifacts + [contract]))
                    self.board.advance(task_id, "result_ready")
        return result

    def conclude(self, task_id, version, decision, registry):
        from .conclusions import conclude
        return conclude(self, task_id, version, decision, registry)

    def recover_conclusions(self, registry):
        from .conclusions import recover
        return recover(self, registry)

    def evidence(self, task_id, output, extra_artifacts=()):
        """Reinspect an existing owned Run from cache; never modify its manifest."""
        from .evidence import artifact, inspect_report, read_artifact
        task = self.board.task(task_id)
        result = [e for e in self.board.events(task_id) if e["kind"] == "result"][-1]
        original = read_artifact(result["payload"]["artifacts"][0])
        job = self.ledger.get(task["fingerprint"])
        environment = json.loads(job["definition"] or "{}").get("team_permit", {}).get("environment", "unknown") if job else "unknown"
        value = inspect_report(task, dict(original, environment=environment, artifacts=original["artifacts"] + list(extra_artifacts)))
        path = Path(output)
        encoded = canonical(value)
        if path.exists() and path.read_text(encoding="utf-8") != encoded:
            raise ValueError("Evidence output already contains different facts; use a new path")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(encoded, encoding="utf-8")
        return artifact(path)
