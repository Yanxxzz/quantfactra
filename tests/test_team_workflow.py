from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import socket
import sqlite3
import subprocess
import tomllib

import pytest

from panda_alpha.platform import PandaClient, dispatch
from panda_alpha.registry import ResearchRegistry
from panda_alpha.team.board import Board
from panda_alpha.team.budget import activate, prepare, ticket
from panda_alpha.team.contracts import canonical, digest
from panda_alpha.team.coordinator import Coordinator
from panda_alpha.team.demo import (SyntheticClient, artifact, blocked, freeze_synthetic, run_demo,
                                   send, synthetic_config, synthetic_grant, synthetic_request)


@pytest.fixture
def team(tmp_path, monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(subprocess, "Popen", blocked)
    proof = tmp_path / "proof.txt"
    proof.write_text("synthetic fixture", encoding="utf-8")
    c = Coordinator(tmp_path / "team.sqlite3")
    registry = ResearchRegistry(tmp_path / "registry.sqlite3")
    config = synthetic_config()
    grant = synthetic_grant(artifact(proof), config)
    try:
        yield c, registry, config, grant, artifact(proof)
    finally:
        registry.close()
        c.close()


def ready(team, owner="market"):
    return freeze_synthetic(team[0].board, synthetic_request(owner, team[4]))


def execute(team, task, client=None):
    c, registry, config, _, _ = team
    return c.dispatch(task["task_id"], config, client or SyntheticClient(), registry, execute=True)


def test_complete_two_line_offline_rehearsal(tmp_path):
    report = run_demo(tmp_path / "demo")
    assert report["status"] == "PILOT_READY"
    assert report["closed_tasks"] == report["synthetic_creates"] == report["synthetic_starts"] == 2
    assert report["official_runs"] == report["paid_llm_calls"] == report["network_calls"] == 0
    assert report["plan"]["control"]["mode"] == "paused"
    with pytest.raises(FileExistsError):
        run_demo(tmp_path / "demo")


def test_independent_challenge_response_approval_and_stale_messages(team):
    c, _, _, _, proof = team
    task = c.board.submit(synthetic_request("market", proof))
    with pytest.raises(ValueError, match="Independent"):
        send(c.board, task["task_id"], "market", "challenge")
    task = send(c.board, task["task_id"], "skeptic", "challenge")
    challenge = c.board.events(task["task_id"])[-1]
    # An exact network retry does not increment the version a second time.
    assert c.board.message(challenge["payload"])["version"] == task["version"]
    changed = dict(challenge["payload"], event_id="other", version=1)
    with pytest.raises(ValueError, match="Stale"):
        c.board.message(changed)
    with pytest.raises(ValueError, match="Author response"):
        send(c.board, task["task_id"], "fundamental", "response", reply_to=challenge["event_id"])
    with pytest.raises(ValueError, match="current research stage"):
        send(c.board, task["task_id"], "skeptic", "review", decision="approve", reply_to=challenge["event_id"])
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        c.board.db.execute("DELETE FROM team_events")


def test_immutable_request_exact_duplicate_and_separate_directions(team):
    c, _, _, _, proof = team
    request = synthetic_request("market", proof)
    task = c.board.submit(request)
    assert c.board.submit(request)["version"] == 1
    duplicate = dict(request, task_id="rename")
    with pytest.raises(ValueError, match="Duplicate"):
        c.board.submit(duplicate)
    with pytest.raises(ValueError, match="distinct information"):
        c.board.submit(dict(synthetic_request("fundamental", proof), information_family=request["information_family"]))
    with pytest.raises(ValueError, match="invalid in state"):
        c.board.freeze(task["task_id"], 1, {"request_hash": task["request_hash"], "priority": 50,
                       "allocation_reason": "test", "diversity_check": "test"})


@pytest.mark.parametrize("declared", [["close"], ["CLOSE"], ["Close"]])
def test_formula_field_case_aliases_preserve_frozen_request_identity(team, declared):
    from panda_alpha.team.contracts import validate_request
    request = synthetic_request("market", team[4])
    request["candidate"]["formula"] = "RANK(CLOSE / REF(CLOSE, 1))"
    request["primary_fields"] = declared
    original = deepcopy(request)
    identity = validate_request(request)
    task = team[0].board.submit(request)
    assert request == original
    assert task["request_hash"] == identity["request_hash"] == digest(original)
    assert task["request"] == original


def test_formula_case_aliases_do_not_hide_an_undeclared_input(team):
    request = synthetic_request("market", team[4])
    request["candidate"]["formula"] = "RANK(CLOSE / VOLUME)"
    request["primary_fields"] = ["close"]
    with pytest.raises(ValueError, match="actual=.*volume"):
        team[0].board.submit(request)


def test_default_offline_rejects_even_execute_before_account_access(team):
    task = ready(team)
    with pytest.raises(ValueError, match="offline"):
        execute(team, task)
    assert team[0].ledger.jobs() == []


@pytest.mark.parametrize("boundary,expected", [("balance", None), ("reserved", "RESERVED"), ("create", "CREATED")])
def test_chat_pause_between_calls_never_starts_a_new_run(team, monkeypatch, tmp_path, boundary, expected):
    c, registry, config, grant, _ = team
    task = ready(team)
    activate(c.board, grant)
    stopped = False

    def check_active():
        if stopped:
            raise InterruptedError("Synthetic pause")

    class PausingClient(SyntheticClient):
        def balance(self):
            nonlocal stopped
            value = super().balance()
            stopped = boundary == "balance"
            return value

        def create(self, *args):
            nonlocal stopped
            value = super().create(*args)
            stopped = boundary == "create"
            return value

    original_reserve = c.ledger.reserve
    def reserve(*args, **kwargs):
        nonlocal stopped
        value = original_reserve(*args, **kwargs)
        stopped = boundary == "reserved"
        return value
    monkeypatch.setattr(c.ledger, "reserve", reserve)
    client = PausingClient()
    with pytest.raises(InterruptedError):
        c.dispatch(task["task_id"], config, client, registry, execute=True, before_remote=check_active)
    job = c.ledger.get(task["fingerprint"])
    assert (job["state"] if job else None) == expected
    assert client.starts == 0 and client.creates == int(boundary == "create")
    if job:
        assert c.resume(task["task_id"], client, tmp_path / "paused")["state"] == expected
        assert client.starts == 0  # No Run ID requires explicit reconciliation, never re-dispatch.


@pytest.mark.parametrize("change,expected", [
    (lambda g: g.update(status="draft"), "explicitly authorized"),
    (lambda g: g.update(allow_dynamic_candidates=False), "explicitly authorized"),
    (lambda g: g.update(max_total_credits=float("nan")), "finite"),
    (lambda g: g.update(max_runs=True), "positive integer"),
    (lambda g: g.update(accept_estimated_charge_risk=False), "explicitly accept"),
])
def test_invalid_grant_never_activates(team, change, expected):
    grant = deepcopy(team[3])
    change(grant)
    with pytest.raises(ValueError, match=expected):
        activate(team[0].board, grant)
    assert team[0].board.mode()["mode"] == "offline"


def test_installed_grant_cannot_be_widened_or_study_reset(team):
    c, _, _, grant, _ = team
    activate(c.board, grant)
    with pytest.raises(ValueError, match="immutable"):
        activate(c.board, dict(grant, max_runs=10))
    changed = deepcopy(grant)
    changed["grant_id"] = "new-date"
    changed["study_policy"]["study_id"] = "reset"
    with pytest.raises(ValueError, match="no automatic"):
        activate(c.board, changed)


def test_ticket_uses_beijing_dates_and_exact_fingerprint(team):
    grant = dict(team[3], valid_from="2026-10-17T16:30:00+00:00", valid_until="2026-10-18T17:00:00+00:00")
    task = ready(team)
    derived = ticket(grant, task)
    assert derived["valid_from"] == "2026-10-18"
    assert derived["valid_until"] == "2026-10-19"
    assert derived["fingerprints"] == [task["fingerprint"]]
    assert derived["batch_id"] == grant["grant_id"]


def test_expired_paused_and_changed_evidence_fail_before_spend(team):
    c, _, _, grant, _ = team
    task = ready(team)
    grant["valid_from"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    grant["valid_until"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    activate(c.board, grant)
    with pytest.raises(ValueError, match="not currently valid"):
        execute(team, task)
    c.board.pause("stop")
    with pytest.raises(ValueError, match="paused"):
        execute(team, task)
    assert c.ledger.jobs() == []


@pytest.mark.parametrize("boundary", ["create", "start"])
@pytest.mark.parametrize("change", ["expire", "pause", "replace"])
def test_grant_changes_are_rechecked_at_each_remote_mutation(team, monkeypatch, tmp_path, boundary, change):
    from panda_alpha.team import budget
    c, _, _, grant, _ = team
    task = ready(team)
    activate(c.board, grant)
    instant = [datetime.now(timezone.utc)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant[0] if tz is not None else instant[0].replace(tzinfo=None)

    monkeypatch.setattr(budget, "datetime", Clock)

    def change_authority():
        if change == "expire":
            instant[0] = datetime.fromisoformat(grant["valid_until"])
        elif change == "pause":
            c.board.pause("operator stopped before next remote mutation")
        else:
            activate(c.board, dict(grant, grant_id=grant["grant_id"] + "-replacement"))

    if boundary == "create":
        original_reserve = c.ledger.reserve

        def reserve_then_change(*args, **kwargs):
            result = original_reserve(*args, **kwargs)
            change_authority()
            return result

        monkeypatch.setattr(c.ledger, "reserve", reserve_then_change)

    class Client(SyntheticClient):
        def create(self, *args):
            factor_id = super().create(*args)
            if boundary == "start":
                change_authority()
            return factor_id

    client = Client()
    expected_error = {"expire": "not currently valid", "pause": "paused", "replace": "active team grant"}[change]
    with pytest.raises(ValueError, match=expected_error):
        execute(team, task, client)
    job = c.ledger.get(task["fingerprint"])
    expected_state = "RESERVED" if boundary == "create" else "CREATED"
    assert job["state"] == expected_state and job["run_id"] is None
    assert client.creates == (0 if boundary == "create" else 1) and client.starts == 0
    assert c.board.task(task["task_id"])["state"] == "submitted"
    # Retrying or querying this fingerprint must never create/start implicitly.
    assert execute(team, task, client)["state"] == expected_state
    assert c.resume(task["task_id"], client, tmp_path / "out")["state"] == expected_state
    assert client.creates == (0 if boundary == "create" else 1) and client.starts == 0


@pytest.mark.parametrize("change", ["expire", "replace"])
def test_existing_run_can_be_resumed_after_grant_is_no_longer_current(team, monkeypatch, tmp_path, change):
    from panda_alpha.team import budget
    c, _, _, grant, _ = team
    task = ready(team)
    activate(c.board, grant)
    client = SyntheticClient()
    original = execute(team, task, client)
    if change == "expire":
        class ExpiredClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.fromisoformat(grant["valid_until"])
        monkeypatch.setattr(budget, "datetime", ExpiredClock)
    else:
        activate(c.board, dict(grant, grant_id=grant["grant_id"] + "-replacement"))
    result = c.resume(task["task_id"], client, tmp_path / "out")
    assert result["state"] == "SETTLED" and c.ledger.get(task["fingerprint"])["run_id"] == original["run_id"]
    assert client.creates == client.starts == 1


@pytest.mark.parametrize("field,value", [("sources", ["undeclared_vendor"]), ("primary_fields", ["undeclared_field"])])
def test_grant_rejects_new_source_or_field(team, field, value):
    request = synthetic_request("market", team[4])
    request[field] = value
    if field == "primary_fields":
        request["candidate"]["formula"] = "RANK(undeclared_field)"
    task = freeze_synthetic(team[0].board, request)
    activate(team[0].board, team[3])
    with pytest.raises(ValueError, match="outside authorized"):
        execute(team, task)


def test_sealed_window_and_config_change_do_not_use_account(team):
    task = ready(team)
    activate(team[0].board, team[3])
    team[2]["research"]["sealed_windows"] = [{"start": "2024-02-01", "end": "2024-02-28"}]
    with pytest.raises(ValueError, match="sealed OOS"):
        execute(team, task)
    team[2]["research"]["sealed_windows"] = []
    team[2]["research"]["cycle"] = 10
    with pytest.raises(ValueError, match="frozen cycle"):
        execute(team, task)


def test_changed_artifact_is_rejected(team):
    from pathlib import Path
    task = ready(team)
    activate(team[0].board, team[3])
    Path(team[4]["path"]).write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="hash|SHA|changed|match"):
        execute(team, task)
    assert team[0].ledger.jobs() == []


def test_legacy_dispatch_cannot_bypass_team_mode_or_permit(team):
    c, _, config, grant, _ = team
    task = ready(team)
    configured = deepcopy(config)
    configured["compute"]["estimated_billing_batch_authorization"] = ticket(grant, task)
    client = SyntheticClient()
    with pytest.raises(ValueError, match="offline"):
        dispatch(task["request"]["candidate"], task["request"]["window"], configured, c.ledger, client)
    activate(c.board, grant)
    with pytest.raises(ValueError, match="Exact permit"):
        dispatch(task["request"]["candidate"], task["request"]["window"], configured, c.ledger, client)
    assert client.creates == 0 and c.board.task(task["task_id"])["state"] == "ready"


def test_synthetic_permission_never_uses_real_client(team):
    c, registry, config, grant, _ = team
    task = ready(team)
    activate(c.board, grant)
    client = PandaClient({})
    with pytest.raises(ValueError, match="Synthetic grant"):
        c.dispatch(task["task_id"], config, client, registry, execute=True)
    prepared = prepare(c.board, task, config, simulation=True)
    with pytest.raises(ValueError, match="Synthetic permit"):
        dispatch(task["request"]["candidate"], task["request"]["window"], prepared, c.ledger, client)


def test_atomic_reservation_rechecks_forged_permit_and_metadata(team):
    c, _, config, grant, _ = team
    task = ready(team)
    activate(c.board, grant)
    prepared = prepare(c.board, task, config, simulation=True)
    prepared["compute"]["team_permit"]["request_hash"] = "0" * 64
    client = SyntheticClient()
    with pytest.raises(ValueError, match="ready frozen"):
        dispatch(task["request"]["candidate"], task["request"]["window"], prepared, c.ledger, client)
    assert client.creates == 0 and c.ledger.jobs() == []
    prepared["compute"]["team_permit"]["request_hash"] = task["request_hash"]
    candidate = dict(task["request"]["candidate"], research_question="changed after review")
    with pytest.raises(ValueError, match="exact reviewed"):
        dispatch(candidate, task["request"]["window"], prepared, c.ledger, client)


def test_ambiguous_create_survives_restart_and_never_redispatches(team, tmp_path):
    c, registry, config, grant, _ = team
    task = ready(team)
    activate(c.board, grant)
    class AmbiguousClient(SyntheticClient):
        def create(self, *args):
            self.creates += 1
            raise TimeoutError("response lost")
    client = AmbiguousClient()
    with pytest.raises(TimeoutError):
        execute(team, task, client)
    assert c.board.task(task["task_id"])["state"] == "submitted"
    reopened = Coordinator(tmp_path / "team.sqlite3")
    try:
        assert reopened.dispatch(task["task_id"], config, client, registry, execute=True)["state"] == "CREATING"
        assert reopened.resume(task["task_id"], client, tmp_path / "out")["state"] == "CREATING"
        assert client.creates == 1 and client.starts == 0
    finally:
        reopened.close()


def test_single_inflight_across_two_connections_then_resume_when_paused(team, tmp_path):
    c, registry, config, grant, _ = team
    first, second = ready(team), ready(team, "fundamental")
    activate(c.board, grant)
    client = SyntheticClient()
    execute(team, first, client)
    other = Coordinator(tmp_path / "team.sqlite3")
    try:
        with pytest.raises(ValueError, match="prior job"):
            other.dispatch(second["task_id"], config, client, registry, execute=True)
        assert other.board.task(second["task_id"])["state"] == "ready"
    finally:
        other.close()
    assert client.creates == 1
    c.board.pause("operator pause")
    assert c.resume(first["task_id"], client, tmp_path / "out")["state"] == "SETTLED"
    assert c.board.task(first["task_id"])["state"] == "result_ready"
    # Repeated recovery does not append a second result event.
    c.resume(first["task_id"], client, tmp_path / "out")
    assert len([e for e in c.board.events(first["task_id"]) if e["kind"] == "result"]) == 1


@pytest.mark.parametrize("limit,value", [("max_runs", 1), ("max_total_credits", 2)])
def test_shared_parent_caps_cover_both_lines(team, tmp_path, limit, value):
    c, _, _, grant, _ = team
    first, second = ready(team), ready(team, "fundamental")
    grant[limit] = value
    activate(c.board, grant)
    client = SyntheticClient()
    execute(team, first, client)
    c.resume(first["task_id"], client, tmp_path / "out")
    with pytest.raises(ValueError, match="budget|slot|exhausted"):
        execute(team, second, client)
    assert client.creates == 1


def test_result_review_binds_reconciled_artifact_not_an_unrelated_file(team, tmp_path):
    c, _, _, grant, proof = team
    task = ready(team)
    activate(c.board, grant)
    client = SyntheticClient()
    execute(team, task, client)
    c.resume(task["task_id"], client, tmp_path / "out")
    event = c.board.events(task["task_id"])[-1]
    with pytest.raises(ValueError, match="original reconciled"):
        send(c.board, task["task_id"], "skeptic", "result_review", decision="confirm", reply_to=event["event_id"], artifacts=[proof])
    task = send(c.board, task["task_id"], "skeptic", "result_review", decision="confirm",
                reply_to=event["event_id"], artifacts=event["payload"]["artifacts"])
    with pytest.raises(ValueError, match="Use conclude"):
        c.board.decide(task["task_id"], task["version"], {"decision": "close", "reason": "no economic claim"})
    review = c.board.events(task["task_id"])[-1]
    assert c.conclude(task["task_id"], task["version"], {"operation_id": "test-close",
        "review_event_id": review["event_id"], "decision": "confirm", "reason": "no economic claim"}, team[1])["state"] == "closed"


def test_existing_human_retirement_blocks_official_attempt(team):
    c, registry, _, grant, _ = team
    task = ready(team)
    registry.append_event("human_retired", "synthetic retired direction", {"actor": "human", "instruction": "Stop this direction",
                          "candidate_ids": [task["request"]["candidate"]["candidate_id"]]}, event_id="retired")
    activate(c.board, grant)
    with pytest.raises(ValueError, match="retired direction"):
        execute(team, task)
    assert c.ledger.jobs() == []




def test_formula_input_scope_cannot_be_hidden_in_declarations(team):
    request = synthetic_request("market", team[4])
    request["candidate"]["formula"] = "RANK(net_profit / total_assets)"
    with pytest.raises(ValueError, match="executable inputs"):
        team[0].board.submit(request)


def test_control_input_requires_explicit_grant_scope(team):
    request = synthetic_request("market", team[4])
    request["control_fields"] = ["net_profit"]
    request["candidate"]["formula"] = "RANK(close / volume + net_profit)"
    task = freeze_synthetic(team[0].board, request)
    activate(team[0].board, team[3])
    with pytest.raises(ValueError, match="Control fields"):
        execute(team, task)


def test_stale_science_registry_cannot_reset_memory_count(team, tmp_path):
    task = ready(team)
    memory = tmp_path / "live.json"
    memory.write_text(json.dumps({"multiple_testing_total": 623}), encoding="utf-8")
    team[2]["research"]["live_memory"] = str(memory)
    activate(team[0].board, team[3])
    with pytest.raises(ValueError, match="ahead of registered facts"):
        execute(team, task)
    assert team[1].new_tested == 0 and team[0].ledger.jobs() == []


def test_synthetic_run_cannot_be_resumed_through_real_account_client(team, tmp_path):
    from panda_alpha.platform import resume
    task = ready(team)
    activate(team[0].board, team[3])
    execute(team, task)
    client = PandaClient({})
    with pytest.raises(ValueError, match="persisted execution environment"):
        team[0].resume(task["task_id"], client, tmp_path / "out")
    with pytest.raises(ValueError, match="persisted execution environment"):
        resume(task["fingerprint"], team[0].ledger, client, tmp_path / "out")


def test_existing_official_ledger_result_is_adopted_without_spend(team, tmp_path):
    from panda_alpha.platform import ExperimentLedger, fingerprint
    request = synthetic_request("market", team[4])
    key = fingerprint(request["candidate"], request["window"], 5, 10)
    path = tmp_path / "legacy.sqlite3"
    old = ExperimentLedger(path)
    old.reserve(key, request["candidate"], {"candidate": request["candidate"], "window": request["window"], "cycle": 5, "groups": 10},
                "exploration", 2, 10, {"total": 100, "gift": 100, "recharge": 0})
    old.transition(key, "RESERVED", "SETTLED", actual=2, factor_id="old", run_id="old",
                   receipt=json.dumps({"status": 2, "actual": 2}))
    old.db.close()
    c = Coordinator(path)
    try:
        task = freeze_synthetic(c.board, request)
        client = PandaClient({})  # Its account methods must never be called.
        assert c.dispatch(task["task_id"], team[2], client, team[1])["run_id"] == "old"
        assert c.board.task(task["task_id"])["state"] == "submitted"
        c.resume(task["task_id"], client, tmp_path / "out")
        assert c.board.task(task["task_id"])["state"] == "result_ready"
        reused = next(e for e in c.board.events(task["task_id"]) if e["kind"] == "reuse_existing")
        assert reused["payload"]["new_independent_oos"] is False
        assert len(c.ledger.jobs()) == 1 and team[1].new_tested == 0
    finally:
        c.close()






def test_readonly_inspection_cannot_install_team_control_on_legacy_ledger(tmp_path):
    from panda_alpha.platform import ExperimentLedger
    path = tmp_path / "legacy.sqlite3"
    ledger = ExperimentLedger(path)
    ledger.db.close()
    with pytest.raises(ValueError, match="not initialized"):
        Coordinator(path, read_only=True)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='team_control'").fetchone() is None
    absent = tmp_path / "missing.sqlite3"
    with pytest.raises(sqlite3.OperationalError):
        Coordinator(absent, read_only=True)
    assert not absent.exists()


def test_role_packets_use_database_readonly_connection(team, tmp_path):
    ready(team)
    c = Coordinator(tmp_path / "team.sqlite3", read_only=True)
    try:
        assert c.board.packet("market")["tasks"][0]["task_id"] == "SYN-market"
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            c.board.pause("unauthorized read-side write")
    finally:
        c.close()


def test_packets_keep_active_work_and_bound_old_context(team):
    c, _, _, _, proof = team
    for index in range(5):
        request = synthetic_request("market", proof)
        request["task_id"] = f"archived-{index}"
        request["candidate"]["formula"] = f"RANK(close / volume + {index})"
        task = c.board.submit(request)
        c.board.decide(task["task_id"], task["version"], {"decision": "park", "reason": "protocol fixture"})
    task = ready(team)
    packet = c.board.packet("market")
    assert len(packet["tasks"]) == 3
    assert packet["tasks"][0]["task_id"] == task["task_id"]
    assert len(packet["recent_history"]) == 5
