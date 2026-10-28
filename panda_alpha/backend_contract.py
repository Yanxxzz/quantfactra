"""QuantFactra v1 adapter contract: explicit evidence, status and cost units.

The original PandaAI wallet path is unchanged. Native adapters never invent
gift/recharge balances, infer a bill from an account delta, or auto-retry a Run.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re

from .backends import NATIVE_CONTRACT, backend_identity
from .registry import verify_artifact
from .research_budget import guard, load_policy, request


TERMINAL = {"succeeded", "failed", "cancelled"}
STATUSES = TERMINAL | {"pending", "running"}


def number(value, label, *, positive=False):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or (positive and value == 0):
        raise ValueError(f"{label} must be a finite {'positive' if positive else 'nonnegative'} number")
    return float(value)


def reference_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", value):
        raise ValueError("Adapter IDs must be filename-safe; retain provider IDs in the adapter's own mapping")
    return value


def read_binding(binding):
    checked = verify_artifact(binding)
    value = json.loads(Path(checked["path"]).read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("Adapter evidence must contain a JSON object")
    return value, checked


def preflight(client, candidate, window, cycle, groups, key):
    """The configured adapter checks its own language and supplies a bound receipt."""
    proof, binding = read_binding(client.preflight(candidate, window, cycle, groups))
    if (proof.get("schema") != "quantfactra-preflight-v1" or proof.get("passed") is not True
            or proof.get("fingerprint") != key):
        raise ValueError("Adapter preflight must pass for the exact frozen definition")
    fields = proof.get("fields")
    if not isinstance(fields, list) or not fields or any(not isinstance(f, str) or not f.strip() for f in fields):
        raise ValueError("Adapter preflight must name the actual input fields")
    return {"schema": proof["schema"], "passed": True, "fingerprint": key,
            "proof": {"input_fields": fields}, "artifacts": [binding]}


def validate_balance(balance, unit=None):
    if not isinstance(balance, dict) or balance.get("schema") != "quantfactra-balance-v1":
        raise ValueError("Native adapters must provide a quantfactra-balance-v1 observation")
    if not isinstance(balance.get("unit"), str) or not balance["unit"].strip() or (unit and balance["unit"] != unit):
        raise ValueError("Adapter balance cost unit differs from the configured unit")
    mode, available = balance.get("billing_mode"), balance.get("available")
    if mode not in {"free", "prepaid"}:
        raise ValueError("Native billing_mode must be free or prepaid")
    if available is not None:
        number(available, "available balance")
    elif mode != "free":
        raise ValueError("Prepaid adapters require an observed available balance")
    return balance


def check_cost_units(jobs, unit):
    for job in jobs:
        definition = json.loads(job.get("definition") or "{}")
        if definition.get("cost_unit", "credits") != unit:
            raise ValueError("Different cost units cannot share an experiment ledger; do not relabel or reset old usage")


def budget_plan(candidates, compute, balance, *, jobs=None, study_state=None):
    """Reuse the authoritative experiment ledger and cumulative research policy."""
    from .platform import _batch_authorization, _batch_limit_problem, beijing_day
    validate_balance(balance)
    jobs = list(jobs or [])
    check_cost_units(jobs, balance["unit"])
    mode = balance["billing_mode"]
    if compute.get("billing_mode") != mode:
        raise ValueError("Observed billing mode differs from the frozen compute configuration")
    estimate = number(compute.get("planning_credit_estimate"), "planning_credit_estimate", positive=True)
    daily = number(compute.get("daily_credit_limit"), "daily_credit_limit", positive=True)
    verified_cap = (compute.get("server_enforced_cap") is True and compute.get("cost_ceiling_verified_at")
                    and compute.get("cost_ceiling_source") and compute.get("cost_ceiling") is not None)
    reservation = number(compute["cost_ceiling"], "cost_ceiling", positive=True) if verified_cap and mode != "free" else estimate
    basis = "free_receipt" if mode == "free" else "enforced_cap" if verified_cap else "authorized_estimate"
    permissions, problem = {}, None
    try:
        if mode == "prepaid":
            allowance = number(compute.get("recharge_credit_limit", 0), "paid-credit allowance", positive=True)
            permissions["recharge"] = _batch_authorization(compute.get("recharge_batch_authorization"),
                                                            "max_recharge_credits", reservation)
            if allowance < reservation:
                raise ValueError("Explicit paid-credit allowance cannot fund this reservation")
            if not verified_cap:
                if compute.get("allow_estimated_billing") is not True:
                    raise ValueError("Prepaid adapters require an enforceable cap or explicit estimated-billing authority")
                permissions["estimated_billing"] = _batch_authorization(
                    compute.get("estimated_billing_batch_authorization"), "max_total_credits", reservation,
                    estimated_risk=True)
            if len({p["batch_id"] for p in permissions.values()}) != 1:
                raise ValueError("Paid-credit and estimated-billing permissions must bind the same batch")
    except ValueError as error:
        problem = str(error)
    # An unknown cancelled bill still occupies its full reservation.
    charged = lambda row: float(row["actual"] if row.get("actual") is not None else row["reserved"])
    used = sum(charged(row) for row in jobs if row["day"] == beijing_day()
               and not (row["state"] == "CANCELLED" and row.get("actual") == 0))
    if used + reservation > daily + 1e-9:
        problem = problem or "Daily experiment budget exhausted"
    if balance["available"] is not None and reservation > balance["available"]:
        problem = problem or "Available balance cannot fund this reservation"
    if any(row["state"] not in {"SETTLED", "CANCELLED"}
           or (row["state"] == "CANCELLED" and row.get("actual") is None) for row in jobs):
        problem = problem or "Reconcile the prior job before dispatching another"
    policy = load_policy(compute)
    study = guard(policy, None, jobs, study_state, reservation)
    problem = problem or study["blocker"]
    selected, blocked, planned = [], [], list(jobs)
    for candidate in candidates:
        category = candidate.get("experiment_category", "exploration")
        try:
            if category not in {"source_probe", "exploration", "validation"}:
                raise ValueError("Unknown research category")
            meta = request(policy, candidate, category)
            check = guard(policy, meta, planned, study_state, reservation)
            reason = problem or check["blocker"] or _batch_limit_problem(
                planned, permissions, reservation, reservation if mode == "prepaid" else 0, additional_runs=1)
            if reason:
                raise ValueError(reason)
            if (used + (len(selected) + 1) * reservation > daily + 1e-9
                    or (balance["available"] is not None and (len(selected) + 1) * reservation > balance["available"])):
                raise ValueError("No remaining budget slot")
            selected.append({"candidate_id": candidate["candidate_id"], "category": category})
            definition = {"cost_unit": balance["unit"], "billing_authorization": permissions}
            if meta:
                definition["research_budget"] = meta
            planned.append({"fingerprint": "planned:" + str(len(selected)), "state": "RESERVED",
                            "actual": None, "reserved": reservation, "definition": json.dumps(definition)})
        except ValueError as error:
            blocked.append({"candidate_id": candidate["candidate_id"], "reason": str(error)})
    return {"date_beijing": beijing_day(), "backend_contract": NATIVE_CONTRACT, "cost_unit": balance["unit"],
            "billing_mode": mode, "cost_basis": basis, "reservation_credit_estimate": reservation,
            "cost_ceiling": reservation if verified_cap and mode != "free" else None,
            "dispatch_ready": problem is None, "blocker": problem, "selected": selected,
            "research_blocked": blocked, "research_budget": study, "billing_authorization": permissions,
            "funding_authorization": False, "official_test_not_pool_admission": True}


def validate_report(report, job, *, simulation=False):
    definition = json.loads(job["definition"])
    if not isinstance(report, dict) or report.get("schema") != "quantfactra-report-v1":
        raise ValueError("Native adapter results must use quantfactra-report-v1")
    if (report.get("run_id") != job["run_id"] or report.get("fingerprint") != job["fingerprint"]
            or report.get("backend_identity") != definition["backend_identity"] or report.get("status") != "succeeded"):
        raise ValueError("Adapter report differs from the original backend, definition or Run")
    if any(report.get(name) != definition[name] for name in ("window", "cycle", "groups")):
        raise ValueError("Adapter report must bind the frozen evaluation settings")
    if simulation and report.get("synthetic") is not True:
        raise ValueError("A synthetic adapter cannot produce a real-platform report")
    raw = report.get("raw_artifacts")
    if not isinstance(raw, list) or (not report.get("synthetic") and not raw):
        raise ValueError("Real-platform normalized reports must bind their provider originals")
    for binding in raw:
        verify_artifact(binding)
    return report


def resume(key, ledger, client, output_dir):
    from .platform import _batch_limit_problem, beijing_day
    job = ledger.get(key)
    definition = json.loads(job["definition"])
    if not job["run_id"]:
        return {"state": job["state"], "action": "Reconcile the original backend mutation before retrying"}
    if job["state"] == "SETTLED":
        return job
    run_id = reference_id(job["run_id"])
    status = client.status(run_id)
    if status not in STATUSES:
        raise ValueError("Native adapter status must be pending, running, succeeded, failed or cancelled")
    if status not in TERMINAL:
        return {"state": job["state"], "platform_status": status, "run_id": run_id}
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / (run_id + ".json")
    if status == "succeeded":
        report = json.loads(path.read_text(encoding="utf-8")) if path.exists() else client.result(run_id)
        validate_report(report, job, simulation=getattr(client, "simulation", False) is True)
        if not path.exists():
            path.write_text(json.dumps(report, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    binding = client.billing(run_id)
    if binding is None:
        return {"state": "BILLING_PENDING", "run_id": run_id, "platform_status": status,
                "action": "Read the original Run's final billing receipt; no redispatch"}
    bill, checked = read_binding(binding)
    if (bill.get("schema") != "quantfactra-billing-v1" or bill.get("confirmed") is not True
            or bill.get("run_id") != run_id or bill.get("status") != status
            or bill.get("backend_identity") != definition["backend_identity"]
            or bill.get("unit") != definition["cost_unit"]):
        raise ValueError("Final billing receipt must bind this backend, Run, status and cost unit")
    actual = number(bill.get("actual"), "actual charge")
    (output_dir / (run_id + ".billing.json")).write_bytes(Path(checked["path"]).read_bytes())
    mode = definition["billing_mode"]
    receipt = {"schema": "quantfactra-settlement-v1", "run_id": run_id, "status": status,
               "actual": actual, "cost_unit": definition["cost_unit"], "billing_artifact": checked,
               "recharge_delta": actual if mode == "prepaid" else 0,
               "attribution": "explicit adapter receipt; no wallet-delta inference",
               "reservation_overrun": actual > job["reserved"]}
    problem = "A declared free backend returned a nonzero bill" if mode == "free" and actual != 0 else None
    permissions = definition.get("billing_authorization", {})
    problem = problem or _batch_limit_problem(ledger.jobs(), permissions, actual, receipt["recharge_delta"], exclude_key=key)
    others = sum(float(row["actual"] if row["actual"] is not None else row["reserved"])
                 for row in ledger.jobs() if row["day"] == job["day"] and row["fingerprint"] != key
                 and not (row["state"] == "CANCELLED" and row.get("actual") == 0))
    if others + actual > definition["daily_credit_limit"] + 1e-9:
        problem = problem or "Daily experiment budget exceeded by actual settlement"
    if definition["cost_basis"] == "enforced_cap" and actual > job["reserved"] + 1e-9:
        problem = problem or "Enforced reservation ceiling exceeded"
    if problem:
        return {"state": "BILLING_REVIEW", "run_id": run_id, "receipt": receipt,
                "action": problem + "; retain the original reservation"}
    ledger.transition(key, job["state"], "SETTLED", actual=actual, receipt=json.dumps(receipt))
    return ledger.get(key)
