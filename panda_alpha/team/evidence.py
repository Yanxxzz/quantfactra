"""验收原 Run 的报告结构与审查绑定，分别记录交付、来源和经济结论。

报告成功不能代替 PIT 核验；漂亮的汇总指标也不是净收益或入池证明。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from pathlib import Path

from ..platform import SOFT_RESEARCH_PENDING
from ..registry import verify_artifact
from .contracts import strings, text_fields


def artifact(path):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def read_artifact(binding):
    return json.loads(Path(verify_artifact(binding)["path"]).read_text(encoding="utf-8-sig"))


def _series(chart):
    dates = chart.get("x", [{}])[0].get("data", []) if chart.get("x") else []
    series = chart.get("y", [])
    return dates, series


def _finite(values):
    return bool(values) and all(type(v) in (int, float) and math.isfinite(v) for v in values)


def required_qualifications(task, contract):
    request = task["request"]
    declared = request.get("source_requirements", [])
    pending = [s for s in request["candidate"].get("data_requirements", []) if s.endswith("_pending")]
    return set(declared) | set(pending) | set(contract["unknown"])


def _numeric(value):
    try:
        return math.isfinite(float(str(value).removesuffix("%")))
    except (ValueError, TypeError):
        return False


def inspect_report(task, payload, *, include_reported_metrics=True):
    """Pure cached-file adapter. Absence of an export remains an explicit unknown."""
    verified = [verify_artifact(a) for a in payload["artifacts"]]
    report, logs, seen = {}, {}, {}
    for binding in verified:
        name = Path(binding["path"]).name
        if name in {str(payload["run_id"]) + ".json", str(payload["run_id"]) + ".logs.json"}:
            if name in seen and seen[name] != binding["sha256"]:
                raise ValueError("Conflicting cached artifacts for the same Run")
            seen[name] = binding["sha256"]
        if name == str(payload["run_id"]) + ".json":
            report = read_artifact(binding)
        elif name == str(payload["run_id"]) + ".logs.json":
            logs = read_artifact(binding)
    if report.get("schema") == "quantfactra-report-v1":
        from ..backend_reports import inspect_report as inspect_native_report
        return inspect_native_report(task, payload, report, logs, verified,
                                     include_reported_metrics=include_reported_metrics)
    analysis = report.get("factor_analysis") or {}
    dates, lines = _series(analysis.get("query_return_chart") or {})
    groups = [s for s in lines if re.fullmatch(r"(?:分组|组|group\s*)[1-9]\d*", str(s.get("name", "")), re.IGNORECASE)]
    group_ids = {int(re.search(r"\d+$", s["name"])[0]) for s in groups}
    sequences = {}
    for name in ("ic", "rank_ic"):
        labels, rows = _series(analysis.get(f"query_{name}_sequence_chart") or {})
        sequences[name] = {"dates_match": bool(dates) and labels == dates,
                           "finite": bool(rows) and _finite(rows[0].get("data", []))
                           and len(rows[0]["data"]) == len(labels)}
    codes = [n["code"] for n in (report.get("nodes") or {}).values() if isinstance(n, dict) and "code" in n]
    code = task["request"]["candidate"].get("code")
    code_matches = code in codes if code and codes else None
    environment = payload.get("environment", "unknown")
    synthetic = report.get("synthetic") is True or environment == "synthetic"
    ordered_dates = bool(dates) and all(isinstance(d, str) for d in dates) and dates == sorted(set(dates))
    try:
        parsed = [datetime.fromisoformat(d).date().isoformat() for d in dates]
        dates_in_window = bool(parsed) and all(task["request"]["window"]["start"] <= d <= task["request"]["window"]["end"] for d in parsed)
    except (ValueError, TypeError):
        dates_in_window = False
    groups_complete = (len(groups) == task["request"]["groups"] and group_ids == set(range(1, task["request"]["groups"] + 1)) and ordered_dates
                       and all(len(s.get("data", [])) == len(dates) and _finite(s["data"]) for s in groups))
    table = [r for r in analysis.get("query_group_return_analysis", [])
             if re.fullmatch(r"(?:分组|组|group\s*)[1-9]\d*", str(r.get("group", "")), re.IGNORECASE)]
    table_ids = {int(re.search(r"\d+$", r["group"])[0]) for r in table}
    columns = ("annualizedReturn", "excessAnnualized", "maxDrawdown", "turnoverRate", "sharpeRatio")
    table_complete = len(table) == task["request"]["groups"] and table_ids == set(range(1, task["request"]["groups"] + 1)) and all(
        all(k in row and _numeric(row[k]) for k in columns) for row in table)
    logs_complete = logs.get("success", True) is True and logs.get("complete") is True and logs.get("run_id") == payload["run_id"]
    terminal_success = report.get("success") is True and report.get("status") == 2
    observation = {"schema": "team-result-evidence-v2" if include_reported_metrics else "team-result-evidence-v1", "task_id": task["task_id"],
            "request_hash": task["request_hash"], "fingerprint": task["fingerprint"],
            "run_id": payload["run_id"], "environment": environment, "synthetic": synthetic,
            "frozen_config": {k: task["request"][k] for k in ("window", "cycle", "groups")},
            "observed": {"code_matches": code_matches, "group_count": len(groups),
                         "group_dates": dates, "groups_complete": groups_complete,
                         "dates_in_request_window": dates_in_window, "group_table_complete": table_complete,
                         "terminal_success": terminal_success,
                         "sequences": sequences, "logs_complete": logs_complete,
                         "log_errors": len(logs.get("errors", []))},
            "report_delivery_verified": bool(terminal_success and dates_in_window and table_complete and groups_complete and logs_complete and not logs.get("errors")
                and all(s["dates_match"] and s["finite"] for s in sequences.values()) and code_matches is not False),
            "reported_metrics": {
                "scope": "Raw cached provider observations, not source certification, net-cost proof or pool admission",
                "factor_summary": analysis.get("query_factor_analysis_data", []),
                "group_returns": analysis.get("query_group_return_analysis", []),
                "rank_ic_chart": analysis.get("query_rank_ic_sequence_chart", {})},
            "source_status": "pending", "economic_status": "not_evaluated", "pool_admission": False,
            "unknown": ["native_input_and_clock", "native_eligible_support", "worker_deployment_binding"]
                       + (["candidate_code_binding"] if code and code_matches is None else []),
            "artifacts": verified}
    if not include_reported_metrics:
        observation.pop("reported_metrics")
    return observation


def assessment_contract(message, result_event):
    # A supplemental snapshot may add cached evidence to an immutable old Run.
    # validate_assessment verifies its identity and original artifact inclusion.
    return message.get("evidence_contract") or result_event["payload"].get("evidence_contract")


def validate_assessment(message, task, result_event, *, expected_environment=None):
    """Typed human/agent judgement, bound to a deterministic delivery observation."""
    value = message.get("assessment")
    if not isinstance(value, dict):
        raise ValueError("Result review requires a scoped assessment")
    if value.get("scope") not in {"capability", "source", "economic"}:
        raise ValueError("Assessment scope is capability, source or economic; admission is separate")
    if value.get("source_status") not in {"pending", "verified"}:
        raise ValueError("Declare source_status")
    if value.get("economic_status") not in {"not_evaluated", "supported", "rejected"}:
        raise ValueError("Declare economic_status")
    strings(value.get("claims"), "assessment claims")
    for key in ("pending", "resolved", "limitations"):
        items = value.get(key)
        if not isinstance(items, list) or any(not isinstance(x, str) or not x.strip() for x in items):
            raise ValueError(f"Assessment {key} must be a string list")
    if set(value["resolved"]) & set(value["pending"]):
        raise ValueError("A qualification cannot be both resolved and pending")
    core_pending = set(value["pending"]) - SOFT_RESEARCH_PENDING
    if value["source_status"] == "verified" and core_pending:
        raise ValueError("Source verified cannot retain unresolved source qualifications")
    if value["source_status"] == "pending" and not core_pending:
        raise ValueError("Pending source assessment must name the remaining source qualifications")
    if value.get("pool_admission") is not False:
        raise ValueError("Team review does not grant pool admission")
    if value["economic_status"] != "not_evaluated" and (
            value["scope"] != "economic" or value["source_status"] != "verified"):
        raise ValueError("Economic conclusions require economic scope and verified source evidence")
    if message["decision"] == "reject" and value["economic_status"] != "rejected":
        raise ValueError("Reject is an economic conclusion; use source_pending for missing evidence")
    if value["economic_status"] == "rejected" and message["decision"] != "reject":
        raise ValueError("Economic rejection must be explicit")
    if message["decision"] == "source_pending" and value["source_status"] != "pending":
        raise ValueError("source_pending decision must retain a source qualification")
    binding = assessment_contract(message, result_event)
    if not binding or binding not in message["artifacts"]:
        raise ValueError("Review must bind a result evidence contract")
    contract = read_artifact(binding)
    for key in ("task_id", "request_hash", "fingerprint"):
        if contract.get(key) != task[key]:
            raise ValueError("Result evidence contract identity differs")
    if contract.get("run_id") != result_event["payload"]["run_id"]:
        raise ValueError("Result evidence contract refers to another Run")
    reconstruction = {"run_id": contract["run_id"], "environment": contract["environment"], "artifacts": contract["artifacts"]}
    if "backend_identity" in contract:
        reconstruction["backend_identity"] = contract["backend_identity"]
    reconstructed = inspect_report(task, reconstruction,
                                   include_reported_metrics=contract.get("schema") != "team-result-evidence-v1")
    if reconstructed != contract:
        raise ValueError("Result evidence contract observations changed")
    original = read_artifact(result_event["payload"]["artifacts"][0])
    if "backend_identity" in contract and original.get("backend_identity") != contract["backend_identity"]:
        raise ValueError("Reviewed backend identity differs from the original manifest")
    if original.get("fingerprint") != task["fingerprint"] or original.get("run_id") != contract["run_id"]:
        raise ValueError("Original manifest identity differs from the reviewed Run")
    environments = [e for e in (expected_environment, original.get("environment"),
                    result_event["payload"].get("environment")) if e is not None]
    if any(contract["environment"] != e for e in environments):
        raise ValueError("Result evidence environment differs from the persisted execution")
    if any(a not in contract["artifacts"] for a in original["artifacts"]):
        raise ValueError("Result evidence contract omits original Run artifacts")
    from ..qualifications import fixed_id
    resolved_ids = {item.get("id") for change in message.get("qualification_changes", {}).get("replacements", [])
                    for item in change.get("items", []) if item.get("status") == "resolved"}
    resolved_keys = {key for key in required_qualifications(task, contract) if fixed_id(task["task_id"], key) in resolved_ids}
    if value["source_status"] == "verified" and required_qualifications(task, contract) - set(value["resolved"]) - resolved_keys - SOFT_RESEARCH_PENDING:
        raise ValueError("Source verification must explicitly resolve all frozen and observed qualifications")
    if contract["synthetic"] and (value["source_status"] == "verified" or value["economic_status"] != "not_evaluated"):
        raise ValueError("Synthetic delivery cannot establish native source or economic truth")
    if message["decision"] == "confirm" and value["scope"] == "capability" and not (
            contract["report_delivery_verified"] or contract["synthetic"]):
        raise ValueError("Capability confirmation requires delivered reports and complete logs")
    if value["source_status"] == "verified":
        text_fields(value, ("source_verification",))
        from .contracts import verify_artifacts
        verify_artifacts(value.get("source_evidence"))
        if any(a not in message["artifacts"] or a in contract["artifacts"] for a in value["source_evidence"]):
            raise ValueError("Source qualification needs separate, bound source evidence")
    if value["economic_status"] != "not_evaluated":
        text_fields(value, ("economic_verification",))
    return value
