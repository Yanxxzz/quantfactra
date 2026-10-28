from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import socket
import subprocess

import pytest

from panda_alpha.registry import ResearchRegistry
from panda_alpha.team.budget import activate
from panda_alpha.team.contracts import canonical
from panda_alpha.team.coordinator import Coordinator
from panda_alpha.team.demo import (SyntheticClient, artifact, blocked, freeze_synthetic, send,
    synthetic_assessment, synthetic_config, synthetic_grant, synthetic_request)
from panda_alpha.team.evidence import inspect_report, read_artifact, validate_assessment
from panda_alpha.team.host import CommandAgents, Host, MailboxAgents
from panda_alpha.team.rehearsal import run_rehearsal


@pytest.fixture
def lifecycle(tmp_path, monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(subprocess, "Popen", blocked)
    proof = tmp_path / "proof.txt"
    proof.write_text("Synthetic protocol proof", encoding="utf-8")
    c, registry = Coordinator(tmp_path / "team.sqlite3"), ResearchRegistry(tmp_path / "registry.sqlite3")
    try:
        yield c, registry, artifact(proof), tmp_path
    finally:
        c.close()
        registry.close()


def settled(env, request=None):
    c, registry, proof, root = env
    task = freeze_synthetic(c.board, request or synthetic_request("market", proof))
    config = synthetic_config()
    activate(c.board, synthetic_grant(proof, config))
    client = SyntheticClient()
    c.dispatch(task["task_id"], config, client, registry, execute=True)
    c.resume(task["task_id"], client, root / "results")
    return c.board.task(task["task_id"]), c.board.events(task["task_id"])[-1]


def result_message(task, result, *, event_id="review", assessment=None):
    return {"event_id": event_id, "task_id": task["task_id"], "actor": "skeptic", "kind": "result_review",
            "version": task["version"], "request_hash": task["request_hash"], "reply_to": result["event_id"],
            "claim": "Evidence limited to delivered fixture", "test": "Review actual delivery and remaining qualifications",
            "decision": "confirm", "artifacts": result["payload"]["artifacts"],
            "evidence_contract": result["payload"]["evidence_contract"],
            "assessment": assessment or synthetic_assessment()}


def close(env, task, result):
    c, registry, _, _ = env
    task = c.board.message(result_message(task, result))
    return c.conclude(task["task_id"], task["version"], {"operation_id": "close", "decision": "confirm",
        "reason": "Only capability; preserve source_pending", "review_event_id": "review"}, registry)


def reopen(env, task, result):
    c, _, _, root = env
    proof = root / "new-proof.txt"
    proof.write_text("New partial local evidence; no native qualification", encoding="utf-8")
    return {"operation_id": "repair", "request_hash": task["request_hash"], "prior_decision_event_id": "close",
            "target_event_id": result["event_id"], "claim_delta": "Local receipt improved; native clock still pending",
            "artifacts": [artifact(proof)]}


def spec(**kw):
    return dict({"objective": "Offline lifecycle test", "max_rounds": 3, "max_actions": 20,
        "deadline": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}, **kw)


def test_three_rounds_recover_both_durable_boundaries(tmp_path):
    report = run_rehearsal(tmp_path / "rehearsal")
    assert report["rounds"] == 3 and report["same_identity_reopens"] == 1
    assert len(report["interruptions_recovered"]) == 2
    assert report["synthetic_creates"] == report["synthetic_starts"] == report["registered_definitions"] == 4
    assert report["cross_line_reviews"] == 6 and report["cross_line_stops"] == 2
    assert report["official_runs"] == report["official_credits"] == report["network_calls"] == 0


def test_default_host_waits_before_any_platform_access(lifecycle):
    c, registry, proof, _ = lifecycle
    task = freeze_synthetic(c.board, synthetic_request("market", proof))
    host = Host(c, registry, "free", spec(seed_tasks=[task["task_id"]]))
    assert host.run(lambda p: pytest.fail("No role work needed"))["reason"] == "execution_not_enabled"
    assert c.ledger.jobs() == [] and registry.total == 0
    with pytest.raises(ValueError, match="immutable"):
        Host(c, registry, "free", spec(max_rounds=100))


@pytest.mark.parametrize("missing", ["candidate", "mechanism_id"])
def test_malformed_proposal_reports_contract_before_scientific_evaluation(lifecycle, missing):
    c, registry, proof, _ = lifecycle
    host = Host(c, registry, "malformed-proposal", spec(workflow="lean", researcher="market", max_model_attempts=4))
    packet = host.next()["packet"]
    request = synthetic_request("market", proof)
    request["task_id"] = packet["task_id"]
    request.pop(missing)
    with pytest.raises(ValueError, match="Nonempty"):
        host.respond(packet["work_id"], {"work_id": packet["work_id"], "output": {"request": request}})
    assert c.board.tasks() == [] and registry.total == 0 and c.ledger.jobs() == []
    assert c.board.db.execute("SELECT state FROM team_host_work WHERE work_id=?", (packet["work_id"],)).fetchone()[0] == "rejected"


def test_host_delivery_restart_idempotency_invalid_reply_and_bounds(lifecycle):
    c, registry, _, _ = lifecycle
    host = Host(c, registry, "bounded", spec(max_actions=2))
    packet = host.next()["packet"]
    assert Host(c, registry, "bounded").next()["packet"] == packet
    bad = {"work_id": packet["work_id"], "output": {"decision": "research", "reason": "test", "assignments": []}}
    with pytest.raises(ValueError, match="distinct"):
        host.respond(packet["work_id"], bad)
    corrected = host.next()["packet"]
    assert corrected["work_id"] != packet["work_id"] and "distinct" in corrected["previous_error"]
    response = {"work_id": corrected["work_id"], "output": {"decision": "stop", "reason": "No informative next step"}}
    host.respond(corrected["work_id"], response)
    host.respond(corrected["work_id"], response)
    assert host.next()["reason"] == "No informative next step"
    with pytest.raises(ValueError, match="different content"):
        host.respond(corrected["work_id"], dict(response, output={"decision": "stop", "reason": "changed"}))


def test_host_expiry_and_action_limit_do_not_create_new_work(lifecycle):
    c, registry, _, _ = lifecycle
    expired = Host(c, registry, "expired", spec(deadline=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()))
    assert expired.next()["reason"] == "deadline"
    bounded = Host(c, registry, "one", spec(max_actions=1))
    packet = bounded.next()["packet"]
    bounded.respond(packet["work_id"], {"work_id": packet["work_id"], "output": {"decision": "research", "reason": "fixture",
        "assignments": [{"owner": r, "brief": r} for r in ("market", "fundamental")]}})
    assert bounded.next()["reason"] == "action_limit"
    assert c.ledger.jobs() == []


@pytest.mark.parametrize("assignments", [["market", "fundamental"], {"market": "brief"}])
def test_malformed_nested_role_reply_gets_a_correctable_delivery(lifecycle, assignments):
    c, registry, _, _ = lifecycle
    host = Host(c, registry, "malformed", spec())
    packet = host.next()["packet"]
    with pytest.raises(ValueError, match="brief objects"):
        host.respond(packet["work_id"], {"work_id": packet["work_id"], "output": {
            "decision": "research", "reason": "test", "assignments": assignments}})
    fresh = Host(c, registry, "malformed").next()["packet"]
    assert fresh["work_id"] != packet["work_id"] and "brief objects" in fresh["previous_error"]
    host.respond(fresh["work_id"], {"work_id": fresh["work_id"], "output": {"decision": "stop", "reason": "corrected"}})
    assert host.next()["status"] == "stopped"


def test_host_run_returns_invalid_reply_to_role_without_manual_json_repair(lifecycle):
    c, registry, _, _ = lifecycle
    host = Host(c, registry, "self-correcting", spec(max_actions=3))
    seen = []
    def roles(packet):
        seen.append(packet)
        output = ({"decision": "research", "reason": "bad nested shape", "assignments": ["market", "fundamental"]}
                  if len(seen) == 1 else {"decision": "stop", "reason": "Role corrected its answer"})
        return {"work_id": packet["work_id"], "output": output}
    assert host.run(roles)["reason"] == "Role corrected its answer"
    assert len(seen) == 2 and seen[0]["work_id"] != seen[1]["work_id"] and "brief objects" in seen[1]["previous_error"]


def test_inflight_recovery_preempts_a_previously_issued_dispatch(lifecycle):
    c, registry, proof, root = lifecycle
    first = freeze_synthetic(c.board, synthetic_request("fundamental", proof))
    second = freeze_synthetic(c.board, synthetic_request("market", proof))
    config = synthetic_config()
    activate(c.board, synthetic_grant(proof, config))
    host = Host(c, registry, "resume-first", spec(seed_tasks=[first["task_id"], second["task_id"]]))
    pending = host.next(execution=True)["packet"]
    assert pending["task_id"] == first["task_id"] and pending["action"] == "dispatch"
    client = SyntheticClient()
    c.dispatch(second["task_id"], config, client, registry, execute=True)
    packet = Host(c, registry, "resume-first").next(execution=True)["packet"]
    assert packet["action"] == "resume" and packet["task_id"] == second["task_id"]
    assert client.creates == client.starts == 1
    c.resume(second["task_id"], client, root / "results")
    assert host.next(execution=True)["packet"]["action"] == "result_review"


def test_host_surfaces_unknown_remote_identity_without_retry(lifecycle):
    c, registry, proof, _ = lifecycle
    task = freeze_synthetic(c.board, synthetic_request("market", proof))
    config = synthetic_config()
    activate(c.board, synthetic_grant(proof, config))
    class LostCreate(SyntheticClient):
        def create(self, *args):
            self.creates += 1
            raise TimeoutError("create response lost")
    client = LostCreate()
    from panda_alpha.team.host import ExistingExecutor
    host = Host(c, registry, "ambiguous", spec(seed_tasks=[task["task_id"]]))
    executor = ExistingExecutor(c, registry, client, config, lifecycle[3] / "results")
    assert host.run(lambda p: pytest.fail("not role work"), executor)["reason"] == "execution_blocked"
    assert host.run(lambda p: pytest.fail("not role work"), executor)["reason"] == "operator_reconciliation"
    assert client.creates == 1 and client.starts == 0


def test_ready_slot_is_freed_before_another_same_line_freeze(lifecycle):
    c, registry, proof, _ = lifecycle
    first = freeze_synthetic(c.board, synthetic_request("market", proof))
    request = synthetic_request("market", proof)
    request["task_id"] = "second-market"
    request["candidate"]["direction"] = 0
    c.board.submit(request)
    send(c.board, request["task_id"], "skeptic", "challenge")
    send(c.board, request["task_id"], "market", "response", reply_to=c.board.events(request["task_id"])[-1]["event_id"])
    send(c.board, request["task_id"], "skeptic", "review", decision="approve", reply_to=c.board.events(request["task_id"])[-1]["event_id"])
    host = Host(c, registry, "one-ready", spec(seed_tasks=[first["task_id"], request["task_id"]]))
    assert host.next()["reason"] == "execution_not_enabled"
    packet = host.next(execution=True)["packet"]
    assert packet["action"] == "dispatch" and packet["task_id"] == first["task_id"]


def test_same_identity_source_review_preserves_usage_and_pending(lifecycle):
    c, registry, _, _ = lifecycle
    task, result = settled(lifecycle)
    task = close(lifecycle, task, result)
    jobs, total = c.ledger.jobs(), registry.total
    request = reopen(lifecycle, task, result)
    version = task["version"]
    fresh = c.board.reopen_evidence(task["task_id"], version, request)
    assert c.board.reopen_evidence(task["task_id"], version, request)["version"] == fresh["version"]
    assert fresh["request_hash"] == task["request_hash"] and fresh["fingerprint"] == task["fingerprint"]
    message = result_message(fresh, result, event_id="review-again")
    message["decision"] = "source_pending"
    message["assessment"]["pending"] = ["native_input_and_clock"]  # Omitted old support remains pending in science memory.
    with pytest.raises(ValueError, match="new artifacts"):
        c.board.message(message)
    message["artifacts"] = message["artifacts"] + request["artifacts"]
    reviewed = c.board.message(message)
    decision = {"operation_id": "close-again", "review_event_id": message["event_id"], "decision": "source_pending", "reason": "Clock remains unverified"}
    final = c.conclude(task["task_id"], reviewed["version"], decision, registry)
    assert final["state"] == "parked" and c.ledger.jobs() == jobs and registry.total == total
    pending = registry.reconciled_memory()["registry_constraints"]["source_pending"][0]
    assert set(pending["pending"]) == {"native_input_and_clock", "native_eligible_support", "worker_deployment_binding"}
    assert c.conclude(task["task_id"], reviewed["version"], decision, registry)["version"] == final["version"]
    duplicate = dict(request, operation_id="renamed", prior_decision_event_id="close-again")
    with pytest.raises(ValueError, match="materially new"):
        c.board.reopen_evidence(task["task_id"], final["version"], duplicate)


def test_evidence_reopen_rejects_changed_identity_stale_parent_and_old_versions(lifecycle):
    c, _, _, _ = lifecycle
    task, result = settled(lifecycle)
    task = close(lifecycle, task, result)
    request = reopen(lifecycle, task, result)
    for change, error in [({"request_hash": "changed"}, "original request"),
                          ({"prior_decision_event_id": "unknown"}, "latest closure"),
                          ({"target_event_id": "review"}, "original result")]:
        with pytest.raises(ValueError, match=error):
            c.board.reopen_evidence(task["task_id"], task["version"], dict(request, **change))
    with pytest.raises(ValueError, match="Stale"):
        c.board.reopen_evidence(task["task_id"], task["version"] - 1, request)


@pytest.mark.parametrize("change,error", [
    ({"pool_admission": True}, "admission"),
    ({"source_status": "verified"}, "unresolved source"),
    ({"economic_status": "rejected"}, "Economic conclusions"),
    ({"scope": "source", "source_status": "verified", "pending": []}, "explicitly resolve"),
])
def test_report_success_never_silently_promotes_qualifications(lifecycle, change, error):
    c, _, _, _ = lifecycle
    task, result = settled(lifecycle)
    message = result_message(task, result)
    message["assessment"].update(change)
    with pytest.raises(ValueError, match=error):
        c.board.message(message)
    assert c.board.task(task["task_id"])["state"] == "result_ready"


def test_frozen_source_requirement_cannot_disappear_from_first_review(lifecycle):
    c, _, proof, _ = lifecycle
    request = synthetic_request("market", proof)
    request["source_requirements"] = ["native_financial_period_pending"]
    task, result = settled(lifecycle, request)
    message = result_message(task, result)
    message["assessment"].update(scope="source", source_status="verified", pending=[],
        resolved=["native_input_and_clock", "native_eligible_support", "worker_deployment_binding"])
    with pytest.raises(ValueError, match="frozen and observed"):
        c.board.message(message)


def test_cross_line_brief_and_required_lead_disposition(lifecycle):
    c, registry, proof, _ = lifecycle
    first = synthetic_request("market", proof)
    c.board.submit(first)
    peer = c.board.submit(synthetic_request("fundamental", proof))
    task_id = first["task_id"]
    send(c.board, task_id, "skeptic", "challenge")
    challenge = c.board.events(task_id)[-1]["event_id"]
    send(c.board, task_id, "market", "response", reply_to=challenge)
    response = c.board.events(task_id)[-1]["event_id"]
    task = send(c.board, task_id, "skeptic", "review", reply_to=response, decision="approve")
    brief = c.board.packet("fundamental")["other_line"][0]
    assert brief["hypothesis"] and brief["availability_clock"] and brief["risk_exposures"]
    task = c.board.message({"event_id": "cross", "task_id": task_id, "version": task["version"], "actor": "fundamental",
        "kind": "cross_review", "request_hash": task["request_hash"], "claim": "Shared size risk", "test": "Matched size control",
        "decision": "retain", "artifacts": [], "peer_task_id": peer["task_id"], "peer_request_hash": peer["request_hash"]})
    decision = {"request_hash": task["request_hash"], "priority": 50, "allocation_reason": "fixture", "diversity_check": "fixture"}
    with pytest.raises(ValueError, match="cross-line"):
        c.board.freeze(task_id, task["version"], decision)
    # Later notes must not hide a still-required control from the lead. Use
    # only the delivered packet to build the response the validator accepts.
    for _ in range(12):
        send(c.board, task_id, "lead", "note")
    host = Host(c, registry, "aged-cross-control", spec(seed_tasks=[task_id]))
    packet = host.next()["packet"]
    assert packet["action"] == "freeze"
    assert all(e["event_id"] != "cross" for e in packet["task"]["events"])
    decision["cross_line_decisions"] = [{"event_id": e["event_id"], "decision": "accept",
        "reason": "Retain the fixed size control"} for e in packet["cross_line_reviews"]]
    host.respond(packet["work_id"], {"work_id": packet["work_id"], "output": decision})
    assert c.board.task(task_id)["state"] == "ready"


def test_cross_review_does_not_depend_on_matching_free_text_risk_labels(lifecycle):
    c, registry, proof, _ = lifecycle
    request = synthetic_request("market", proof)
    request["risk_exposures"] = ["size_and_industry"]
    task = c.board.submit(request)
    peer_request = synthetic_request("fundamental", proof)
    peer_request["risk_exposures"] = ["capital_intensity"]
    peer = c.board.submit(peer_request)
    send(c.board, task["task_id"], "skeptic", "challenge")
    send(c.board, task["task_id"], "market", "response", reply_to=c.board.events(task["task_id"])[-1]["event_id"])
    send(c.board, task["task_id"], "skeptic", "review", decision="approve", reply_to=c.board.events(task["task_id"])[-1]["event_id"])
    host = Host(c, registry, "distinct-risk-cross-review", spec(seed_tasks=[task["task_id"]]))
    packet = host.next()["packet"]
    assert packet["action"] == "cross_review" and packet["role"] == "fundamental"
    assert packet["peer_task_id"] == peer["task_id"]
    assert packet["peer_request_hash"] == peer["request_hash"]
    assert c.board.task(task["task_id"])["request"] == request


def test_result_packet_contains_actual_provider_metrics_without_certifying_them(lifecycle):
    c, _, proof, root = lifecycle
    task = c.board.submit(synthetic_request("market", proof))
    analysis = {"query_factor_analysis_data": [{"indicator": "Rank_IC", "factor1": "0.04"}],
                "query_group_return_analysis": [{"group": "分组10", "sharpeRatio": "1.2", "excessAnnualized": "8.0%"}],
                "query_rank_ic_sequence_chart": {"x": [{"data": ["2020-01-02"]}],
                    "y": [{"name": "Rank_IC", "data": [.04]}, {"name": "Cum_Rank_IC", "data": [.04]}]}}
    report = root / "captured-run.json"
    report.write_text(json.dumps({"success": True, "status": 2, "factor_analysis": analysis}), encoding="utf-8")
    observation = inspect_report(task, {"run_id": "captured-run", "environment": "official", "artifacts": [artifact(report)]})
    assert observation["reported_metrics"]["factor_summary"] == analysis["query_factor_analysis_data"]
    assert observation["reported_metrics"]["group_returns"] == analysis["query_group_return_analysis"]
    assert observation["reported_metrics"]["rank_ic_chart"] == analysis["query_rank_ic_sequence_chart"]
    assert not observation["report_delivery_verified"]
    assert observation["source_status"] == "pending" and not observation["pool_admission"]


def test_legacy_v1_observation_reconstructs_without_mutating_frozen_evidence(lifecycle):
    task, result = settled(lifecycle)
    old = read_artifact(result["payload"]["evidence_contract"])
    old["schema"] = "team-result-evidence-v1"
    old.pop("reported_metrics")
    path = lifecycle[3] / "legacy-observation.json"
    path.write_text(json.dumps(old), encoding="utf-8")
    binding = artifact(path)
    message = result_message(task, result)
    message["evidence_contract"] = binding
    message["artifacts"] = message["artifacts"] + [binding]
    before = path.read_bytes()
    validate_assessment(message, task, result, expected_environment="synthetic")
    assert path.read_bytes() == before
    old["observed"]["group_count"] = 999
    path.write_text(json.dumps(old), encoding="utf-8")
    message["evidence_contract"] = artifact(path)
    message["artifacts"].append(message["evidence_contract"])
    with pytest.raises(ValueError, match="observations changed"):
        validate_assessment(message, task, result, expected_environment="synthetic")


def test_completed_design_reopen_does_not_hijack_first_run_result_review(lifecycle):
    c, registry, proof, root = lifecycle
    request = synthetic_request("market", proof)
    task_id = c.board.submit(request)["task_id"]
    send(c.board, task_id, "skeptic", "challenge")
    challenge = c.board.events(task_id)[-1]["event_id"]
    send(c.board, task_id, "market", "response", reply_to=challenge)
    response = c.board.events(task_id)[-1]["event_id"]
    task = send(c.board, task_id, "skeptic", "review", reply_to=response, decision="park")
    parked = c.board.events(task_id)[-1]["event_id"]
    new_design = root / "new-design.txt"
    new_design.write_text("New design evidence, not a result artifact", encoding="utf-8")
    new_proof = artifact(new_design)
    task = c.board.reopen_evidence(task_id, task["version"], {
        "operation_id": "design-source-reopen", "request_hash": task["request_hash"],
        "prior_decision_event_id": parked, "target_event_id": response,
        "claim_delta": "Bound new design evidence", "artifacts": [new_proof]})
    with pytest.raises(ValueError, match="original target and new artifacts"):
        send(c.board, task_id, "skeptic", "review", reply_to=response, decision="approve")
    task = send(c.board, task_id, "skeptic", "review", reply_to=response, decision="approve", artifacts=[new_proof])
    task = c.board.freeze(task_id, task["version"], {"request_hash": task["request_hash"],
        "priority": 50, "allocation_reason": "fixture", "diversity_check": "fixture"})
    config = synthetic_config()
    activate(c.board, synthetic_grant(proof, config))
    client = SyntheticClient()
    c.dispatch(task_id, config, client, registry, execute=True)
    c.resume(task_id, client, root / "first-run-results")
    task = c.board.task(task_id)
    result = c.board.events(task_id)[-1]
    message = result_message(task, result)
    assert new_proof not in message["artifacts"]
    assert message["reply_to"] != response
    assert c.board.message(message)["state"] == "result_reviewed"


def native_report_fixture(env):
    c, _, proof, root = env
    task = c.board.submit(synthetic_request("market", proof))
    dates = ["2024-06-18 00:00:00", "2024-06-26 00:00:00"]
    chart = lambda rows: {"x": [{"data": dates}], "y": rows}
    report = {"success": True, "status": 2, "factor_analysis": {"query_return_chart": chart([{"name": f"组{i}", "data": [0., .01]} for i in range(1, 11)]),
        "query_group_return_analysis": [{"group": f"分组{i}", "annualizedReturn": "1%", "excessAnnualized": "0%",
            "maxDrawdown": "2%", "turnoverRate": "5%", "sharpeRatio": "0.2"} for i in range(1, 11)],
        "query_ic_sequence_chart": chart([{"name": "IC", "data": [.01, -.02]}]),
        "query_rank_ic_sequence_chart": chart([{"name": "RankIC", "data": [.02, -.01]}])}}
    path, logs = root / "owned-run.json", root / "owned-run.logs.json"
    path.write_text(canonical(report), encoding="utf-8")
    logs.write_text(canonical({"run_id": "owned-run", "complete": True, "errors": []}), encoding="utf-8")
    payload = {"run_id": "owned-run", "artifacts": [artifact(path), artifact(logs)], "environment": "official"}
    return task, report, path, logs, payload


def test_cached_native_report_adapter_requires_matching_dates_and_logs(lifecycle):
    task, report, path, logs, payload = native_report_fixture(lifecycle)
    dates = report["factor_analysis"]["query_return_chart"]["x"][0]["data"]
    contract = inspect_report(task, payload)
    assert contract["report_delivery_verified"] and contract["source_status"] == "pending"
    assert contract["observed"]["group_dates"] == dates and contract["pool_admission"] is False
    for mutation in ("failed", "not_date", "outside_window", "missing_table"):
        broken = deepcopy(report)
        if mutation == "failed":
            broken.update(success=False, status=3)
        elif mutation == "missing_table":
            broken["factor_analysis"].pop("query_group_return_analysis")
        else:
            bad_dates = ["not-a-date-1", "not-a-date-2"] if mutation == "not_date" else ["2015-01-01", "2015-01-08"]
            for key in ("query_return_chart", "query_ic_sequence_chart", "query_rank_ic_sequence_chart"):
                broken["factor_analysis"][key]["x"] = [{"data": bad_dates}]
        path.write_text(canonical(broken), encoding="utf-8")
        changed = dict(payload, artifacts=[artifact(path), artifact(logs)])
        assert not inspect_report(task, changed)["report_delivery_verified"], mutation
    path.write_text(canonical(report), encoding="utf-8")
    report["factor_analysis"]["query_ic_sequence_chart"]["x"] = [{"data": dates[::-1]}]
    path.write_text(canonical(report), encoding="utf-8")
    with pytest.raises(ValueError, match="SHA256"):
        inspect_report(task, payload)
    payload["artifacts"][0] = artifact(path)
    assert not inspect_report(task, payload)["report_delivery_verified"]
    # Execution environment comes from the stored permit, not a report claiming
    # that synthetic=false. Native-looking fixture data stays synthetic.
    assert inspect_report(task, dict(payload, environment="synthetic"))["synthetic"]


def test_explicit_supplemental_contract_can_accept_later_complete_logs(lifecycle):
    task, _, _, _, payload = native_report_fixture(lifecycle)
    root = lifecycle[3]
    original = dict(payload, fingerprint=task["fingerprint"], artifacts=payload["artifacts"][:1])
    manifest, old_path, new_path = [root / n for n in ("manifest.json", "old.json", "supplemented.json")]
    manifest.write_text(canonical(original), encoding="utf-8")
    old_path.write_text(canonical(inspect_report(task, original)), encoding="utf-8")
    new_path.write_text(canonical(inspect_report(task, payload)), encoding="utf-8")
    result = {"event_id": "original-result", "payload": dict(original,
        evidence_contract=artifact(old_path), artifacts=[artifact(manifest), *original["artifacts"], artifact(old_path)])}
    message = result_message(task, result)
    with pytest.raises(ValueError, match="delivered reports"):
        validate_assessment(message, task, result)
    message.update(evidence_contract=artifact(new_path), artifacts=message["artifacts"] + payload["artifacts"][1:] + [artifact(new_path)])
    assert validate_assessment(message, task, result)["scope"] == "capability"
    assert not read_artifact(artifact(old_path))["report_delivery_verified"]
    assert read_artifact(artifact(new_path))["report_delivery_verified"]
    with pytest.raises(ValueError, match="persisted execution"):
        validate_assessment(message, task, result, expected_environment="synthetic")


def test_supplemental_contract_survives_conclusion_recovery_without_replacing_original(lifecycle, monkeypatch):
    c, registry, _, root = lifecycle
    task, result = settled(lifecycle)
    task = close(lifecycle, task, result)
    original_binding = result["payload"]["evidence_contract"]
    original = read_artifact(original_binding)
    request = reopen(lifecycle, task, result)
    fresh = c.board.reopen_evidence(task["task_id"], task["version"], request)
    binding = c.evidence(task["task_id"], root / "supplemented.json", request["artifacts"])
    message = result_message(fresh, result, event_id="supplemented-review")
    message.update(evidence_contract=binding, artifacts=message["artifacts"] + request["artifacts"] + [binding], decision="source_pending")
    reviewed = c.board.message(message)
    from panda_alpha.team import conclusions
    with monkeypatch.context() as guard:
        def interrupt(*args):
            raise RuntimeError("interrupt after registry commit")
        guard.setattr(conclusions, "_commit_board", interrupt)
        with pytest.raises(RuntimeError, match="interrupt"):
            c.conclude(task["task_id"], reviewed["version"], {"operation_id": "supplemented-close", "review_event_id": message["event_id"],
                "decision": "source_pending", "reason": "Keep source limitations"}, registry)
    c.recover_conclusions(registry)
    evaluation = [e for e in registry.events() if e["kind"] == "evaluation"][-1]
    assert evaluation["payload"]["evidence_contract"] == binding
    assert read_artifact(original_binding) == original
    assert registry.total == 1 and len(c.ledger.jobs()) == 1


@pytest.mark.parametrize("economic_status", ["rejected", "supported"])
def test_conclusion_repairs_source_and_returns_scoped_economics_to_next_round(lifecycle, economic_status):
    # Exercise the native-shaped branch with an in-process fake client. Socket
    # and subprocess guards remain active; these assertions are not PIT proof.
    c, registry, proof, root = lifecycle
    _, report, _, _, _ = native_report_fixture(lifecycle)
    request = synthetic_request("fundamental", proof)
    task = freeze_synthetic(c.board, request)
    config = synthetic_config()
    grant = dict(synthetic_grant(proof, config), environment="official")
    activate(c.board, grant)
    class NativeShapedFake(SyntheticClient):
        simulation = False
        def result(self, run_id):
            return dict(report, run_id=run_id)
    client = NativeShapedFake()
    c.dispatch(task["task_id"], config, client, registry, execute=True)
    c.resume(task["task_id"], client, root / "native-shaped-results")
    task = c.board.task(task["task_id"])
    result = c.board.events(task["task_id"])[-1]
    task = close(lifecycle, task, result)
    assert registry.reconciled_memory()["registry_constraints"]["source_pending"]
    repair = reopen(lifecycle, task, result)
    fresh = c.board.reopen_evidence(task["task_id"], task["version"], repair)
    message = result_message(fresh, result, event_id="economic-fixture-review")
    message.update(decision="reject" if economic_status == "rejected" else "confirm", artifacts=message["artifacts"] + repair["artifacts"])
    message["assessment"].update(scope="economic", source_status="verified", economic_status=economic_status, pending=[],
        resolved=["native_input_and_clock", "native_eligible_support", "worker_deployment_binding"],
        source_evidence=repair["artifacts"], source_verification="Isolated fixture exercises typed source-review branch only",
        economic_verification="Isolated fixture exercises a predeclared criterion and scoped verdict; no investment conclusion")
    state = c.board.task_packet(task["task_id"])["qualifications"]
    message["qualification_changes"] = {"base_head": state["head"], "replacements": [{
        "supersedes": [{"id": row["id"], "revision": row["revision"]}],
        "items": [{"id": row["id"], "text": row["text"], "status": "resolved"}],
        "reason": "Native-shaped test evidence; not real PIT verification", "evidence": repair["artifacts"]}
        for row in state["items"] if row["status"] == "pending"]}
    reviewed = c.board.message(message)
    c.conclude(task["task_id"], reviewed["version"], {"operation_id": "economic-fixture-close",
        "review_event_id": message["event_id"], "decision": "close" if economic_status == "rejected" else "confirm", "reason": "Record explicit scoped fixture judgement"}, registry)
    memory = registry.reconciled_memory()
    assert not memory["registry_constraints"]["source_pending"]
    assert len(memory["registry_constraints"]["fixed_rejected"]) == (1 if economic_status == "rejected" else 0)
    assert memory["recent_evaluations"][-1]["assessment"]["economic_status"] == economic_status
    assert sum(e["kind"] == "source_repair" for e in registry.events()) == 1
    host = Host(c, registry, "memory-next", spec())
    plan = host.next()["packet"]
    host.respond(plan["work_id"], {"work_id": plan["work_id"], "output": {"decision": "research", "reason": "Memory regression",
        "assignments": [{"owner": r, "brief": r} for r in ("fundamental", "market")]}})
    packet = host.next()["packet"]
    assert packet["memory"]["recent_evaluations"][-1]["assessment"]["economic_status"] == economic_status
    renamed = deepcopy(request)
    renamed["task_id"] = renamed["candidate"]["candidate_id"] = packet["task_id"]
    if economic_status == "rejected":
        with pytest.raises(ValueError, match="Scientific memory blocks"):
            host.respond(packet["work_id"], {"work_id": packet["work_id"], "output": {"request": renamed}})
    else:
        # Support does not authorize a renamed duplicate or create a new OOS.
        with pytest.raises(ValueError, match="Duplicate definition"):
            host.respond(packet["work_id"], {"work_id": packet["work_id"], "output": {"request": renamed}})
    assert client.starts == registry.total == 1 and len(c.ledger.jobs()) == 1


def test_explicit_command_transport_caches_bound_replies(tmp_path, monkeypatch):
    calls = []
    def invoke(argv, **kw):
        packet = json.loads(kw["input"])
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, canonical({"work_id": packet["work_id"], "output": {"decision": "stop"}}))
    monkeypatch.setattr(subprocess, "run", invoke)
    backend = CommandAgents({r: ["trusted-host", r] for r in ("lead", "market", "fundamental", "skeptic")}, tmp_path)
    packet = {"work_id": "immutable-work", "role": "skeptic"}
    assert backend(packet) == backend(packet) and calls == [["trusted-host", "skeptic"]]


def test_mailbox_emits_real_work_and_waits_without_fabricating_agent_output(tmp_path):
    backend = MailboxAgents(tmp_path)
    packet = {"role": "market", "work_id": "work", "objective": "Read the actual source contract"}
    assert backend(packet) is None
    request = tmp_path / "market/requests/work.json"
    assert json.loads(request.read_text(encoding="utf-8")) == packet
    response = {"work_id": "work", "output": {"decision": "no_candidate", "reason": "Source remains unresolved"}}
    (tmp_path / "market/responses/work.json").write_text(canonical(response), encoding="utf-8")
    assert backend(packet) == response
    with pytest.raises(ValueError, match="different content"):
        backend(dict(packet, objective="changed"))
