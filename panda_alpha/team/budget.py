"""Delegated scope grants; billing and study counters stay in ExperimentLedger.

This is a trusted local coordinator, not an identity or credential sandbox.
Only the operator installs grants. Research agents cannot authorize themselves.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import math

from .contracts import (INFORMATION, TASKS, canonical, digest, strings, text_fields,
                        timestamp, validate_request, verify_artifacts)


def validate_grant(grant):
    text_fields(grant, ("grant_id", "valid_from", "valid_until"))
    if grant.get("status") != "authorized" or grant.get("allow_dynamic_candidates") is not True:
        raise ValueError("An explicitly authorized dynamic-candidate grant is required")
    if grant.get("environment") not in {"synthetic", "official"}:
        raise ValueError("Grant environment must be synthetic or official")
    if timestamp(grant["valid_from"]) >= timestamp(grant["valid_until"]):
        raise ValueError("Invalid grant validity interval")
    if type(grant.get("max_runs")) is not int or grant["max_runs"] <= 0:
        raise ValueError("Grant max_runs must be a positive integer")
    for key in ("max_total_credits", "max_recharge_credits"):
        value = grant.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError("Grant credits must be finite and nonnegative")
    if not 0 < grant["max_total_credits"] or grant["max_recharge_credits"] > grant["max_total_credits"]:
        raise ValueError("Grant credit limits are inconsistent")
    if grant.get("accept_estimated_charge_risk") is not True:
        raise ValueError("Grant must explicitly accept estimated billing risk; this is not a server cap")
    verify_artifacts([grant.get("approval", {})])
    from ..research_budget import load_policy
    policy = load_policy({"official_research_policy": grant.get("study_policy")})
    if policy is None:
        raise ValueError("Grant must bind an existing cumulative study policy")
    scope = grant.get("scope", {})
    strings(scope.get("official_tasks"), "official_tasks")
    if set(scope["official_tasks"]) - set(TASKS):
        raise ValueError("Only factor tasks are supported by this grant")
    if not isinstance(scope.get("lines"), dict) or not scope["lines"] or set(scope["lines"]) - set(INFORMATION):
        raise ValueError("Grant must explicitly scope its research lines")
    for line in scope["lines"].values():
        for field in ("sources", "primary_fields"):
            strings(line.get(field), field)
    for key in ("cycle", "groups"):
        if type(scope.get(key)) is not int or scope[key] <= 0:
            raise ValueError("Grant must freeze cycle and groups")
    from ..evaluation import check_window
    check_window(scope["window"]["start"], scope["window"]["end"], scope.get("sealed_windows", []))
    return grant


def activate(board, grant):
    """Administrative operation. No account calls, no implicit funding approval."""
    validate_grant(grant)
    with board.transaction():
        existing = board.db.execute("SELECT grant_hash FROM team_grants WHERE grant_id=?", (grant["grant_id"],)).fetchone()
        if existing and existing[0] != digest(grant):
            raise ValueError("Installed grant is immutable; do not silently widen its scope")
        from ..research_budget import bind_policy
        bind_policy(board.db, grant["study_policy"])
        board.db.execute("INSERT OR IGNORE INTO team_grants VALUES(?,?,?)", (grant["grant_id"], digest(grant), canonical(grant)))
        board.db.execute("UPDATE team_control SET mode='pilot',reason='Explicit grant installed',grant_id=? WHERE singleton=1",
                         (grant["grant_id"],))
        board.event("*", "operator", "activate", {"grant_id": grant["grant_id"], "grant_hash": digest(grant)})
    return board.mode()


def active_grant(connection):
    row = connection.execute("SELECT mode,grant_id FROM team_control WHERE singleton=1").fetchone()
    if row[0] != "pilot":
        raise ValueError("Team is offline or paused; stop before formal pilot")
    row = connection.execute("SELECT grant_hash,payload FROM team_grants WHERE grant_id=?", (row[1],)).fetchone()
    if row is None:
        raise ValueError("Missing installed grant")
    grant = json.loads(row[1])
    if digest(grant) != row[0]:
        raise ValueError("Installed grant hash mismatch")
    validate_grant(grant)
    instant = datetime.now(timezone.utc)
    if not timestamp(grant["valid_from"]) <= instant < timestamp(grant["valid_until"]):
        raise ValueError("Grant is not currently valid")
    return grant


def check_scope(grant, task):
    request = task["request"]
    identity = validate_request(request)
    if identity != {"request_hash": task["request_hash"], "fingerprint": task["fingerprint"]}:
        raise ValueError("Frozen task identity changed")
    scope = grant["scope"]
    allowed = scope["lines"].get(task["owner"])
    if allowed is None or any(set(request[field]) - set(allowed[field]) for field in ("sources", "primary_fields")):
        raise ValueError("Request is outside authorized source/field scope")
    if set(request.get("control_fields", [])) - set(allowed.get("control_fields", [])):
        raise ValueError("Control fields are outside grant scope")
    if request["candidate"]["official_task"] not in scope["official_tasks"]:
        raise ValueError("Official task is outside grant scope")
    if any(request[key] != scope[key] for key in ("cycle", "groups")):
        raise ValueError("Cycle/groups are outside grant scope")
    w, outer = request["window"], scope["window"]
    if not outer["start"] <= w["start"] <= w["end"] <= outer["end"]:
        raise ValueError("Window is outside grant scope")
    from ..evaluation import check_window
    check_window(w["start"], w["end"], scope.get("sealed_windows", []), w.get("warmup_start"))
    if w.get("warmup_start", w["start"]) < outer["start"]:
        raise ValueError("Warmup is outside grant scope")


def ticket(grant, task):
    """One exact definition, while every ticket shares the parent's spending cap."""
    beijing = timezone(timedelta(hours=8))
    return {"batch_id": grant["grant_id"], "valid_from": timestamp(grant["valid_from"]).astimezone(beijing).date().isoformat(),
            "valid_until": timestamp(grant["valid_until"]).astimezone(beijing).date().isoformat(),
            "fingerprints": [task["fingerprint"]], "accept_estimated_charge_risk": True,
            **{k: grant[k] for k in ("max_runs", "max_total_credits", "max_recharge_credits")}}


def prepare(board, task, config, *, simulation=False):
    grant = active_grant(board.db)
    if grant["environment"] != ("synthetic" if simulation else "official"):
        raise ValueError("Synthetic grant cannot authorize official execution")
    if task["state"] != "ready":
        raise ValueError("Only reviewed and frozen tasks are dispatchable")
    check_scope(grant, task)
    from ..research_budget import limits, load_policy
    policy = load_policy(config["compute"])
    if policy is None or limits(policy) != limits(grant["study_policy"]):
        raise ValueError("Execution config must use the grant's cumulative study policy")
    if any(config["research"][k] != task["request"][k] for k in ("cycle", "groups")):
        raise ValueError("Execution config differs from frozen cycle/groups")
    from ..evaluation import check_window
    window = task["request"]["window"]
    check_window(window["start"], window["end"], config["research"].get("sealed_windows", []), window.get("warmup_start"))
    configured = deepcopy(config)
    compute = configured["compute"]
    compute["daily_credit_limit"] = min(compute["daily_credit_limit"], grant["max_total_credits"])
    compute["recharge_credit_limit"] = min(compute.get("recharge_credit_limit", 0), grant["max_recharge_credits"])
    compute["estimated_billing_batch_authorization"] = ticket(grant, task)
    compute["recharge_batch_authorization"] = ticket(grant, task)
    compute["team_permit"] = {"task_id": task["task_id"], "request_hash": task["request_hash"],
                              "grant_id": grant["grant_id"], "grant_hash": digest(grant), "environment": grant["environment"]}
    return configured


def guard_reservation(connection, key, candidate, definition, ceiling, balance):
    """Called INSIDE the existing BEGIN IMMEDIATE, before any remote mutation.

Legacy ledgers are unchanged. A team ledger requires its permit even when a
caller tries to invoke the old dispatch function directly.
"""
    exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='team_control'").fetchone()
    permit = definition.get("team_permit")
    if not exists:
        if permit:
            raise ValueError("Team permit requires the authoritative team ledger")
        return
    grant = active_grant(connection)
    if (not isinstance(permit, dict) or permit.get("grant_id") != grant["grant_id"]
            or permit.get("grant_hash") != digest(grant) or permit.get("environment") != grant["environment"]):
        raise ValueError("Exact permit from the installed grant is required")
    row = connection.execute("SELECT * FROM team_tasks WHERE task_id=?", (permit.get("task_id"),)).fetchone()
    if row is None:
        raise ValueError("Permit references an unknown task")
    task = dict(row)
    task["request"] = json.loads(task["request"])
    if task["state"] != "ready" or task["fingerprint"] != key or task["request_hash"] != permit.get("request_hash"):
        raise ValueError("Permit does not match a ready frozen task")
    check_scope(grant, task)
    request = task["request"]
    if candidate != request["candidate"] or any(definition.get(k) != request[k] for k in ("window", "cycle", "groups")):
        raise ValueError("Dispatch differs from the exact reviewed request")
    if candidate.get("code"):
        from .contracts import input_fields
        if set(definition.get("native_preflight", {}).get("proof", {}).get("input_fields", [])) != input_fields(request):
            raise ValueError("Verified native inputs differ from the frozen scope")
    from ..research_budget import limits
    if definition.get("research_budget", {}).get("limits") != limits(grant["study_policy"]):
        raise ValueError("Grant study budget cannot be bypassed")
    # Every official run, including verified-cap/gift-only paths, carries the
    # same batch identity, so the existing batch ledger is the only accountant.
    permissions = definition.setdefault("billing_authorization", {})
    permissions["team"] = ticket(grant, task)
    connection.execute("UPDATE team_tasks SET state='submitted',version=version+1 WHERE task_id=?", (task["task_id"],))


def guard_remote_mutation(connection, key, expected_state):
    """Recheck persisted team authority immediately before create or start.

    Reservation already checked scope and source evidence. Only current grant,
    job and task bindings are checked here; read-only Run recovery never calls it.
    A refusal must precede the ambiguous CREATING/DISPATCHING transition.
    """
    job = connection.execute("SELECT state,definition FROM jobs WHERE fingerprint=?", (key,)).fetchone()
    if job is None or job["state"] != expected_state:
        raise ValueError("Remote mutation requires its unchanged reserved job state")
    permit = json.loads(job["definition"] or "{}").get("team_permit")
    team_exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='team_control'").fetchone()
    if not team_exists:
        if permit is not None:
            raise ValueError("Team permit requires the authoritative team ledger")
        return  # Preserve non-team legacy dispatch behavior.
    grant = active_grant(connection)
    if (not isinstance(permit, dict) or permit.get("grant_id") != grant["grant_id"]
            or permit.get("grant_hash") != digest(grant) or permit.get("environment") != grant["environment"]):
        raise ValueError("Reserved experiment no longer matches the active team grant")
    task = connection.execute("SELECT state,fingerprint,request_hash FROM team_tasks WHERE task_id=?",
                              (permit.get("task_id"),)).fetchone()
    if (task is None or task["state"] != "submitted" or task["fingerprint"] != key
            or task["request_hash"] != permit.get("request_hash")):
        raise ValueError("Reserved experiment no longer matches its submitted team task")
