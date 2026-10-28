"""An offline, hash-bound ECB study on a real optional bt execution platform."""
from __future__ import annotations

import hashlib
from importlib.resources import files
import json
from pathlib import Path
import shutil

import numpy as np
import pandas as pd

from panda_alpha.evaluation import check_window
from panda_alpha.registry import verify_artifact
from quantfactra import create_backend
from quantfactra.execution import ExperimentLedger, dispatch, fingerprint, resume
from quantfactra.bt_engine import sdk_version


REFERENCE_SEALS = [{"start": "2017-09-20", "end": "2019-09-19"},
                   {"start": "2019-09-20", "end": "2021-09-17"}]


def _json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _binding(path):
    path = Path(path)
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _prepare(root):
    engine_version = sdk_version()  # A missing optional dependency leaves no partial case directory.
    resources = files("quantfactra").joinpath("cases", "fx_momentum")
    revision = json.loads(resources.joinpath("execution-revision.json").read_text(encoding="utf-8"))
    protocol_bytes = resources.joinpath(revision["current_protocol_file"]).read_bytes()
    if hashlib.sha256(protocol_bytes).hexdigest() != revision["current_protocol_sha256"]:
        raise ValueError("Packaged current protocol differs from its pre-execution revision receipt")
    protocol = json.loads(protocol_bytes)
    check_window(protocol["window"]["start"], protocol["window"]["end"], REFERENCE_SEALS, protocol["source_window"]["start"])
    check_window(protocol["source_window"]["start"], protocol["source_window"]["end"], REFERENCE_SEALS)
    source = json.loads(resources.joinpath("source.json").read_text(encoding="utf-8"))
    rates_bytes = resources.joinpath("rates.json").read_bytes()
    if hashlib.sha256(rates_bytes).hexdigest() != source["rates_sha256"]:
        raise ValueError("Packaged ECB data differs from the published snapshot hash")
    original_protocol_hash = hashlib.sha256(resources.joinpath("protocol.json").read_bytes()).hexdigest()
    if original_protocol_hash != source["research_protocol_sha256"] or original_protocol_hash != revision["original_protocol_sha256"]:
        raise ValueError("Packaged case protocol differs from the source-preparation receipt")
    rates = json.loads(rates_bytes)
    quotes = pd.DataFrame(rates["rows"], columns=rates["columns"]).set_index("date")
    quotes.index = pd.to_datetime(quotes.index, format="%Y-%m-%d")
    quotes = quotes.apply(pd.to_numeric, errors="coerce")
    valid = np.isfinite(quotes).all(axis=1) & (quotes > 0).all(axis=1)
    prices = (1. / quotes.loc[valid]).sort_index(axis=1)
    provenance = {"publisher": "European Central Bank", "raw_dates": len(quotes), "valid_dates": len(prices),
                  "excluded_dates": [d.date().isoformat() for d in quotes.index[~valid]],
                  "source": source, "conversion": "EUR per currency unit = reciprocal of published currency units per EUR",
                  "missing_policy": protocol["source_calendar_rule"], "data_pit_status": "not_historical_vintage_certified"}
    root.mkdir(parents=True, exist_ok=False)
    inputs = root / "inputs"
    inputs.mkdir()
    for name in ("rates.json", "source.json", "DATA_NOTICE.md", "execution-revision.json", "protocol.json"):
        (inputs / name).write_bytes(resources.joinpath(name).read_bytes())
    prices_path = inputs / "prices.csv"
    prices.to_csv(prices_path, index_label="date", date_format="%Y-%m-%d", float_format="%.17g", lineterminator="\n")
    _json(inputs / "provenance.json", provenance)
    (root / "protocol.json").write_bytes(protocol_bytes)
    lookback = int(protocol["formula"].removeprefix("MOMENTUM(close,").removesuffix(")"))
    spec = {"schema": "bt-strategy-v2", "kind": "momentum_groups", "lookback": lookback,
            "fee_reserve": protocol["fee_reserve"],
            "lag": protocol["execution_lag_observations"], "bt_version": engine_version,
            "data_sha256": _binding(prices_path)["sha256"],
            "input_window": {"start": prices.index[0].date().isoformat(), "end": prices.index[-1].date().isoformat()},
            "cost_bps": protocol["one_way_cost_bps"], "primary_cost_bps": protocol["primary_cost_bps"],
            "initial_capital": protocol["initial_capital"], "numeraire": protocol["numeraire"],
            "annualization": protocol["annualization_observations"], "cash_interest": protocol["cash_interest"],
            "currency_interest": protocol["currency_interest"], "leverage": protocol["leverage"],
            "integer_positions": protocol["integer_positions"]}
    # Code is the native platform's declarative JSON language, not Python. All
    # material input/execution settings participate in the original fingerprint.
    candidate = {"candidate_id": protocol["case_id"], "source": "ecb_public_reference_snapshot",
                 "code": json.dumps(spec, sort_keys=True, separators=(",", ":")), "direction": protocol["direction"]}
    config = {"platform": {"backend": "quantfactra.adapters.bt:build_client", "contract": "quantfactra-v1",
                "connection_id": "public-ecb-reference-case", "cost_unit": "bt_service_fee_units",
                "state_dir": "adapter", "prices_path": "inputs/prices.csv", "sealed_windows": REFERENCE_SEALS},
              "research": {"cycle": protocol["cycle"], "groups": protocol["groups"]},
              "compute": {"billing_mode": "free", "planning_credit_estimate": 1,
                          "daily_credit_limit": 1, "recharge_credit_limit": 0}}
    manifest = {"schema": "quantfactra-case-spec-v1", "candidate": candidate, "window": protocol["window"],
                "config": config, "inputs": {name: _binding(inputs / name) for name in
                    ("rates.json", "source.json", "DATA_NOTICE.md", "prices.csv", "provenance.json", "execution-revision.json", "protocol.json")},
                "protocol": _binding(root / "protocol.json")}
    _json(root / "case-spec.json", manifest)
    _json(root / "case-spec.sha256.json", _binding(root / "case-spec.json"))
    return manifest


def run_case(output, *, resume_existing=False):
    root = Path(output).resolve()
    if resume_existing:
        verify_artifact(_read(root / "case-spec.sha256.json"))
        manifest = _read(root / "case-spec.json")
        for binding in [manifest["protocol"], *manifest["inputs"].values()]:
            verify_artifact(binding)
    else:
        manifest = _prepare(root)
    config = json.loads(json.dumps(manifest["config"]))
    for key in ("state_dir", "prices_path"):
        config["platform"][key] = str(root / config["platform"][key])
    client = create_backend(config["platform"])
    ledger = ExperimentLedger(root / "experiments.sqlite3")
    try:
        existing = bool(ledger.jobs())
        job = dispatch(manifest["candidate"], manifest["window"], config, ledger, client)
        result = resume(job["fingerprint"], ledger, create_backend(config["platform"]), root / "reports")
        if result["state"] != "SETTLED":
            raise ValueError(f"Original local Run is {result['state']}; inspect adapter logs before any retry")
        report = client.result(job["run_id"])
        for binding in report["raw_artifacts"]:
            verify_artifact(binding)
        if (root / "delivery.json").exists():
            delivery = _read(root / "delivery.json")
            for binding in delivery["artifacts"]:
                verify_artifact(binding)
            saved = dict(_read(root / "summary.json"), reused_existing=True)
            if saved["case_id"] == "ecb-fx-momentum-v1":
                saved["execution_notice"] = "V1 permitted small fee borrowing. Retained read-only; use a new directory for the self-funded V2 case."
            return saved
        raw_path = Path(report["raw_artifacts"][0]["path"])
        raw = _read(raw_path)
        from panda_alpha.backend_reports import inspect_report
        from panda_alpha.team.contracts import digest
        request = {"candidate": manifest["candidate"], "window": manifest["window"],
                   "cycle": config["research"]["cycle"], "groups": config["research"]["groups"]}
        observation = inspect_report({"task_id": manifest["candidate"]["candidate_id"], "request": request,
                "request_hash": digest(request), "fingerprint": job["fingerprint"]},
            {"backend_identity": client.backend_identity, "run_id": job["run_id"], "environment": "real_local_engine"},
            report, client.logs(job["run_id"]), report["raw_artifacts"])
        if not observation["report_delivery_verified"]:
            raise ValueError("Local engine results did not pass the normalized report-delivery contract")
        _json(root / "evidence.json", observation)
        provenance = _read(root / "inputs/provenance.json")
        protocol = _read(root / "protocol.json")
        primary = {p["name"]: p for p in raw["paths"] if p["cost_bps"] == protocol["primary_cost_bps"]}
        excess = primary[f"group-{protocol['groups']}"]["metrics"]["total_return"] - primary["benchmark"]["metrics"]["total_return"]
        finding = ("较高动量组在这个固定样本、指定成本下的总回报高于等权对照。" if excess > 0 else
                   "较高动量组在这个固定样本、指定成本下没有超过等权对照。")
        summary = {"case_id": protocol["case_id"], "state": result["state"], "run_id": job["run_id"],
                   "engine": raw["engine"], "engine_backtests": len(raw["paths"]), "ledger_jobs": len(ledger.jobs()),
                   "provider_service_fee": {"actual": result["actual"], "unit": config["platform"]["cost_unit"]},
                   "primary_excess_total_return": excess, "finding_zh": finding,
                   "prices_sha256": json.loads(manifest["candidate"]["code"])["data_sha256"],
                   "evaluation_dates": len(raw["dates"]), "rebalance_dates": len(raw["targets"]["benchmark"]["dates"]),
                   "report_delivery_verified": observation["report_delivery_verified"],
                   "source_status": "published_snapshot; historical_vintage_and_executable_quotes_unverified",
                   "pool_admission": False, "reused_existing": existing, "report": str(root / "report.html")}
        shutil.copyfile(raw_path, root / "engine-result.json")
        _json(root / "summary.json", summary)
        from .case_report import render
        render(root, protocol, provenance, raw, summary)
        _json(root / "delivery.json", {"run_id": job["run_id"], "artifacts": [_binding(root / name) for name in
              ("case-spec.json", "summary.json", "engine-result.json", "evidence.json", "report.html", "nav.svg")]})
        return summary
    finally:
        ledger.db.close()


def review_packet(output):
    """Read-only aggregate context from a completed hash-verified public case."""
    from panda_alpha.team.contracts import digest
    root = Path(output).resolve()
    delivery = _read(root / "delivery.json")
    for binding in delivery["artifacts"]:
        verify_artifact(binding)
    protocol, summary = _read(root / "protocol.json"), _read(root / "summary.json")
    manifest = _read(root / "case-spec.json")
    for binding in [manifest["protocol"], *manifest["inputs"].values()]:
        verify_artifact(binding)
    if protocol["case_id"] not in {"ecb-fx-momentum-v1", "ecb-fx-momentum-v2"} or not summary["report_delivery_verified"]:
        raise ValueError("Expected a completed public ECB case")
    raw = _read(root / "engine-result.json")
    context = {"protocol": protocol, "source": _read(root / "inputs/source.json"),
               "finding": summary["finding_zh"], "primary_excess_total_return": summary["primary_excess_total_return"],
               "paths": [{key: path[key] for key in ("name", "cost_bps", "metrics", "total_fees", "audit")}
                         for path in raw["paths"]]}
    if protocol["case_id"] == "ecb-fx-momentum-v1":
        context["known_execution_issue"] = "V1 fee payments permitted small borrowing despite a no-leverage statement; not the corrected current reference."
    return {"work_id": "public-case-review-" + digest(context), "role": "skeptic", "action": "source_analysis",
            "task_id": protocol["case_id"], "scope": "Standalone read-only public-case discussion; no live Host task",
            "role_instructions": "Explain the unsuccessful hypothesis and costs using all paths. Separate source/execution "
                "limits from returns. Propose an independent next test without changing this frozen protocol. "
                "You cannot qualify sources, authorize funds or admit a factor.",
            "funding_authorization": False, "context": context,
            "reply_contract": {"envelope": "{work_id: exact work_id, output: action-specific JSON}",
                "source_analysis": "claim/test, decision=analysis_only, resolved/pending/limitations string lists; no qualification changes"}}
