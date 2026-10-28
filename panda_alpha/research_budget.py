"""Hybrid research routing and one cumulative search budget, without network calls."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path

from .diversity import canonical_signature
from .registry import verify_artifact

LIMITS = ("study_id", "max_total_credits", "max_runs", "review_interval_credits",
          "max_initial_runs_per_mechanism", "max_runs_per_mechanism")
TASKS = {"native_screen": "exploration", "source_diagnostic": "source_probe",
         "key_confirmation": "validation", "stateful_strategy": "exploration"}
LOCAL_TASKS = {"syntax_check", "cost_sensitivity", "attribution", "correlation",
               "pool_increment", "stability_replay"}


def load_policy(compute: dict) -> dict | None:
    policy = compute.get("official_research_policy")
    if policy is None and compute.get("official_research_policy_file"):
        path = Path(compute["official_research_policy_file"])
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[1] / path
        policy = json.loads(path.read_text(encoding="utf-8-sig"))
    if policy is None:
        return None  # Unchanged legacy callers and resumed jobs retain their identities.
    if not isinstance(policy, dict) or not isinstance(policy.get("study_id"), str) or not policy["study_id"].strip():
        raise ValueError("Official research needs a persistent study_id")
    for name in LIMITS[1:]:
        value = policy.get(name)
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"Research budget {name} must be positive and finite")
        if "runs" in name and type(value) is not int:
            raise ValueError(f"Research budget {name} must be an integer")
    if policy["review_interval_credits"] > policy["max_total_credits"] or policy["max_initial_runs_per_mechanism"] > policy["max_runs_per_mechanism"]:
        raise ValueError("Research budget review/mechanism limits are inconsistent")
    return policy


def limits(policy: dict) -> dict:
    return {name: policy[name] for name in LIMITS}


def request(policy: dict | None, candidate: dict, category: str) -> dict | None:
    if policy is None:
        return None
    task = candidate.get("official_task")
    if task in LOCAL_TASKS:
        raise ValueError(f"{task} belongs to local review; use key_confirmation only for a stated native information gap")
    if task not in TASKS or TASKS[task] != category:
        raise ValueError("Declare official_task and its matching research category")
    for name in ("research_question", "decision_if_pass", "decision_if_fail", "official_information_gap"):
        if not isinstance(candidate.get(name), str) or not candidate[name].strip():
            raise ValueError(f"Official research requires {name}")
    mechanism = candidate.get("budget_mechanism_id") or candidate.get("mechanism")
    if not isinstance(mechanism, str) or not mechanism.strip():
        raise ValueError("Official research requires a stable economic mechanism identity")
    if task == "key_confirmation":
        evidence = candidate.get("local_review_evidence")
        if not isinstance(evidence, list) or not evidence:
            raise ValueError("Key confirmation needs prior local/official review evidence, not formal admission")
        for artifact in evidence:
            verify_artifact(artifact)
    return {"study_id": policy["study_id"], "mechanism_id": mechanism.strip(),
            "family_id": canonical_signature(candidate.get("formula") or candidate.get("code") or "",
                                             ignore_numeric_parameters=True),
            "task": task, "limits": limits(policy)}


def scoped_jobs(jobs: list[dict], study_id: str) -> list[tuple[dict, dict]]:
    result = []
    for job in jobs:
        definition = json.loads(job["definition"]) if job.get("definition") else {}
        meta = definition.get("research_budget")
        if not isinstance(meta, dict) or meta.get("study_id") != study_id:
            continue
        # A failed/ambiguous/charged cancellation remains an attempt. Only a
        # confirmed zero-charge cancellation before account creation is free.
        if job["state"] == "CANCELLED" and job.get("actual") == 0 and not job.get("factor_id") and not job.get("run_id"):
            continue
        result.append((job, meta))
    return result


def usage(jobs: list[dict], study_id: str) -> dict:
    rows = scoped_jobs(jobs, study_id)
    charges = [float(job["actual"] if job.get("actual") is not None else job["reserved"]) for job, _ in rows]
    if any(not math.isfinite(value) or value < 0 for value in charges):
        raise ValueError("Study charge is not reconciled")
    return {"credits": sum(charges), "runs": len(rows),
            "unsettled": sum(job["state"] != "SETTLED" for job, _ in rows)}


def guard(policy: dict | None, meta: dict | None, jobs: list[dict], state: dict | None,
          reservation: float) -> dict:
    if policy is None:
        return {"enabled": False, "blocker": "Active study controls cannot be disabled" if (state or {}).get("policy") else None}
    state = state or {}
    used = usage(jobs, policy["study_id"])
    pinned = state.get("policy")
    blocker = None
    if pinned and pinned != limits(policy):
        blocker = "Active study budget is pinned; changing study_id or limits does not reset it"
    milestone = math.floor((used["credits"] + 1e-9) / policy["review_interval_credits"]) * policy["review_interval_credits"]
    reviews = state.get("reviews", [])
    review = next((r for r in reversed(reviews) if r["milestone"] == milestone), None)
    if not blocker and used["runs"] >= policy["max_runs"]:
        blocker = "Shared study run limit reached"
    if not blocker and (used["credits"] >= policy["max_total_credits"] - 1e-9 or
                        used["credits"] + reservation > policy["max_total_credits"] + 1e-9):
        blocker = "Shared study credit limit reached"
    if not blocker and milestone and not review:
        blocker = f"Free research review required at {milestone:g} credits"
    if not blocker and review and review["decision"] == "local_only":
        blocker = "Latest research review chose local_only"
    if not blocker and meta:
        peers = [(j, m) for j, m in scoped_jobs(jobs, policy["study_id"])
                 if m["mechanism_id"] == meta["mechanism_id"] or m["family_id"] == meta["family_id"]]
        if len(peers) >= policy["max_runs_per_mechanism"]:
            blocker = "Economic mechanism/family run limit reached"
        elif meta["task"] != "key_confirmation" and sum(m["task"] != "key_confirmation" for _, m in peers) >= policy["max_initial_runs_per_mechanism"]:
            blocker = "Mechanism initial-probe limit reached; review locally before key confirmation"
    return {"enabled": True, "study_id": policy["study_id"], "limits": limits(policy),
            "usage": used, "next_review": milestone + policy["review_interval_credits"],
            "review_due": milestone if milestone and not review else None,
            "blocker": blocker, "funding_authorization": False, "server_enforced_cap": False}


def read_state(connection) -> dict:
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "research_budget_control" not in tables:
        return {}
    row = connection.execute("SELECT policy FROM research_budget_control WHERE singleton=1").fetchone()
    return {"policy": json.loads(row[0]) if row else None,
            "reviews": [json.loads(r[0]) for r in connection.execute("SELECT payload FROM research_budget_reviews ORDER BY rowid")]}


def bind_policy(connection, policy: dict) -> None:
    pinned = read_state(connection).get("policy")
    if pinned and pinned != limits(policy):
        raise ValueError("Active study budget is pinned; no automatic new study or increased cap")
    connection.execute("INSERT OR IGNORE INTO research_budget_control(singleton,policy) VALUES (1,?)",
                       (json.dumps(limits(policy), sort_keys=True),))


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def amendment_snapshot(ledger) -> dict:
    """Bind an amendment to the existing policy, every job and prior reviews."""
    state = read_state(ledger.db)
    if not state.get("policy"):
        raise ValueError("Amendment requires an existing pinned study")
    jobs = sorted(ledger.jobs(), key=lambda job: job["fingerprint"])
    return {"policy": state["policy"], "policy_sha256": _digest(state["policy"]),
            "usage": usage(jobs, state["policy"]["study_id"]),
            "all_job_count": len(jobs), "all_jobs_sha256": _digest(jobs),
            "reviews_sha256": _digest(state["reviews"])}


def amend_policy(ledger, amendment: dict, *, execute: bool = False) -> dict:
    """Explicit same-study cap increase; never funds or resumes a pilot.

    The operator supplies the approval artifact. This is an audit mechanism in
    a trusted local workspace, not an authentication boundary. Preview is safe
    on a read-only connection and creates no tables. Existing job definitions
    retain their old limits; usage continues to accumulate by study_id.
    """
    for name in ("amendment_id", "ledger_path", "reason"):
        if not isinstance(amendment.get(name), str) or not amendment[name].strip():
            raise ValueError(f"Amendment requires {name}")
    if amendment.get("status") not in {"draft", "authorized"}:
        raise ValueError("Amendment status must be draft or authorized")
    if execute and amendment["status"] != "authorized":
        raise ValueError("Applying an amendment requires explicit authorization")
    actual_path = next(row[2] for row in ledger.db.execute("PRAGMA database_list") if row[1] == "main")
    if Path(actual_path).resolve() != Path(amendment["ledger_path"]).resolve():
        raise ValueError("Amendment is bound to a different ledger path")
    request_hash = _digest(amendment)
    ledger.db.execute("BEGIN IMMEDIATE" if execute else "BEGIN")
    try:
        tables = {row[0] for row in ledger.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "research_budget_amendments" in tables:
            prior = ledger.db.execute("SELECT request_hash,payload FROM research_budget_amendments WHERE amendment_id=?",
                                      (amendment["amendment_id"],)).fetchone()
            if prior:
                if prior[0] != request_hash:
                    raise ValueError("Amendment ID already has different content")
                ledger.db.execute("ROLLBACK")
                return dict(json.loads(prior[1]), recorded=True, idempotent=True)
        before = amendment_snapshot(ledger)
        if amendment.get("expected_snapshot") != before:
            raise ValueError("Stale policy, usage or evidence snapshot; review a fresh amendment")
        if any(job["state"] not in {"SETTLED", "CANCELLED"} for job in ledger.jobs()) or before["usage"]["unsettled"]:
            raise ValueError("Reconcile all existing jobs before amending limits")
        if "team_control" in tables:
            control = ledger.db.execute("SELECT mode FROM team_control WHERE singleton=1").fetchone()
            if not control or control[0] not in {"offline", "paused"}:
                raise ValueError("Pause the team before amending limits")
        old, new = before["policy"], amendment.get("new_policy")
        if not isinstance(new, dict) or set(new) != set(LIMITS):
            raise ValueError("Amendment needs the complete new policy")
        load_policy({"official_research_policy": new})
        caps = ("max_total_credits", "max_runs")
        if any(new[key] != old[key] for key in LIMITS if key not in caps):
            raise ValueError("Only cumulative credit/run caps may increase; keep study and review/mechanism rules")
        if any(new[key] < old[key] for key in caps) or all(new[key] == old[key] for key in caps):
            raise ValueError("Amendment must increase at least one cap without reducing another")
        if new["max_runs"] < before["usage"]["runs"] or new["max_total_credits"] < before["usage"]["credits"]:
            raise ValueError("New caps must cover all historical usage")
        approval = verify_artifact(amendment.get("approval", {}))
        payload = {"amendment_id": amendment["amendment_id"], "request_hash": request_hash,
                   "ledger_path": str(Path(actual_path).resolve()), "reason": amendment["reason"],
                   "approval": approval, "before": before, "new_policy": new,
                   "recorded_at": datetime.now(timezone.utc).isoformat(),
                   "funding_authorization": False, "official_calls": 0, "team_state_changed": False}
        if execute:
            # Individual statements keep DDL, policy update and audit append in
            # one transaction; executescript would implicitly commit early.
            ledger.db.execute("CREATE TABLE IF NOT EXISTS research_budget_amendments ("
                              "amendment_id TEXT PRIMARY KEY, request_hash TEXT NOT NULL, payload TEXT NOT NULL)")
            for action in ("UPDATE", "DELETE"):
                ledger.db.execute(f"CREATE TRIGGER IF NOT EXISTS research_budget_amendments_no_{action.lower()} "
                                  f"BEFORE {action} ON research_budget_amendments "
                                  "BEGIN SELECT RAISE(ABORT, 'budget amendments are immutable'); END")
            ledger.db.execute("UPDATE research_budget_control SET policy=? WHERE singleton=1",
                              (json.dumps(new, sort_keys=True),))
            after = amendment_snapshot(ledger)
            if any(after[key] != before[key] for key in ("usage", "all_job_count", "all_jobs_sha256", "reviews_sha256")):
                raise ValueError("Historical usage or evidence changed while amending limits")
            payload["after"] = after
            ledger.db.execute("INSERT INTO research_budget_amendments VALUES (?,?,?)",
                              (amendment["amendment_id"], request_hash, json.dumps(payload, sort_keys=True)))
            ledger.db.execute("COMMIT")
        else:
            ledger.db.execute("ROLLBACK")
        return dict(payload, recorded=execute, idempotent=False)
    except BaseException:
        if ledger.db.in_transaction:
            ledger.db.execute("ROLLBACK")
        raise


def record_review(ledger, policy: dict, evidence: dict, execute: bool = False) -> dict:
    if evidence.get("study_id") != policy["study_id"] or evidence.get("decision") not in {"continue", "redirect", "local_only"}:
        raise ValueError("Review needs the active study and an explicit next decision")
    for name in ("findings", "next_steps"):
        if not isinstance(evidence.get(name), str) or not evidence[name].strip():
            raise ValueError(f"Review needs {name}")
    artifacts = evidence.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ValueError("Review needs existing result artifacts")
    checked = [verify_artifact(a) for a in artifacts]
    ledger.db.execute("BEGIN IMMEDIATE" if execute else "BEGIN")
    try:
        state, jobs = read_state(ledger.db), ledger.jobs()
        if state.get("policy") and state["policy"] != limits(policy):
            raise ValueError("Review cannot rename or increase the active study")
        used = usage(jobs, policy["study_id"])
        milestone = math.floor((used["credits"] + 1e-9) / policy["review_interval_credits"]) * policy["review_interval_credits"]
        if not milestone or used["unsettled"]:
            raise ValueError("Review requires a reached milestone and settled executions")
        payload = {"study_id": policy["study_id"], "milestone": milestone,
                   "usage": used, "covered_fingerprints": [j["fingerprint"] for j, _ in scoped_jobs(jobs, policy["study_id"])],
                   "decision": evidence["decision"], "findings": evidence["findings"],
                   "next_steps": evidence["next_steps"], "artifacts": checked,
                   "official_calls": 0, "new_economic_contexts": 0}
        key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        if execute:
            bind_policy(ledger.db, policy)
            ledger.db.execute("INSERT OR IGNORE INTO research_budget_reviews(review_id,payload) VALUES (?,?)",
                              (key, json.dumps(payload, sort_keys=True)))
            ledger.db.execute("COMMIT")
        else:
            ledger.db.execute("ROLLBACK")
        return dict(payload, recorded=execute)
    except BaseException:
        ledger.db.execute("ROLLBACK")
        raise
