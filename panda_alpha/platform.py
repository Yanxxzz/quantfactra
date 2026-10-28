"""Budget-aware official experimentation, distinct from portfolio admission.

SQLite reservations are atomic. An ambiguous dispatch is never automatically
retried. Local reservations are not a server-side billing cap; gift-only dispatch
requires evidence of an enforceable cap, or an explicit batch authorization for
the risk of an estimated charge. This distinction is visible in every plan.
"""
from __future__ import annotations

from contextlib import closing
from datetime import date, datetime, timezone, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
from typing import Any

from .backends import bind_backend, check_backend_config, check_job_backend, uses_native_contract

from .research_budget import (load_policy, request as research_request,
                              guard as research_guard, read_state as research_state,
                              bind_policy)


SOFT_RESEARCH_PENDING = frozenset({"full_a_coverage_pending", "official_pool_increment_pending",
                                  "formal_admission_pending", "post_effective_B_pending"})
NATIVE_SOURCE_DIAGNOSTIC_PENDING = frozenset({"native_financial_period_pending",
                                            "native_financial_value_parity_pending",
                                            "full_pit_certification_pending"})


def beijing_day() -> str:
    return datetime.now(timezone(timedelta(hours=8))).date().isoformat()


def _wallet_funding_models(balance: dict) -> set[str]:
    """Identify only models supported by the observed wallet arithmetic.

    Some official wallets expose freeBalance as a duplicate of giftBalance.
    Keep that observation, but never add it to funding twice. A zero free
    balance can support more than one model; both ends of settlement decide.
    """
    values = [balance.get(name, 0.) for name in ("total", "gift", "recharge", "free")]
    if any(type(value) not in (int, float) or not math.isfinite(value) or value < -1e-9
           for value in values):
        return set()
    total, gift, recharge, free = values
    close = lambda left, right: math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-7)
    models = set()
    if close(total, gift + recharge + free):
        models.add("independent_gift_recharge_free")
    if close(total, gift + recharge):
        if "free" in balance and close(free, gift):
            models.add("gift_recharge_free_alias")
        if "free" not in balance or close(free, 0.):
            models.add("gift_recharge")
    return models


def fingerprint(candidate: dict, window: dict, cycle: int, groups: int) -> str:
    definition = {k: candidate.get(k) for k in ("code", "formula", "direction")}
    definition.update(window=window, cycle=cycle, groups=groups)
    # This prospective contract separates the worker's price-loading window
    # from the fixed economic output. Keep existing experiment identities
    # stable; the new purpose binds both windows and its exact source masks.
    if candidate.get("native_preflight_purpose") == "source_qualified_projection_research":
        if candidate.get("formula") or not isinstance(candidate.get("code"), str) or not candidate["code"].strip():
            raise ValueError("Native projection requires an exact Python code definition")
        contract = candidate.get("native_evaluation_contract")
        if not isinstance(contract, dict) or contract.get("input_window") != window:
            raise ValueError("Native projection must bind the actual creation window")
        definition["native_preflight_purpose"] = candidate["native_preflight_purpose"]
        definition["native_evaluation_contract"] = contract
    if candidate.get("native_preflight_purpose") == "financial_source_diagnostic_research":
        if candidate.get("formula") or not isinstance(candidate.get("code"), str) or not candidate["code"].strip():
            raise ValueError("Native financial diagnostic requires an exact Python code definition")
        contract = candidate.get("native_diagnostic_contract")
        if not isinstance(contract, dict):
            raise ValueError("Native financial diagnostic requires its nominal source contract")
        definition["native_preflight_purpose"] = candidate["native_preflight_purpose"]
        definition["native_diagnostic_contract"] = contract
    if candidate.get("native_preflight_purpose") == "conditional_domain_research":
        contract = candidate.get("native_conditional_contract")
        if (candidate.get("formula") or not candidate.get("code") or not isinstance(contract, dict)
                or contract.get("window") != window or contract.get("cycle") != cycle
                or contract.get("groups") != groups):
            raise ValueError("Conditional research must bind exact code, domain and dispatch parameters")
        definition["native_preflight_purpose"] = candidate["native_preflight_purpose"]
        definition["native_conditional_contract"] = contract
    return hashlib.sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest()


def _frozen_code_path(candidate: dict) -> Path | None:
    if candidate.get("formula") or not candidate.get("code_path"):
        return None
    path = Path(candidate["code_path"]).resolve()
    if not path.is_file() or path.read_bytes().decode("utf-8") != candidate.get("code"):
        raise ValueError("Candidate file differs from the frozen code definition")
    return path


class PandaClient:
    def __init__(self, config: dict, *, before_call=None):
        self.config = config
        self.before_call = before_call

    def _invoke(self, command: list[str], timeout: int = 60) -> dict:
        if self.before_call is not None:
            self.before_call()
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=timeout, env=env)
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            # Do not expose arbitrary CLI stderr, which might include account data.
            raise RuntimeError(f"PandaAI returned invalid JSON (exit {result.returncode})") from None
        if not data.get("success", True):
            raise RuntimeError("PandaAI request failed; inspect the host CLI separately")
        return data

    def cli(self, *args: str, timeout: int = 60) -> dict:
        config = self.config
        if config.get("python") and config.get("site_packages"):
            entry = "import sys; sys.path.insert(0,sys.argv.pop(1)); import cli; cli.main()"
            command = [config["python"], "-X", "utf8", "-B", "-c", entry, config["site_packages"]]
        else:
            command = list(config.get("cli_command", ["pandaai-cli"]))
        return self._invoke(command + ["--json", *args], timeout)

    def balance(self) -> dict:
        raw = self.cli("balance")["balance"]
        balance = {"total": float(raw["computingPower"]), "gift": float(raw.get("giftBalance", 0)),
                   "free": float(raw.get("freeBalance", 0)), "recharge": float(raw.get("rechargeBalance", 0)),
                   "gift_expires": raw.get("nextExpireAt"),
                   "observed_at": datetime.now(timezone.utc).isoformat()}
        # Retain only the original balance fields, never account identifiers.
        balance["raw_wallet_balances"] = {name: raw[name] for name in
            ("computingPower", "giftBalance", "freeBalance", "rechargeBalance") if name in raw}
        balance["funding_models"] = sorted(_wallet_funding_models(balance))
        return balance

    def create(self, candidate: dict, window: dict, cycle: int, groups: int, name: str) -> str:
        if candidate.get("official_task") == "stateful_strategy" or candidate.get("economic_evaluation_type") == "stateful_account":
            raise ValueError("Stateful strategies require the dedicated account workflow, never factor_create")
        # Validate the actual CLI loading dates even when create is called
        # directly. The separate evaluation contract must never silently
        # replace these dates after authorization or preflight.
        fingerprint(candidate, window, cycle, groups)
        mode = "--formula" if candidate.get("formula") else "--code"
        source = candidate.get("formula") or candidate.get("code")
        if not source:
            raise ValueError("Candidate has no executable definition")
        path = _frozen_code_path(candidate)
        if path is not None:
            # Documented file mode keeps large financial-source literals out
            # of the Windows command line without changing the definition.
            mode, source = "--file", str(path)
        data = self.cli("factor_create", mode, source, "--name", name,
                        "--start-date", window["start"].replace("-", ""),
                        "--end-date", window["end"].replace("-", ""),
                        "--adjustment-cycle", str(cycle), "--group-number", str(groups),
                        "--factor-direction", str(candidate["direction"]))
        return data["factor_id"]

    def _bridge(self, action: str, object_id: str) -> dict:
        if not self.config.get("python") or not self.config.get("site_packages"):
            raise ValueError("Resume-safe dispatch needs platform.python and platform.site_packages")
        return self._invoke([self.config["python"], "-X", "utf8", "-B",
                             str(Path(__file__).with_name("platform_worker.py")),
                             self.config["site_packages"], action, object_id])

    def start(self, factor_id: str) -> str:
        return self._bridge("start", factor_id)["run_id"]

    def status(self, run_id: str) -> Any:
        return self._bridge("status", run_id)["status"]

    def logs(self, run_id: str) -> dict:
        return self._bridge("logs", run_id)

    def result(self, run_id: str) -> dict:
        return self.cli("factor_result", run_id, timeout=480)


class ExperimentLedger:
    def __init__(self, path: str | Path, *, read_only: bool = False):
        path = Path(path)
        if read_only:
            self.db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30, isolation_level=None)
            self.db.row_factory = sqlite3.Row
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS jobs (
            fingerprint TEXT PRIMARY KEY, candidate_id TEXT, category TEXT, day TEXT,
            state TEXT, reserved REAL DEFAULT 0, actual REAL, factor_id TEXT, run_id TEXT,
            definition TEXT, balance_before TEXT, receipt TEXT)""")
        self.db.execute("CREATE TABLE IF NOT EXISTS research_budget_control (singleton INTEGER PRIMARY KEY CHECK(singleton=1), policy TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS research_budget_reviews (review_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")

    def get(self, key: str) -> dict | None:
        row = self.db.execute("SELECT * FROM jobs WHERE fingerprint=?", (key,)).fetchone()
        return dict(row) if row else None

    def jobs(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM jobs ORDER BY rowid")]

    def research_state(self) -> dict:
        return research_state(self.db)

    def reserve(self, key: str, candidate: dict, definition: dict, category: str,
                ceiling: float, daily_limit: float, balance: dict, day: str | None = None):
        if category not in ("source_probe", "exploration", "validation"):
            raise ValueError("Unknown research category")
        if not math.isfinite(ceiling) or ceiling <= 0:
            raise ValueError("A positive current cost reservation is required")
        day = day or beijing_day()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if self.get(key):
                raise ValueError("This exact candidate/window is already reserved; resume it")
            if definition.get("backend_contract") == "quantfactra-v1" and any(
                    row["state"] == "CANCELLED" and row.get("actual") is None for row in self.jobs()):
                raise ValueError("Reconcile the prior unknown cancelled bill before dispatching another")
            active = self.db.execute("SELECT count(*) FROM jobs WHERE state NOT IN ('SETTLED','CANCELLED')").fetchone()[0]
            if active:
                raise ValueError("Reconcile the prior job before dispatching another")
            spent = self.db.execute("SELECT COALESCE(SUM(COALESCE(actual,reserved)),0) FROM jobs WHERE day=? AND state!='CANCELLED'", (day,)).fetchone()[0]
            if spent + ceiling > daily_limit + 1e-9:
                raise ValueError("Daily experiment budget exhausted")
            from .backend_contract import check_cost_units
            check_cost_units(self.jobs(), definition.get("cost_unit", "credits"))
            native = definition.get("backend_contract") == "quantfactra-v1"
            if native:
                from .backend_contract import validate_balance
                validate_balance(balance, definition["cost_unit"])
                if balance["available"] is not None and ceiling > balance["available"]:
                    raise ValueError("Account balance below reservation")
            elif ceiling > balance["total"]:
                raise ValueError("Account balance below reservation")
            from .team.budget import guard_reservation
            guard_reservation(self.db, key, candidate, definition, ceiling, balance)
            meta = definition.get("research_budget")
            if meta is None and self.research_state().get("policy"):
                raise ValueError("Active study requires research_budget metadata")
            if meta is not None:
                policy = load_policy({"official_research_policy": meta["limits"]})
                if meta != research_request(policy, candidate, category):
                    raise ValueError("Study request metadata differs from the frozen candidate")
                check = research_guard(policy, meta, self.jobs(), self.research_state(), ceiling)
                if check["blocker"]:
                    raise ValueError(check["blocker"])
                bind_policy(self.db, policy)
            billing = definition.get("billing_authorization", {})
            recharge_reserved = (ceiling if definition["billing_mode"] == "prepaid" else 0.) if native else max(0., ceiling - float(balance["gift"]))
            if billing:
                problem = _batch_limit_problem(self.jobs(), billing, ceiling, recharge_reserved,
                                               additional_runs=1)
                if problem:
                    raise ValueError(problem)
                definition = dict(definition, recharge_reservation=recharge_reserved)
            self.db.execute("INSERT INTO jobs (fingerprint,candidate_id,category,day,state,reserved,definition,balance_before) VALUES (?,?,?,?,?,?,?,?)",
                            (key, candidate["candidate_id"], category, day, "RESERVED", ceiling,
                             json.dumps(definition, sort_keys=True), json.dumps(balance)))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def transition(self, key: str, expected: str, state: str, **fields):
        allowed = {"factor_id", "run_id", "receipt", "actual"}
        if set(fields) - allowed:
            raise ValueError("Invalid ledger fields")
        assignments = ["state=?"] + [f"{k}=?" for k in fields]
        result = self.db.execute(f"UPDATE jobs SET {','.join(assignments)} WHERE fingerprint=? AND state=?",
                                 (state, *fields.values(), key, expected))
        if result.rowcount != 1:
            raise ValueError("Job changed concurrently or has an unexpected state")


def read_experiment_jobs(path: str | Path) -> list[dict]:
    """Inspect a budget ledger without creating or migrating it."""
    path = Path(path).resolve()
    if not path.is_file():
        return []
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute("SELECT * FROM jobs ORDER BY rowid")]


def read_research_state(path: str | Path) -> dict:
    path = Path(path).resolve()
    if not path.is_file():
        return {}
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        return research_state(connection)


def _batch_authorization(authorization: Any, amount_field: str, minimum: float,
                         key: str | None = None, estimated_risk: bool = False) -> dict:
    if (not isinstance(authorization, dict) or not isinstance(authorization.get("batch_id"), str)
            or not authorization["batch_id"].strip()):
        raise ValueError("Today's explicit scoped batch authorization is required")
    first, last = authorization.get("valid_from"), authorization.get("valid_until")
    if first is not None or last is not None:
        try:
            start, end = date.fromisoformat(first), date.fromisoformat(last)
            today = date.fromisoformat(beijing_day())
            valid = (start.isoformat() == first and end.isoformat() == last and start <= today <= end)
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise ValueError("Scoped batch authorization is outside its explicit finite validity period")
    elif authorization.get("day") != beijing_day():
        raise ValueError("Today's explicit scoped batch authorization is required")
    fingerprints = authorization.get("fingerprints")
    if (not isinstance(fingerprints, list) or not fingerprints or
            any(not isinstance(value, str) or len(value) != 64 or
                any(character not in "0123456789abcdef" for character in value) for value in fingerprints)):
        raise ValueError("Batch authorization requires exact experiment fingerprints")
    amount = authorization.get(amount_field)
    if type(amount) not in (int, float) or not math.isfinite(amount) or amount < minimum:
        raise ValueError(f"Batch authorization {amount_field} must cover the configured credit limit")
    if key is not None and key not in fingerprints:
        raise ValueError("This definition/window is outside the authorized batch fingerprints")
    if estimated_risk and authorization.get("accept_estimated_charge_risk") is not True:
        raise ValueError("The concrete batch must explicitly accept estimated-charge risk")
    if "max_runs" in authorization and (type(authorization["max_runs"]) is not int or authorization["max_runs"] <= 0):
        raise ValueError("Batch max_runs must be a positive integer")
    for field in ("max_total_credits", "max_recharge_credits"):
        if field in authorization and (type(authorization[field]) not in (int, float)
                or not math.isfinite(authorization[field]) or authorization[field] < 0):
            raise ValueError(f"Batch {field} must be finite and nonnegative")
    return authorization


def _job_definition(job: dict) -> dict:
    return json.loads(job["definition"]) if job.get("definition") else {}


def _batch_usage(jobs: list[dict], batch_id: str, *, exclude_key: str | None = None) -> dict:
    """Use actual settled charges, reserved unresolved amounts, and all attempts."""
    total, recharge, runs = 0., 0., 0
    for job in jobs:
        if job["state"] == "CANCELLED" or job["fingerprint"] == exclude_key:
            continue
        definition = _job_definition(job)
        permissions = definition.get("billing_authorization", {})
        if not any(isinstance(value, dict) and value.get("batch_id") == batch_id
                   for value in permissions.values()):
            continue
        runs += 1
        charged = float(job["actual"] if job["actual"] is not None else job["reserved"])
        total += charged
        receipt = json.loads(job["receipt"]) if job.get("receipt") else {}
        if "recharge_delta" in receipt:
            debit = receipt["recharge_delta"]
            if type(debit) not in (int, float) or not math.isfinite(debit) or debit < 0:
                raise ValueError("Batch recharge settlement is not reconciled")
            recharge += debit
        elif job["actual"] is not None:
            # An explicit receipt without funding buckets does not prove zero
            # recharge. Conservatively consume the amount until reconciled.
            recharge += charged if permissions.get("recharge") else 0.
        else:
            recharge += float(definition.get("recharge_reservation", 0.))
    return {"total": total, "recharge": recharge, "runs": runs}


def _batch_limit_problem(jobs: list[dict], permissions: dict, total: float,
                         recharge: float, *, additional_runs: int = 0,
                         exclude_key: str | None = None) -> str | None:
    for authorization in permissions.values():
        if not isinstance(authorization, dict):
            continue
        used = _batch_usage(jobs, authorization["batch_id"], exclude_key=exclude_key)
        if "max_total_credits" in authorization and used["total"] + total > authorization["max_total_credits"] + 1e-9:
            return "Confirmed batch total budget exhausted"
        if "max_recharge_credits" in authorization and used["recharge"] + recharge > authorization["max_recharge_credits"] + 1e-9:
            return "Confirmed batch recharge budget exhausted"
        if "max_runs" in authorization and used["runs"] + additional_runs > authorization["max_runs"]:
            return "Confirmed batch run count exhausted"
    if recharge > 1e-9 and not permissions.get("recharge"):
        return "Recharge debit has no explicit batch permission"
    return None


def _adaptive_estimate(compute: dict, jobs: list[dict]) -> float | None:
    estimate = compute.get("planning_credit_estimate", compute.get("cost_ceiling"))
    if estimate is not None and (type(estimate) not in (int, float) or not math.isfinite(estimate) or estimate <= 0):
        raise ValueError("planning_credit_estimate must be a positive finite number")
    if compute.get("cost_ceiling") is None and estimate is not None:
        settled = [float(job["actual"]) for job in jobs if job["state"] == "SETTLED"
                   and job.get("actual") is not None and job["actual"] > 0
                   and _job_definition(job).get("cost_basis") == "authorized_estimate"]
        if settled:
            estimate = max(float(estimate), max(settled[-5:]))
    return estimate


def budget_plan(candidates: list[dict], compute: dict, balance: dict, *, jobs: list[dict] | None = None,
                study_state: dict | None = None) -> dict:
    """Preview caps or authorized estimates; shared-priority quotas are targets.

    Supplying current ledger jobs accounts for already consumed daily/batch
    budgets. Dispatch always supplies them and rechecks atomically at reserve.
    """
    if balance.get("schema") == "quantfactra-balance-v1":
        from .backend_contract import budget_plan as native_budget_plan
        return native_budget_plan(candidates, compute, balance, jobs=jobs, study_state=study_state)
    jobs = jobs or []
    from .backend_contract import check_cost_units
    check_cost_units(jobs, "credits")
    research_policy = load_policy(compute)
    ceiling = compute.get("cost_ceiling")
    if ceiling is not None and (type(ceiling) not in (int, float) or not math.isfinite(ceiling) or ceiling <= 0):
        raise ValueError("cost_ceiling must be a positive finite number or null")
    for name in ("daily_credit_limit", "recharge_credit_limit"):
        amount = compute.get(name, 0)
        if type(amount) not in (int, float) or not math.isfinite(amount) or amount < 0:
            raise ValueError(f"{name} must be finite and nonnegative")
    verified = bool(compute.get("cost_ceiling_verified_at") and compute.get("cost_ceiling_source"))
    enforceable = compute.get("server_enforced_cap", False)
    capped = bool(ceiling and verified and enforceable is True)
    estimated_risk_requested = compute.get("allow_estimated_billing", False) and not capped
    estimated_risk_authorized = False
    estimate = _adaptive_estimate(compute, jobs)
    new_estimate = ceiling is None and estimated_risk_requested
    risk_authorization, recharge_authorization = None, None
    authorization_problem = None
    if estimated_risk_requested:
        try:
            risk_authorization = _batch_authorization(
                compute.get("estimated_billing_batch_authorization"), "max_total_credits",
                float(estimate or 0) if new_estimate else float(compute["daily_credit_limit"]), estimated_risk=True)
            estimated_risk_authorized = True
        except ValueError as exc:
            authorization_problem = str(exc)
    if compute.get("recharge_credit_limit", 0) > 0:
        try:
            recharge_authorization = _batch_authorization(compute.get("recharge_batch_authorization"), "max_recharge_credits",
                                                        float(compute["recharge_credit_limit"]))
        except ValueError as exc:
            authorization_problem = str(exc)
    limit = min(float(compute["daily_credit_limit"]), balance["gift"] + float(compute.get("recharge_credit_limit", 0)))
    run_slots = None
    if new_estimate and risk_authorization and not authorization_problem:
        used = _batch_usage(jobs, risk_authorization["batch_id"])
        daily_spent = sum(float(job["actual"] if job["actual"] is not None else job["reserved"])
                          for job in jobs if job["day"] == beijing_day() and job["state"] != "CANCELLED")
        recharge_left = 0.
        if recharge_authorization:
            if recharge_authorization["batch_id"] != risk_authorization["batch_id"]:
                authorization_problem = "Recharge and estimated-billing permissions must refer to the same confirmed batch"
            else:
                recharge_left = max(0., recharge_authorization["max_recharge_credits"] - used["recharge"])
        limit = max(0., min(float(compute["daily_credit_limit"]) - daily_spent,
                           risk_authorization["max_total_credits"] - used["total"],
                           balance["gift"] + recharge_left))
        maxima = [authorization["max_runs"] for authorization in (risk_authorization, recharge_authorization)
                  if authorization and "max_runs" in authorization]
        if maxima:
            run_slots = max(0, min(maxima) - used["runs"])
    cost_basis, reservation = None, None
    if not authorization_problem:
        if ceiling and verified and enforceable is True:
            cost_basis, reservation = "enforced_cap", float(ceiling)
        elif estimated_risk_authorized:
            # Existing concretely authorized estimate batches may retain their
            # old reservation field. A new estimate has its own evidence and
            # never becomes a fictional service-side cost ceiling.
            if ceiling and verified:
                reservation = float(ceiling)
            elif (estimate and isinstance(compute.get("planning_cost_source"), str)
                  and compute["planning_cost_source"].strip()
                  and isinstance(compute.get("estimate_verified_at"), str)
                  and compute["estimate_verified_at"].strip()):
                reservation = float(estimate)
            if reservation:
                cost_basis = "authorized_estimate"
    ready = bool(cost_basis and reservation and not authorization_problem)
    study_check = research_guard(research_policy, None, jobs, study_state, float(reservation or estimate or 0))
    if study_check["blocker"]:
        ready = False
        authorization_problem = authorization_problem or study_check["blocker"]
    slots = int(limit // (reservation if ready else estimate)) if (ready or estimate) else 0
    if run_slots is not None:
        slots = min(slots, run_slots)
    fractions = {k: compute[f"{k}_fraction"] for k in ("source_probe", "exploration", "validation")}
    if any(type(f) not in (int, float) or not math.isfinite(f) or f < 0 for f in fractions.values()) or not math.isclose(sum(fractions.values()), 1):
        raise ValueError("Research category fractions must be nonnegative and sum to one")
    allocation = compute.get("allocation_mode", "category_quota")
    if allocation not in ("category_quota", "shared_priority"):
        raise ValueError("Unknown research allocation mode")
    quota = {k: (max(0 if allocation == "shared_priority" else 1, round(slots * v)) if slots else 0)
             for k, v in fractions.items()}
    ordered, counters = [], dict.fromkeys(quota, 0)
    local_review, research_blocked, planned_jobs = [], [], list(jobs)
    ranked = list(candidates)
    if allocation == "shared_priority":
        for candidate in ranked:
            priority = candidate.get("research_priority", 0)
            if type(priority) not in (int, float) or not math.isfinite(priority):
                raise ValueError("research_priority must be a finite number")
        ranked.sort(key=lambda item: item.get("research_priority", 0), reverse=True)
    for c in ranked:
        category = c.get("experiment_category", "exploration")
        if category not in quota:
            raise ValueError("Unknown experiment category")
        try:
            meta = research_request(research_policy, c, category)
        except ValueError as error:
            target = local_review if c.get("official_task") in {"syntax_check", "cost_sensitivity", "attribution", "correlation", "pool_increment", "stability_replay"} else research_blocked
            target.append({"candidate_id": c["candidate_id"], "reason": str(error)})
            continue
        check = research_guard(research_policy, meta, planned_jobs, study_state, float(reservation or estimate or 0))
        if check["blocker"]:
            research_blocked.append({"candidate_id": c["candidate_id"], "reason": check["blocker"]})
            continue
        if len(ordered) < slots and (allocation == "shared_priority" or counters[category] < quota[category]):
            ordered.append({"candidate_id": c["candidate_id"], "category": category})
            counters[category] += 1
            if meta:
                planned_jobs.append({"fingerprint": "planned:" + str(len(ordered)), "state": "RESERVED",
                                     "actual": None, "reserved": float(reservation or estimate or 0),
                                     "definition": json.dumps({"research_budget": meta})})
    return {"date_beijing": beijing_day(), "budget": limit, "cost_ceiling": ceiling,
            "dispatch_ready": ready, "server_enforced_cap": enforceable,
            "cost_basis": cost_basis, "reservation_credit_estimate": reservation,
            "planning_only_estimate": None if ready else estimate,
            "blocker": None if ready else authorization_problem or "Current pricing/cap evidence or estimated-billing authorization required",
            "authorization_scope": "Exact fingerprints are checked again at dispatch",
            "allocation_mode": allocation,
            "allocation_note": ("Category fractions are targets; spare slots, including zero-fraction categories, may be borrowed"
                                if allocation == "shared_priority" else "Category quotas limit selection"),
            "quota": quota, "selected": ordered, "unspent_slots": max(0, slots - len(ordered)),
            "research_budget": study_check, "local_review": local_review,
            "research_blocked": research_blocked,
            "official_test_not_pool_admission": True,
            "admission_gates_apply_at": "Formal pool admission only; official research categories require executable research evidence"}


def dispatch(candidate: dict, window: dict, config: dict, ledger: ExperimentLedger, client: PandaClient,
             category: str = "exploration", *, before_dispatch=None, native_fixture_reader=None, before_remote=None) -> dict:
    research, compute = config["research"], config["compute"]
    identity = check_backend_config(client, config.get("platform", {}))
    native = uses_native_contract(client)
    if compute.get("team_permit", {}).get("environment") == "synthetic" and getattr(client, "simulation", False) is not True:
        raise ValueError("Synthetic permit cannot use an official client")
    cycle, groups = research["cycle"], research["groups"]
    if type(candidate.get("direction")) is not int or candidate.get("direction") not in (0, 1):
        raise ValueError("Direction must have frozen evidence before execution")
    requirements = candidate.get("data_requirements", [])
    informative_research = (category in ("source_probe", "exploration", "validation") and
                            all(isinstance(candidate.get(field), str) and candidate[field].strip()
                                for field in ("research_question", "decision_if_pass", "decision_if_fail")))
    allowed_pending = SOFT_RESEARCH_PENDING
    if category == "source_probe" and candidate.get("official_task") == "source_diagnostic":
        allowed_pending = allowed_pending | NATIVE_SOURCE_DIAGNOSTIC_PENDING
    unresolved = [r for r in requirements if not (informative_research and isinstance(r, str) and r in allowed_pending)]
    if (candidate.get("source", "").startswith("historical") or
        candidate.get("provenance") == "historical_memory_requires_revalidation" or
        any("requires_source:" in str(r) or "revalidate_legacy" in str(r) or "_pending" in str(r)
            or "direction_evidence" in str(r) or "direction_requires_evidence" in str(r) for r in unresolved)):
        raise ValueError("Resolve legacy operator/source/direction evidence before official execution")
    key = fingerprint(candidate, window, cycle, groups)
    existing = ledger.get(key)
    if existing:
        check_job_backend(existing, client)
        return existing  # Zero duplicate dispatches on resume.
    from .backend_contract import check_cost_units
    check_cost_units(ledger.jobs(), identity.get("cost_unit", "credits"))
    # Reject local loading errors before reserving funds or touching the account.
    # create also rechecks the bytes immediately before its service request.
    _frozen_code_path(candidate)
    research_policy = load_policy(compute)
    research_meta = research_request(research_policy, candidate, category)
    study_check = research_guard(research_policy, research_meta, ledger.jobs(), ledger.research_state(),
                                 float(_adaptive_estimate(compute, ledger.jobs()) or 0))
    if study_check["blocker"]:
        raise ValueError(study_check["blocker"])
    native_preflight = None
    if native:
        from .backend_contract import preflight
        native_preflight = preflight(client, candidate, window, cycle, groups, key)
    elif candidate.get("code") and not candidate.get("formula"):
        from .native_preflight import verify_native_preflight
        if native_fixture_reader is None:
            native_preflight = verify_native_preflight(candidate, window, cycle, groups)
        else:
            # The caller supplies trusted local source code explicitly. No
            # candidate string or receipt can import a reader on its own.
            native_preflight = verify_native_preflight(candidate, window, cycle, groups,
                                                        fixture_reader=native_fixture_reader)
    recharge_authorization, risk_authorization = None, None
    if not native and compute.get("recharge_credit_limit", 0) > 0:
        recharge_authorization = _batch_authorization(
            compute.get("recharge_batch_authorization"), "max_recharge_credits",
            float(compute["recharge_credit_limit"]), key)
    verified_cap = (compute.get("cost_ceiling") and compute.get("cost_ceiling_verified_at")
                    and compute.get("cost_ceiling_source") and compute.get("server_enforced_cap") is True)
    if not native and compute.get("allow_estimated_billing", False) and not verified_cap:
        minimum_risk = (_adaptive_estimate(compute, ledger.jobs()) or 0) if compute.get("cost_ceiling") is None else float(compute["daily_credit_limit"])
        risk_authorization = _batch_authorization(
            compute.get("estimated_billing_batch_authorization"), "max_total_credits",
            minimum_risk, key, estimated_risk=True)
        if recharge_authorization and recharge_authorization["batch_id"] != risk_authorization["batch_id"]:
            raise ValueError("Recharge and estimated-billing permissions must refer to the same confirmed batch")
    if before_remote is not None:
        before_remote()
    balance = client.balance()
    if native:
        from .backend_contract import validate_balance
        validate_balance(balance, identity["cost_unit"])
    plan = budget_plan([dict(candidate, experiment_category=category)], compute, balance,
                       jobs=ledger.jobs(), study_state=ledger.research_state())
    if not plan["dispatch_ready"] or not plan["selected"]:
        raise ValueError(plan["blocker"] or "No authorized budget slot")
    ceiling = float(plan["reservation_credit_estimate"])
    if native:
        permissions = plan["billing_authorization"]
        for purpose, permission in permissions.items():
            _batch_authorization(permission, "max_recharge_credits" if purpose == "recharge" else "max_total_credits",
                                 ceiling, key, estimated_risk=purpose == "estimated_billing")
        recharge_authorization = permissions.get("recharge")
        risk_authorization = permissions.get("estimated_billing")
    if not native and compute["billing_mode"] == "gift_only" and balance["gift"] < ceiling:
        raise ValueError("Gift balance cannot fund the next reservation")
    definition = {"candidate": candidate, "window": window, "cycle": cycle, "groups": groups}
    bind_backend(definition, identity)
    if native:
        definition.update(backend_contract="quantfactra-v1", cost_unit=identity["cost_unit"],
                          billing_mode=balance["billing_mode"])
    if compute.get("team_permit") is not None:
        definition["team_permit"] = compute["team_permit"]
    permissions = {name: authorization for name, authorization in
                   (("estimated_billing", risk_authorization), ("recharge", recharge_authorization))
                   if authorization is not None}
    if permissions:
        definition["billing_authorization"] = permissions
    definition["cost_basis"] = plan["cost_basis"]
    definition["cost_ceiling"] = plan["cost_ceiling"] if native else compute.get("cost_ceiling")
    definition["reservation_credit_estimate"] = ceiling
    definition["daily_credit_limit"] = compute["daily_credit_limit"]
    definition["official_test_not_pool_admission"] = True
    if research_meta:
        definition["research_budget"] = research_meta
    if native_preflight is not None:
        definition["native_preflight"] = native_preflight
    if before_remote is not None:
        before_remote()
    if before_dispatch is not None:
        # Definition registration precedes any account mutation or service run.
        # A failed registration must not reserve funds or create a service job.
        before_dispatch()
    ledger.reserve(key, candidate, definition, category, ceiling, float(compute["daily_credit_limit"]), balance)
    from .team.budget import guard_remote_mutation
    # Unknown responses remain ambiguous; do not silently retry account mutations.
    if before_remote is not None:
        before_remote()
    guard_remote_mutation(ledger.db, key, "RESERVED")
    ledger.transition(key, "RESERVED", "CREATING")
    factor_id = client.create(candidate, window, cycle, groups, ("QF-" if native else "PA-") + key[:16])
    if native:
        from .backend_contract import reference_id
        reference_id(factor_id)
    ledger.transition(key, "CREATING", "CREATED", factor_id=factor_id)
    if before_remote is not None:
        before_remote()
    guard_remote_mutation(ledger.db, key, "CREATED")
    ledger.transition(key, "CREATED", "DISPATCHING")
    run_id = client.start(factor_id)
    if native:
        reference_id(run_id)
    ledger.transition(key, "DISPATCHING", "RUNNING", run_id=run_id)
    return ledger.get(key)


def settle_receipt(key: str, ledger: ExperimentLedger, receipt: dict) -> dict:
    """Reconcile an ambiguous/zero-charge execution with an explicit billing receipt.

    The caller must verify the attached official billing record. Balance deltas
    alone are insufficient to release a zero-charge or midnight-crossing job.
    """
    job = ledger.get(key)
    if not job or job["state"] == "SETTLED":
        raise ValueError("Unknown/already settled job")
    amount = receipt.get("actual")
    if type(amount) not in (int, float) or not math.isfinite(amount) or amount < 0:
        raise ValueError("Receipt must state a finite nonnegative charge")
    if (not receipt.get("official_receipt_path") or not receipt.get("official_receipt_sha256") or
            not receipt.get("verified_by") or receipt.get("run_id") != job["run_id"] or
            receipt.get("final_status") not in ("SUCCESS", "FAILED", "BILLING_STOP")):
        raise ValueError("Final status, verified receipt and matching Run ID required")
    artifact = Path(receipt["official_receipt_path"])
    if hashlib.sha256(artifact.read_bytes()).hexdigest() != receipt["official_receipt_sha256"]:
        raise ValueError("Official billing artifact has changed")
    ledger.transition(key, job["state"], "SETTLED", actual=amount, receipt=json.dumps(receipt))
    return ledger.get(key)


def resume(key: str, ledger: ExperimentLedger, client: PandaClient, output_dir: Path) -> dict:
    job = ledger.get(key)
    if not job:
        raise ValueError("Unknown job")
    check_job_backend(job, client)
    team_environment = json.loads(job.get("definition") or "{}").get("team_permit", {}).get("environment")
    if team_environment and (team_environment == "synthetic") != (getattr(client, "simulation", False) is True):
        raise ValueError("Resume client must match the persisted execution environment")
    if uses_native_contract(client):
        from .backend_contract import resume as native_resume
        return native_resume(key, ledger, client, output_dir)
    if not job["run_id"]:
        return {"state": job["state"], "action": "Read factor_list/factor_info and reconcile ambiguous mutation before retrying"}
    if job["state"] == "SETTLED":
        return job
    status = client.status(job["run_id"])
    if status not in (2, 3, 6):
        return {"state": job["state"], "platform_status": status, "run_id": job["run_id"]}
    output_dir.mkdir(parents=True, exist_ok=True)
    result_path = output_dir / (job["run_id"] + ".json")
    if status == 2 and not result_path.exists():
        result_path.write_text(json.dumps(client.result(job["run_id"]), ensure_ascii=False), encoding="utf-8")
    failure = {}
    if status == 3:
        log_path = output_dir / (job["run_id"] + ".logs.json")
        try:
            if not log_path.exists():
                log_path.write_text(json.dumps(client.logs(job["run_id"]), ensure_ascii=False), encoding="utf-8")
            logs = json.loads(log_path.read_text(encoding="utf-8"))
            failure = {"failure_log_path": str(log_path),
                       "failure_log_sha256": hashlib.sha256(log_path.read_bytes()).hexdigest(),
                       "failure_logs_complete": logs.get("complete", False),
                       "node_errors": logs.get("errors", [])}
        except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired):
            # A read-only diagnostic failure must not obscure billing settlement.
            failure = {"failure_logs_pending": True,
                       "failure_log_action": "Read the same owned Run ID's logs; do not redispatch"}
    before, after = json.loads(job["balance_before"]), client.balance()
    actual = before["total"] - after["total"]
    if not math.isfinite(actual) or actual <= 0:
        return {"state": "BILLING_PENDING", "run_id": job["run_id"], "platform_status": status,
                "action": "Re-query balance after settlement; no re-dispatch or new reservation", **failure}
    receipt = {"run_id": job["run_id"], "status": status, "actual": actual,
               "gift_delta": before["gift"] - after["gift"],
               "recharge_delta": before["recharge"] - after["recharge"],
               "free_delta": before.get("free", 0.) - after.get("free", 0.),
               "attribution": "serial balance delta, provisional; external spend/grants may confound",
               "reservation_overrun": actual > job["reserved"], "day_before": job["day"], "day_after": beijing_day(),
               "wallet_balances": {"before": {name: before.get(name, 0.) for name in
                                               ("total", "gift", "recharge", "free")},
                                   "after": {name: after.get(name, 0.) for name in
                                              ("total", "gift", "recharge", "free")}},
               "raw_wallet_balances": {"before": before.get("raw_wallet_balances"),
                                       "after": after.get("raw_wallet_balances")},
               **failure}
    if receipt["day_before"] != receipt["day_after"]:
        return {"state": "BILLING_REVIEW", "receipt": receipt, "action": "Daily expiry/grant prevents automatic balance-delta settlement"}
    models = _wallet_funding_models(before) & _wallet_funding_models(after)
    if not models:
        return {"state": "BILLING_REVIEW", "receipt": receipt,
                "action": "Wallet funding model is unknown or changed; future dispatch remains locked"}
    if "gift_recharge_free_alias" in models:
        model, names = "gift_recharge_free_alias", ("gift", "recharge")
        aliases = {"free": "gift"}
    elif "independent_gift_recharge_free" in models:
        model, names = "independent_gift_recharge_free", ("gift", "recharge", "free")
        aliases = {}
    else:
        model, names, aliases = "gift_recharge", ("gift", "recharge"), {}
    receipt.update(funding_model=model, funding_aliases=aliases,
                   funding_bucket_deltas={name: receipt[name + "_delta"] for name in names})
    buckets = tuple(receipt[name + "_delta"] for name in names)
    if (any(type(value) not in (int, float) or not math.isfinite(value) or value < -1e-9 for value in buckets)
            or any(after.get(name, 0.) < -1e-9 for name in ("gift", "recharge", "free"))
            or not math.isclose(sum(buckets), actual, rel_tol=1e-9, abs_tol=1e-7)):
        return {"state": "BILLING_REVIEW", "receipt": receipt,
                "action": "Funding bucket deltas do not reconcile; future dispatch remains locked"}
    definition = _job_definition(job)
    permissions = definition.get("billing_authorization", {})
    problem = _batch_limit_problem(ledger.jobs(), permissions, actual, max(0., receipt["recharge_delta"]),
                                   exclude_key=key)
    flexible_estimate = (definition.get("cost_basis") == "authorized_estimate"
                         and definition.get("cost_ceiling") is None and permissions.get("estimated_billing"))
    if flexible_estimate:
        daily_used = sum(float(row["actual"] if row["actual"] is not None else row["reserved"])
                         for row in ledger.jobs() if row["day"] == job["day"]
                         and row["fingerprint"] != key and row["state"] != "CANCELLED")
        if daily_used + actual > definition["daily_credit_limit"] + 1e-9:
            problem = "Daily experiment budget exceeded by actual settlement"
        receipt["estimate_overrun"] = actual > job["reserved"] + 1e-9
    elif actual > job["reserved"] + 1e-9:
        problem = "Enforced or legacy reservation ceiling exceeded"
    if problem:
        return {"state": "BILLING_REVIEW", "receipt": receipt,
                "action": problem + "; future dispatch remains locked"}
    ledger.transition(key, job["state"], "SETTLED", actual=actual, receipt=json.dumps(receipt))
    return ledger.get(key)
