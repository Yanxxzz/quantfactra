"""Installed adapters preserve the original lifecycle and cannot adopt other jobs."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from panda_alpha.backends import backend_identity, create_backend
from panda_alpha.platform import ExperimentLedger, PandaClient, dispatch, fingerprint, resume
from panda_alpha.team.coordinator import Coordinator
from panda_alpha.team.demo import synthetic_request
from panda_alpha.team.evidence import artifact


PLUGIN = '''
class Client:
    def __init__(self):
        self.remaining = 100.
        self.calls = []
    def balance(self):
        self.calls.append("balance")
        return {"total": self.remaining, "gift": self.remaining, "recharge": 0.}
    def create(self, candidate, window, cycle, groups, name):
        self.calls.append("create")
        return "local-factor"
    def start(self, factor_id):
        self.calls.append("start")
        self.remaining -= 2.
        return "local-run"
    def status(self, run_id):
        self.calls.append("status")
        return 2
    def result(self, run_id):
        self.calls.append("result")
        return {"success": True, "run_id": run_id, "adapter_fixture": True}
    def logs(self, run_id):
        self.calls.append("logs")
        return {"complete": True, "run_id": run_id, "logs": [], "errors": []}
def make(config):
    if config.get("fixture_token") != "passed-through":
        raise ValueError("Factory did not receive platform configuration")
    return Client()
def alternate_make(config):
    return make(config)
def incomplete(config):
    return object()
not_callable = 42
'''

CANDIDATE = {"candidate_id": "adapter-fixture", "formula": "RANK(close)", "direction": 1}
WINDOW = {"start": "2024-01-01", "end": "2024-06-30"}


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    module = tmp_path / "installed_fixture_backend.py"
    module.write_text(PLUGIN, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    # Each test imports the real file, rather than a patched import function.
    sys.modules.pop("installed_fixture_backend", None)
    value = {"backend": "installed_fixture_backend:make", "connection_id": "fixture-one",
             "fixture_token": "passed-through"}
    yield value
    sys.modules.pop("installed_fixture_backend", None)


def config(platform=None):
    return {"platform": platform or {}, "research": {"cycle": 5, "groups": 10}, "compute": {
        "daily_credit_limit": 10, "recharge_credit_limit": 0, "cost_ceiling": 2,
        "cost_ceiling_verified_at": "fixture", "cost_ceiling_source": "offline fixture",
        "server_enforced_cap": True, "billing_mode": "gift_only",
        "source_probe_fraction": .3, "exploration_fraction": .4, "validation_fraction": .3}}


@pytest.mark.parametrize("options", [{}, {"backend": "pandaai"}])
def test_default_factory_retains_panda_client_and_guard(options):
    guard = lambda: None
    client = create_backend(options, before_call=guard)
    assert type(client) is PandaClient
    assert client.before_call is guard
    assert backend_identity(client) == {"backend": "pandaai", "connection_id": None}


def test_installed_adapter_dispatch_resume_and_immutable_configuration(tmp_path, adapter):
    original = deepcopy(adapter)
    client = create_backend(adapter)
    ledger = ExperimentLedger(tmp_path / "jobs.sqlite3")
    try:
        job = dispatch(CANDIDATE, WINDOW, config(adapter), ledger, client)
        assert json.loads(job["definition"])["backend_identity"] == {
            "backend": adapter["backend"], "connection_id": "fixture-one"}
        assert job["fingerprint"] == fingerprint(CANDIDATE, WINDOW, 5, 10)
        assert dispatch(CANDIDATE, WINDOW, config(adapter), ledger, client) == job
        assert client.calls == ["balance", "create", "start"]
        settled = resume(job["fingerprint"], ledger, client, tmp_path / "results")
        assert settled["state"] == "SETTLED" and settled["actual"] == 2.
        assert json.loads((tmp_path / "results/local-run.json").read_text())["adapter_fixture"]
        assert adapter == original
    finally:
        ledger.db.close()


def test_each_adapter_call_runs_guard_before_the_method(adapter):
    client = None
    observed = []
    def guard():
        observed.append(list(client.calls))
    client = create_backend(adapter, before_call=guard)
    client.balance()
    client.create(CANDIDATE, WINDOW, 5, 10, "fixture")
    client.start("local-factor")
    client.status("local-run")
    client.result("local-run")
    client.logs("local-run")
    assert [len(calls) for calls in observed] == list(range(6))
    assert client.calls == ["balance", "create", "start", "status", "result", "logs"]
    def paused():
        raise InterruptedError("Original deadline reached")
    blocked = create_backend(adapter, before_call=paused)
    with pytest.raises(InterruptedError, match="deadline"):
        blocked.start("local-factor")
    assert blocked.calls == []


@pytest.mark.parametrize("change", [
    {"backend": None}, {"backend": "unknown"}, {"backend": "bad-module:factory"},
    {"backend": "module:factory:extra"}, {"connection_id": None},
    {"connection_id": ""}, {"connection_id": "  "}, {"connection_id": 42},
])
def test_invalid_custom_configuration_fails_before_import(adapter, change, monkeypatch):
    def forbidden(*args):
        raise AssertionError("Invalid settings must fail before importing user code")
    monkeypatch.setattr("panda_alpha.backends.importlib.import_module", forbidden)
    with pytest.raises(ValueError):
        create_backend(dict(adapter, **change))


@pytest.mark.parametrize("factory,match", [("incomplete", "balance, create, start, status, result, logs"),
                                            ("not_callable", "not callable")])
def test_invalid_factory_is_not_accepted(adapter, factory, match):
    with pytest.raises(ValueError, match=match):
        create_backend(dict(adapter, backend="installed_fixture_backend:" + factory))


def test_missing_installed_module_fails_explicitly():
    with pytest.raises(ModuleNotFoundError):
        create_backend({"backend": "nonexistent_quant_adapter_726498:make", "connection_id": "fixture"})


@pytest.mark.parametrize("other", ["connection", "backend", "pandaai"])
def test_cross_backend_or_connection_dispatch_and_resume_are_read_only(tmp_path, adapter, other):
    ledger = ExperimentLedger(tmp_path / "jobs.sqlite3")
    owner = create_backend(adapter)
    try:
        original = dispatch(CANDIDATE, WINDOW, config(adapter), ledger, owner)
        changed = dict(adapter)
        if other == "connection":
            changed["connection_id"] = "fixture-two"
        elif other == "backend":
            changed["backend"] = "installed_fixture_backend:alternate_make"
        else:
            changed = {"backend": "pandaai"}
        client = create_backend(changed)
        def forbidden(*args, **kwargs):
            raise AssertionError("A foreign job must never reach the adapter")
        for method in ("balance", "create", "start", "status", "result", "logs"):
            setattr(client, method, forbidden)
        with pytest.raises(ValueError, match="persisted job"):
            dispatch(CANDIDATE, WINDOW, config(changed), ledger, client)
        with pytest.raises(ValueError, match="persisted job"):
            resume(original["fingerprint"], ledger, client, tmp_path / "results")
        assert ledger.get(original["fingerprint"]) == original
        assert not (tmp_path / "results").exists()
    finally:
        ledger.db.close()


def test_default_definitions_stay_legacy_and_legacy_jobs_reject_custom(tmp_path, adapter):
    from installed_fixture_backend import Client
    ledger = ExperimentLedger(tmp_path / "legacy.sqlite3")
    try:
        job = dispatch(CANDIDATE, WINDOW, config(), ledger, Client())
        assert "backend_identity" not in json.loads(job["definition"])
        with pytest.raises(ValueError, match="persisted job"):
            resume(job["fingerprint"], ledger, create_backend(adapter), tmp_path / "output")
        assert ledger.get(job["fingerprint"]) == job
    finally:
        ledger.db.close()


def test_panda_connection_label_is_bound_without_changing_old_default():
    assert backend_identity(PandaClient({"connection_id": "panda-test"})) == {
        "backend": "pandaai", "connection_id": "panda-test"}


def test_client_and_config_identity_mismatch_never_creates_job(tmp_path, adapter):
    ledger = ExperimentLedger(tmp_path / "empty.sqlite3")
    try:
        client = create_backend(adapter)
        with pytest.raises(ValueError, match="platform configuration"):
            dispatch(CANDIDATE, WINDOW, config(), ledger, client)
        assert ledger.jobs() == [] and client.calls == []
    finally:
        ledger.db.close()


def test_coordinator_cannot_adopt_or_resume_foreign_legacy_job(tmp_path, adapter):
    proof = tmp_path / "proof.txt"
    proof.write_text("Synthetic lifecycle fixture", encoding="utf-8")
    request = synthetic_request("market", artifact(proof))
    path = tmp_path / "team.sqlite3"
    ledger = ExperimentLedger(path)
    key = fingerprint(request["candidate"], request["window"], request["cycle"], request["groups"])
    ledger.reserve(key, request["candidate"], {}, "exploration", 2, 10,
                   {"total": 100., "gift": 100., "recharge": 0.})
    ledger.db.close()
    c = Coordinator(path)
    try:
        c.board.submit(request)
        task = c.board.task(request["task_id"])
        job = c.ledger.get(task["fingerprint"])
        events = c.board.events(task["task_id"])
        client = create_backend(adapter)
        with pytest.raises(ValueError, match="persisted job"):
            c.adopt_existing(task, job, client)
        with pytest.raises(ValueError, match="persisted job"):
            c.dispatch(task["task_id"], config(adapter), client, None)
        with pytest.raises(ValueError, match="persisted job"):
            c.resume(task["task_id"], client, tmp_path / "results")
        assert c.board.events(task["task_id"]) == events
        assert c.ledger.get(task["fingerprint"]) == job
        assert client.calls == []
    finally:
        c.close()


def test_installed_factory_loads_in_real_python_subprocess(tmp_path, adapter):
    script = '''
import json, sys
from panda_alpha.backends import create_backend
client = create_backend(json.loads(sys.argv[1]))
assert client.balance()["total"] == 100.
assert client.backend_identity["connection_id"] == "fixture-one"
print("installed factory loaded")
'''
    root = str(Path(__file__).resolve().parents[1])
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(tmp_path), root]), PYTHONUTF8="1")
    completed = subprocess.run([sys.executable, "-B", "-c", script, json.dumps(adapter)],
                               cwd=tmp_path, env=env, capture_output=True, text=True, timeout=20)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "installed factory loaded"




