"""Native adapters must work without PandaAI wallets, status codes or reports."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import socket

import pytest

from panda_alpha.backend_contract import budget_plan, reference_id
from panda_alpha.backends import create_backend
from panda_alpha.platform import ExperimentLedger, beijing_day, dispatch, fingerprint, resume
from panda_alpha.team.evidence import artifact, inspect_report
from quantfactra.demo import run_demo


C = {"candidate_id": "native-fixture", "formula": "RANK(close)", "direction": 1}
W = {"start": "2025-01-02", "end": "2025-01-31"}


@pytest.fixture
def native(tmp_path):
    platform = {"backend": "quantfactra.adapters.demo:build_client", "contract": "quantfactra-v1",
                "connection_id": "test-native", "cost_unit": "credits", "state_dir": str(tmp_path / "adapter")}
    config = {"platform": platform, "research": {"cycle": 5, "groups": 2},
              "compute": {"billing_mode": "free", "planning_credit_estimate": 1,
                          "daily_credit_limit": 10, "recharge_credit_limit": 0}}
    ledger = ExperimentLedger(tmp_path / "jobs.sqlite3")
    yield config, create_backend(platform), ledger, tmp_path
    ledger.db.close()


def test_free_native_lifecycle_can_restart_without_a_wallet(native):
    config, client, ledger, root = native
    job = dispatch(C, W, config, ledger, client)
    definition = json.loads(job["definition"])
    assert definition["native_preflight"]["proof"]["input_fields"] == ["close"]
    assert json.loads(job["balance_before"]) == {
        "schema": "quantfactra-balance-v1", "available": None, "unit": "credits", "billing_mode": "free"}
    assert job["reserved"] == 1 and job["actual"] is None
    restored = create_backend(config["platform"])
    done = resume(job["fingerprint"], ledger, restored, root / "results")
    assert done["state"] == "SETTLED" and done["actual"] == 0
    assert "wallet_balances" not in json.loads(done["receipt"])
    assert json.loads((root / "results" / (job["run_id"] + ".json")).read_text())["schema"] == "quantfactra-report-v1"
    assert dispatch(C, W, config, ledger, restored) == done


def test_running_native_run_does_not_read_results_or_billing(native, monkeypatch):
    config, client, ledger, root = native
    job = dispatch(C, W, config, ledger, client)
    monkeypatch.setattr(client._backend, "status", lambda run: "running")
    def forbidden(*args):
        raise AssertionError("Only the existing status may be read")
    monkeypatch.setattr(client._backend, "result", forbidden)
    monkeypatch.setattr(client._backend, "billing", forbidden)
    assert resume(job["fingerprint"], ledger, client, root / "results")["platform_status"] == "running"
    assert ledger.get(job["fingerprint"]) == job


def test_unknown_bill_retains_full_reservation(native, monkeypatch):
    config, client, ledger, root = native
    job = dispatch(C, W, config, ledger, client)
    monkeypatch.setattr(client._backend, "billing", lambda run: None)
    assert resume(job["fingerprint"], ledger, client, root / "results")["state"] == "BILLING_PENDING"
    assert ledger.get(job["fingerprint"]) == job
    with pytest.raises(ValueError, match="budget slot|prior job"):
        dispatch(dict(C, formula="RANK(close)", direction=0), W, config, ledger, client)


@pytest.mark.parametrize("field,value", [
    ("unit", "USD"), ("run_id", "foreign-run"), ("status", "failed"),
    ("confirmed", False), ("backend_identity", {"backend": "foreign"}), ("actual", float("inf")),
])
def test_foreign_or_invalid_billing_cannot_release_reservation(native, monkeypatch, field, value):
    config, client, ledger, root = native
    job = dispatch(C, W, config, ledger, client)
    original = client._backend.billing
    def billing(run):
        binding = original(run)
        path = Path(binding["path"])
        bill = json.loads(path.read_text())
        bill[field] = value
        path.write_text(json.dumps(bill), encoding="utf-8")
        return artifact(path)
    monkeypatch.setattr(client._backend, "billing", billing)
    with pytest.raises(ValueError, match="billing receipt|actual charge"):
        resume(job["fingerprint"], ledger, client, root / "results")
    assert ledger.get(job["fingerprint"]) == job


def test_nonzero_free_bill_is_reviewed_and_not_settled(native, monkeypatch):
    config, client, ledger, root = native
    job = dispatch(C, W, config, ledger, client)
    original = client._backend.billing
    def billing(run):
        path = Path(original(run)["path"])
        bill = json.loads(path.read_text())
        bill["actual"] = 2
        path.write_text(json.dumps(bill), encoding="utf-8")
        return artifact(path)
    monkeypatch.setattr(client._backend, "billing", billing)
    result = resume(job["fingerprint"], ledger, client, root / "results")
    assert result["state"] == "BILLING_REVIEW" and result["receipt"]["actual"] == 2
    assert ledger.get(job["fingerprint"]) == job


def test_paid_native_backend_requires_explicit_authority(native, monkeypatch):
    config, client, ledger, root = native
    config["compute"]["billing_mode"] = "prepaid"
    monkeypatch.setattr(client._backend, "balance", lambda: {
        "schema": "quantfactra-balance-v1", "available": 100, "unit": "credits", "billing_mode": "prepaid"})
    with pytest.raises(ValueError, match="paid-credit allowance"):
        dispatch(C, W, config, ledger, client)
    assert ledger.jobs() == []


def test_paid_native_receipt_uses_original_batch_and_daily_limits(native, monkeypatch):
    config, client, ledger, root = native
    key = fingerprint(C, W, 5, 2)
    authorization = {"batch_id": "native-fixture", "day": beijing_day(), "fingerprints": [key],
                     "max_total_credits": 5, "max_recharge_credits": 5, "max_runs": 1,
                     "accept_estimated_charge_risk": True}
    config["compute"].update(billing_mode="prepaid", recharge_credit_limit=5,
        allow_estimated_billing=True, estimated_billing_batch_authorization=authorization,
        recharge_batch_authorization=authorization)
    monkeypatch.setattr(client._backend, "balance", lambda: {
        "schema": "quantfactra-balance-v1", "available": 100, "unit": "credits", "billing_mode": "prepaid"})
    original = client._backend.billing
    def billing(run):
        path = Path(original(run)["path"])
        bill = json.loads(path.read_text())
        bill["actual"] = 2
        path.write_text(json.dumps(bill), encoding="utf-8")
        return artifact(path)
    monkeypatch.setattr(client._backend, "billing", billing)
    job = dispatch(C, W, config, ledger, client)
    done = resume(key, ledger, client, root / "results")
    assert done["state"] == "SETTLED" and done["actual"] == 2
    assert json.loads(done["receipt"])["recharge_delta"] == 2
    assert json.loads(job["definition"])["billing_authorization"]["recharge"] == authorization


def test_failed_preflight_precedes_account_access(native, monkeypatch):
    config, client, ledger, root = native
    proof = root / "invalid-proof.json"
    proof.write_text(json.dumps({"schema": "quantfactra-preflight-v1", "passed": False,
                                "fingerprint": fingerprint(C, W, 5, 2), "fields": ["close"]}))
    monkeypatch.setattr(client._backend, "preflight", lambda *args: artifact(proof))
    monkeypatch.setattr(client._backend, "balance", lambda: pytest.fail("Account access preceded preflight"))
    with pytest.raises(ValueError, match="preflight"):
        dispatch(C, W, config, ledger, client)
    assert ledger.jobs() == []


def test_same_provider_cannot_switch_contract_on_resume(native):
    config, client, ledger, root = native
    job = dispatch(C, W, config, ledger, client)
    legacy = create_backend(dict(config["platform"], contract="pandaai-v1"))
    with pytest.raises(ValueError, match="persisted job"):
        resume(job["fingerprint"], ledger, legacy, root / "results")


@pytest.mark.parametrize("value", ["../run", "/absolute", "C:\\run", "", None])
def test_adapter_ids_cannot_escape_result_directory(value):
    with pytest.raises(ValueError, match="filename-safe"):
        reference_id(value)


def test_cost_units_are_not_silently_mixed(native):
    config, client, ledger, root = native
    dispatch(C, W, config, ledger, client)
    balance = {"schema": "quantfactra-balance-v1", "available": None, "unit": "USD", "billing_mode": "free"}
    with pytest.raises(ValueError, match="Different cost units"):
        budget_plan([C], config["compute"], balance, jobs=ledger.jobs())


def test_unknown_cancelled_bill_still_blocks_native_dispatch(native):
    config, client, ledger, root = native
    job = dispatch(C, W, config, ledger, client)
    ledger.transition(job["fingerprint"], "RUNNING", "CANCELLED")
    original = ledger.get(job["fingerprint"])
    with pytest.raises(ValueError, match="prior job|budget slot"):
        dispatch(dict(C, direction=0), W, config, ledger, client)
    assert ledger.get(job["fingerprint"]) == original
    assert original["actual"] is None and original["reserved"] == 1


def test_legacy_adapter_cannot_enter_a_ledger_with_native_currency(native):
    config, client, ledger, root = native
    definition = {"backend_contract": "quantfactra-v1", "cost_unit": "USD", "billing_mode": "free"}
    balance = {"schema": "quantfactra-balance-v1", "available": None, "unit": "USD", "billing_mode": "free"}
    ledger.reserve("usd-fixture", C, definition, "exploration", 1, 10, balance)
    ledger.transition("usd-fixture", "RESERVED", "SETTLED", actual=0)
    original = ledger.jobs()
    class Legacy:
        def balance(self):
            pytest.fail("Cost-unit mismatch must precede account access")
    with pytest.raises(ValueError, match="Different cost units"):
        dispatch(C, W, {"platform": {}, "research": config["research"], "compute": {}}, ledger, Legacy())
    with pytest.raises(ValueError, match="Different cost units"):
        ledger.reserve("legacy-direct", C, {}, "exploration", 1, 10, {"total": 100, "gift": 100})
    assert ledger.jobs() == original


def test_panda_numeric_status_is_not_interpreted_as_native_success(native, monkeypatch):
    config, client, ledger, root = native
    job = dispatch(C, W, config, ledger, client)
    monkeypatch.setattr(client._backend, "status", lambda run: 2)
    with pytest.raises(ValueError, match="Native adapter status"):
        resume(job["fingerprint"], ledger, client, root / "results")


def test_neutral_report_delivery_is_reconstructed_without_panda_fields(native):
    config, client, ledger, root = native
    job = dispatch(C, W, config, ledger, client)
    report = client.result(job["run_id"])
    report["series"] = {"dates": ["2025-01-02", "2025-01-03"],
        "groups": [{"group": 1, "values": [0, -.01]}, {"group": 2, "values": [0, .01]}],
        "ic": [.1, .2], "rank_ic": [.1, .2]}
    report["metrics"] = {"groups": [dict(group=i, annualized_return=.1, excess_annualized_return=.05,
        max_drawdown=.02, turnover=.2, sharpe=.5) for i in (1, 2)]}
    path = root / (job["run_id"] + ".json")
    path.write_text(json.dumps(report))
    logs = root / (job["run_id"] + ".logs.json")
    logs.write_text(json.dumps(client.logs(job["run_id"])))
    task = {"task_id": "native-report", "request_hash": "fixed-request", "fingerprint": job["fingerprint"],
            "request": {"candidate": C, "window": W, "cycle": 5, "groups": 2}}
    payload = {"run_id": job["run_id"], "environment": "synthetic", "backend_identity": report["backend_identity"],
               "artifacts": [artifact(path), artifact(logs)]}
    checked = inspect_report(task, payload)
    assert checked["report_delivery_verified"] is True
    assert checked["source_status"] == "pending" and checked["economic_status"] == "not_evaluated"
    assert checked["pool_admission"] is False and checked["synthetic"] is True
    assert "factor_analysis" not in report and checked == inspect_report(task, deepcopy(payload))
    changed = dict(payload, backend_identity=dict(report["backend_identity"], connection_id="foreign"))
    with pytest.raises(ValueError, match="persisted execution backend"):
        inspect_report(task, changed)


def test_first_run_is_offline_and_preserves_its_output_directory(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("The public first run must be offline")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    result = run_demo(tmp_path / "quickstart")
    assert result["state"] == "SETTLED" and result["actual"] == 0
    assert result["llm_calls"] == result["external_calls"] == 0
    assert Path(result["report"]).is_file()
    assert "QuantFactra" in Path(result["report"]).read_text(encoding="utf-8")
    with pytest.raises(FileExistsError):
        run_demo(tmp_path / "quickstart")
