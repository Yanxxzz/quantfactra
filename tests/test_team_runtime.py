from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

from panda_alpha.registry import ResearchRegistry
from panda_alpha.team.codex_worker import command, role_config
from panda_alpha.team.coordinator import Coordinator
from panda_alpha.team.demo import artifact, synthetic_request
from panda_alpha.team.host import Host, MailboxAgents
from panda_alpha.team.contracts import digest
from panda_alpha.team.runtime import prepare, retry_attention, serve
from panda_alpha.team.runtime_io import identity, lease, owned, read, terminate, write

REPO = Path(__file__).resolve().parents[1]


def setup(tmp_path, mode="ok", deadline=60, **limits):
    proof = tmp_path / "proof.txt"
    proof.write_text("Synthetic fixture, not native source evidence", encoding="utf-8")
    ledger, registry = tmp_path / "team.sqlite3", tmp_path / "registry.sqlite3"
    c, r = Coordinator(ledger), ResearchRegistry(registry)
    seeds = []
    for role in ("market", "fundamental"):
        request = synthetic_request(role, artifact(proof))
        c.board.submit(request)
        seeds.append(request["task_id"])
    Host(c, r, "offline", {"objective": "Fixture transport and recovery; no account access", "max_rounds": 3,
         "max_actions": 60, "deadline": (datetime.now(timezone.utc) + timedelta(seconds=deadline)).isoformat(), "seed_tasks": seeds})
    c.close()
    r.close()
    modefile = tmp_path / "mode.txt"
    modefile.write_text(mode, encoding="utf-8")
    config = tmp_path / "workers.json"
    write(config, dict({"codex_command": [sys.executable, str(REPO / "tests/fixtures/codex_role_cli.py")],
                       "turn_timeout": 2, "max_attempts": 2, "poll_interval": .05, "retry_backoff": 0,
                       "orphan_timeout": 10, "context_files": [str(modefile)]}, **limits))
    return ledger, registry, "offline", config, tmp_path / "runtime"


def launch(args):
    ledger, registry, session, config, root = args
    log = (root.parent / "supervisor.log").open("ab")
    child = subprocess.Popen([sys.executable, "-m", "panda_alpha.team", "host-serve", "--ledger", str(ledger),
        "--registry", str(registry), "--session", session, "--workers", str(config), "--output", str(root)],
        cwd=REPO, stdout=log, stderr=log, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    log.close()
    return child


def until(check, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = check()
        if value:
            return value
        time.sleep(.05)
    raise AssertionError("Timed out waiting for runtime receipt")


def cleanup(args, child):
    root = args[-1]
    write(root / "control.json", {"run": False, "heartbeat": time.time()})
    for path in (root / "workers").glob("*/work/*/state.json"):
        attempts = read(path)["attempts"]
        if attempts:
            terminate(attempts[-1].get("process"))
    for path in (root / "components").glob("*/process.json"):
        terminate(read(path).get("process"))
    if child.poll() is None:
        child.terminate()
    child.wait(timeout=10)


def test_mailbox_retries_a_transient_windows_response_lock(tmp_path, monkeypatch):
    packet = {"role": "lead", "work_id": "locked-response"}
    response = tmp_path / "lead/responses/locked-response.json"
    envelope = {"work_id": packet["work_id"], "output": {"decision": "stop"}}
    write(response, envelope)
    original = Path.read_text
    locked = True
    def read_once_locked(path, *args, **kwargs):
        nonlocal locked
        if path == response and locked:
            locked = False
            raise PermissionError("Temporary Windows sharing conflict")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", read_once_locked)
    assert MailboxAgents(tmp_path)(packet) == envelope


def test_four_processes_three_rounds_and_tool_restrictions(tmp_path):
    args = setup(tmp_path)
    result = serve(*args)
    assert result["status"] == "complete" and result["result"]["reason"] == "round_limit"
    assert set(result["components"]) == {"lead", "market", "fundamental", "skeptic", "coordinator"}
    root = args[-1]
    sessions = [read(root / "workers" / r / "session.json")["thread_id"] for r in ("lead", "market", "fundamental", "skeptic")]
    assert len(set(sessions)) == 4
    manifest = read(root / "manifest.json")
    argv = command(manifest["workers"], tmp_path, sessions[0])
    assert argv[argv.index("resume") + 1] == sessions[0] and "--last" not in argv
    assert '--ignore-user-config' in argv and 'sandbox_mode="read-only"' in argv and 'approval_policy="never"' in argv
    c, r = Coordinator(args[0]), ResearchRegistry(args[1])
    try:
        assert Host(c, r, "offline").state()["round"] == 3
        assert c.ledger.jobs() == []
        assert all(t["state"] == "parked" for t in c.board.tasks())
    finally:
        c.close()
        r.close()
    assert not any(owned(read(p)["process"]) for p in (root / "components").glob("*/process.json"))


def test_disconnect_retries_same_work_and_same_role_thread(tmp_path):
    args = setup(tmp_path, "retry")
    result = serve(*args)
    assert result["result"]["reason"] == "round_limit"
    states = [read(p) for p in (args[-1] / "workers").glob("*/work/*/state.json")]
    assert states and all(len(s["attempts"]) == 2 for s in states)
    assert all(s["attempts"][1]["resume_thread"].startswith("fixture-") for s in states)


def test_completed_string_encoding_recovery_avoids_new_model_attempts(tmp_path):
    args = setup(tmp_path, "literal_string_controls")
    result = serve(*args)
    assert result['result']['reason'] == 'round_limit'
    states = [read(p) for p in (args[-1] / 'workers').glob('*/work/*/state.json')]
    assert states and all(len(s['attempts']) == 1 for s in states)
    assert all(s['status'] == 'done' for s in states)
    repairs = list((args[-1] / 'workers').glob('*/work/*/1/output-json-repair.json'))
    assert len(repairs) == len(states)
    assert all(read(p)['changed_control_characters'] == 3 for p in repairs)


@pytest.mark.parametrize("mode,reason,attempts", [("timeout", "model_attempt_limit", 2), ("quota", "model_quota", 1),
                                                ("auth_stderr", "model_authentication", 1),
                                                ("invalid_completed", "model_attempt_limit", 2)])
def test_timeout_and_quota_stop_with_durable_limits(tmp_path, mode, reason, attempts):
    args = setup(tmp_path, mode)
    result = serve(*args)
    assert result["status"] == "attention" and result["result"]["reason"] == reason
    states = list((args[-1] / "workers").glob("*/work/*/state.json"))
    before = [p.read_bytes() for p in states]
    assert all(len(read(p)["attempts"]) == attempts for p in states)
    if mode == 'invalid_completed':
        assert all(a['reason'] == 'invalid_model_reply' for p in states for a in read(p)['attempts'])
    assert serve(*args)["status"] == "attention"
    assert before == [p.read_bytes() for p in states]


def test_restart_adopts_active_model_then_recovers_worker_and_coordinator(tmp_path):
    args = setup(tmp_path, "timeout", turn_timeout=4)
    root = args[-1]
    parent = launch(args)
    try:
        path = until(lambda: next((p for p in (root / "workers").glob("*/work/*/state.json")
                                  if read(p)["attempts"][-1]["status"] == "running"), None))
        first = read(path)["attempts"][0]["process"]
        role = path.parents[2].name
        worker = read(root / "components" / role / "process.json")["process"]
        # A hard worker exit leaves the model process alive; replacement adopts it.
        owned(worker).kill()
        until(lambda: len(read(root / "supervisor.json")["components"][role]["launches"]) == 2)
        assert read(path)["attempts"][0]["process"] == first
        owned(read(root / "components/coordinator/process.json")["process"]).kill()
        until(lambda: len(read(root / "supervisor.json")["components"]["coordinator"]["launches"]) == 2)
        parent.kill()
        parent.wait(timeout=5)
        parent = launch(args)
        parent.wait(timeout=30)
        assert read(root / "supervisor.json")["result"]["reason"] == "model_attempt_limit"
        assert len(read(path)["attempts"]) == 2
        assert not owned(first)
    finally:
        cleanup(args, parent)


def test_exclusive_runtime_and_immutable_binding(tmp_path):
    args = setup(tmp_path)
    with lease(args[-1] / "supervisor.lock"):
        with pytest.raises(RuntimeError, match="lease"):
            serve(*args)
    prepare(*args)
    config = read(args[3])
    config["max_attempts"] = 3
    write(args[3], config)
    with pytest.raises(ValueError, match="binding changed"):
        prepare(*args)


def test_role_model_choices_are_frozen_and_used_on_resume(tmp_path):
    args = setup(tmp_path, model="gpt-6.1-sol", reasoning_effort="max",
                 role_overrides={"skeptic": {"reasoning_effort": "ultra"}})
    _, manifest = prepare(*args)
    for role, effort in (("lead", "max"), ("market", "max"), ("fundamental", "max"), ("skeptic", "ultra")):
        for thread in (None, "existing-role-session"):
            argv = command(role_config(manifest["workers"], role), tmp_path, thread)
            assert argv[argv.index("-m") + 1] == "gpt-6.1-sol"
            assert 'model_reasoning_effort="' + effort + '"' in argv
            assert 'approval_policy="never"' in argv
    changed = read(args[3])
    changed["role_overrides"]["skeptic"]["reasoning_effort"] = "max"
    write(args[3], changed)
    with pytest.raises(ValueError, match="binding changed"):
        prepare(*args)


@pytest.mark.parametrize("overrides", [{"unknown": {}}, {"lead": {"turn_timeout": 3600}},
                                       {"market": {"model": ""}}, {"skeptic": None}])
def test_role_overrides_cannot_change_tools_or_runtime_limits(tmp_path, overrides):
    args = setup(tmp_path, role_overrides=overrides)
    with pytest.raises(ValueError, match="roles|overridden"):
        prepare(*args)


def test_deadline_stops_live_attempt_without_resetting_session(tmp_path):
    args = setup(tmp_path, "timeout", deadline=3, turn_timeout=60)
    result = serve(*args)
    assert result["result"]["reason"] == "deadline"
    assert not any(owned(read(p)["attempts"][-1].get("process")) for p in (args[-1] / "workers").glob("*/work/*/state.json"))


def test_pid_reuse_never_signals_unrelated_process():
    receipt = identity()
    receipt["created"] -= 100
    assert owned(receipt) is None
    terminate(receipt)


def test_ambiguous_model_launch_never_reissued(tmp_path):
    args = setup(tmp_path)
    root, _ = prepare(*args)
    c, r = Coordinator(args[0]), ResearchRegistry(args[1])
    packet = Host(c, r, "offline").next()["packet"]
    c.close()
    r.close()
    MailboxAgents(root / "mailbox")(packet)
    path = root / "workers" / packet["role"] / "work" / packet["work_id"] / "state.json"
    write(path, {"packet_hash": digest(packet), "status": "pending", "attempts": [{"status": "launching", "number": 1}]})
    assert serve(*args)["result"]["reason"] == "ambiguous_model_launch"
    assert len(read(path)["attempts"]) == 1
    with pytest.raises(ValueError, match="Only a repaired"):
        retry_attention(root, "Cannot establish whether model was launched")


def test_operator_auth_retry_keeps_original_limits_and_audit(tmp_path):
    args = setup(tmp_path, "quota")
    assert serve(*args)["result"]["reason"] == "model_quota"
    retry_attention(args[-1], "Operator restored model quota")
    result = serve(*args)  # Fixture remains unavailable, so second failure exhausts the original budget.
    assert result["result"]["reason"] == "model_quota"
    assert len(list((args[-1] / "operator-retries").glob("*.json"))) == 1
    with pytest.raises(ValueError, match="exhausted"):
        retry_attention(args[-1], "Cannot reset exhausted attempts")
