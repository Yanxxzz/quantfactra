"""Pure inspection of provider-neutral reports; no provider calls or new labels."""
from __future__ import annotations

from datetime import date
import hashlib
import math


def inspect_report(task, payload, report, logs, artifacts, *, include_reported_metrics=True):
    from .registry import verify_artifact
    for binding in report.get("raw_artifacts", []):
        verify_artifact(binding)
    request = task["request"]
    identity = payload.get("backend_identity")
    if not identity or report.get("backend_identity") != identity:
        raise ValueError("Neutral report must match the persisted execution backend")
    if (report.get("run_id") != payload["run_id"] or report.get("fingerprint") != task["fingerprint"]
            or any(report.get(key) != request[key] for key in ("window", "cycle", "groups"))):
        raise ValueError("Neutral report differs from the frozen request or Run")
    series = report.get("series", {})
    dates, groups = series.get("dates", []), series.get("groups", [])
    finite = lambda values: bool(values) and all(type(v) in (int, float) and math.isfinite(v) for v in values)
    ordered = isinstance(dates, list) and bool(dates) and all(isinstance(d, str) for d in dates) and dates == sorted(set(dates))
    try:
        in_window = bool(dates) and all(request["window"]["start"] <= date.fromisoformat(d).isoformat() <= request["window"]["end"] for d in dates)
    except (ValueError, TypeError):
        in_window = False
    group_ids = [row.get("group") for row in groups if isinstance(row, dict)]
    complete = (len(groups) == request["groups"] and all(type(n) is int for n in group_ids)
                and set(group_ids) == set(range(1, request["groups"] + 1)) and ordered
                and all(isinstance(row, dict) and len(row.get("values", [])) == len(dates) and finite(row["values"]) for row in groups))
    sequences = {}
    for key in ("ic", "rank_ic"):
        axis = series.get(key + "_dates", dates)
        valid_axis = (isinstance(axis, list) and bool(axis) and all(isinstance(d, str) for d in axis)
                      and axis == sorted(set(axis)))
        try:
            valid_axis = valid_axis and all(request["window"]["start"] <= date.fromisoformat(d).isoformat() <= request["window"]["end"] for d in axis)
        except (ValueError, TypeError):
            valid_axis = False
        sequences[key] = {"dates_match": bool(valid_axis) and len(series.get(key, [])) == len(axis),
                          "finite": finite(series.get(key, []))}
        if key + "_dates" in series:
            sequences[key]["dates"] = axis
    metrics = report.get("metrics", {})
    table = metrics.get("groups", [])
    columns = ("annualized_return", "excess_annualized_return", "max_drawdown", "turnover", "sharpe")
    table_ids = [row.get("group") for row in table if isinstance(row, dict)]
    table_complete = (len(table) == request["groups"] and all(type(n) is int for n in table_ids)
                      and set(table_ids) == set(range(1, request["groups"] + 1))
                      and all(isinstance(row, dict) and all(type(row.get(key)) in (int, float) and math.isfinite(row[key])
                                                          for key in columns) for row in table))
    code = request["candidate"].get("code")
    observed_code = report.get("code_sha256")
    code_matches = hashlib.sha256(code.encode()).hexdigest() == observed_code if code and observed_code else None
    logs_complete = logs.get("run_id") == payload["run_id"] and logs.get("complete") is True
    errors = logs.get("errors", [])
    terminal = report.get("status") == "succeeded"
    observation = {"schema": "team-result-evidence-v3", "task_id": task["task_id"],
        "request_hash": task["request_hash"], "fingerprint": task["fingerprint"], "run_id": payload["run_id"],
        "backend_identity": identity, "environment": payload.get("environment", "unknown"),
        "synthetic": report.get("synthetic") is True or payload.get("environment") == "synthetic",
        "frozen_config": {key: request[key] for key in ("window", "cycle", "groups")},
        "observed": {"code_matches": code_matches, "group_count": len(groups), "group_dates": dates,
                     "groups_complete": complete, "dates_in_request_window": in_window,
                     "group_table_complete": table_complete, "terminal_success": terminal,
                     "sequences": sequences, "logs_complete": logs_complete,
                     "log_errors": len(errors) if isinstance(errors, list) else 1},
        "report_delivery_verified": bool(terminal and complete and in_window and table_complete and logs_complete
            and not errors and code_matches is not False and all(s["dates_match"] and s["finite"] for s in sequences.values())),
        "source_status": "pending", "economic_status": "not_evaluated", "pool_admission": False,
        "unknown": ["native_input_and_clock", "native_eligible_support", "worker_deployment_binding"]
                   + (["candidate_code_binding"] if code and code_matches is None else []), "artifacts": artifacts}
    if include_reported_metrics:
        observation["reported_metrics"] = {"scope": "Normalized provider observations; source, net-cost and pool admission remain separate",
            "provider": report.get("source"), "factor_summary": metrics.get("factor_summary", []),
            "group_returns": table, "rank_ic_chart": {"dates": series.get("rank_ic_dates", dates), "values": series.get("rank_ic", [])},
            "raw_artifacts": report.get("raw_artifacts", [])}
    return observation
