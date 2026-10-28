"""Authored HTTP fixtures, never paid model outputs or research judgments."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sys
import threading

import pytest
import requests

from quantfactra.ai import JSONProvider, ProviderPlanner, ResearchEngine
from quantfactra.providers import MODEL_ACTIONS
from quantfactra.workflow import CommandAgents, ModelAgents


def wire(protocol, output, *, usage=True):
    text = json.dumps(output)
    data = {"id": "authored-response", "model": "authored-fixture-model"}
    if protocol == "openai_responses":
        data.update(status="completed", output=[{"type": "reasoning"}, {"type": "message", "content": [
            {"type": "output_text", "text": text}]}])
    elif protocol == "anthropic_messages":
        data.update(stop_reason="end_turn", content=[{"type": "text", "text": text}])
    else:
        data["choices"] = [{"finish_reason": "stop", "message": {"content": text}}]
    if usage:
        data["usage"] = ({"prompt_tokens": 23, "completion_tokens": 11, "total_tokens": 34}
                         if protocol == "chat_completions" else {"input_tokens": 23, "output_tokens": 11})
    return data


@contextmanager
def server(responder):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append({"path": self.path, "headers": dict(self.headers), "body": body})
            status, data = responder(body)
            content = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("x-request-id", "authored-http-request")
            self.end_headers()
            self.wfile.write(content)
        def log_message(self, *args):
            pass
    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{http.server_port}/v1", calls
    finally:
        http.shutdown()
        http.server_close()
        thread.join(timeout=5)


def config(protocol, base):
    return {"enabled": True, "provider": protocol, "model": "authored-fixture-model",
            "base_url": base, "api_key_env": "QF_TEST_KEY", "api_version": "2026-10-21",
            "max_output_tokens": 256, "timeout_seconds": 10}


def packet(action="propose"):
    return {"work_id": "authored-" + action, "role": "market", "action": action,
            "role_instructions": "Authored transport fixture; preserve the original task.",
            "task": {"task_id": "TEST-ONLY", "version": 3},
            "reply_contract": {"envelope": "exact work_id and action-specific output"}}


@pytest.mark.parametrize("protocol,path", [("openai_responses", "/v1/responses"),
    ("chat_completions", "/v1/chat/completions"), ("anthropic_messages", "/v1/messages")])
def test_real_http_protocols_preserve_context_usage_and_never_repeat(tmp_path, monkeypatch, protocol, path):
    monkeypatch.setenv("QF_TEST_KEY", "test-only-not-a-real-key")
    p = packet()
    expected = {"work_id": p["work_id"], "output": {"decision": "no_candidate", "reason": "Authored fixture"}}
    with server(lambda body: (200, wire(protocol, expected))) as (base, calls):
        provider = JSONProvider(config(protocol, base), tmp_path / "receipts")
        assert not provider.root.exists()
        agents = ModelAgents(provider)
        assert agents(p) == expected
        monkeypatch.delenv("QF_TEST_KEY")
        assert ModelAgents(JSONProvider(config(protocol, base), provider.root))(p) == expected
        assert len(calls) == 1 and calls[0]["path"] == path
        body = calls[0]["body"]
        messages = body.get("input", body.get("messages"))
        assert json.loads(messages[-1]["content"]) == p
        assert calls[0]["headers"]["Authorization"] == "Bearer test-only-not-a-real-key"
        if protocol == "anthropic_messages":
            assert calls[0]["headers"]["anthropic-version"] == "2026-10-21"
        receipt = json.loads(next(provider.root.glob("*.receipt.json")).read_text())
        assert receipt["usage"] == wire(protocol, expected)["usage"]
        assert receipt["actual_cost"] is None and receipt["provider_request_id"] == "authored-http-request"
        assert "test-only-not-a-real-key" not in next(provider.root.glob("*.request.json")).read_text()
        with pytest.raises(ValueError, match="different context"):
            agents(dict(p, task={"task_id": "changed"}))


@pytest.mark.parametrize("action", sorted(MODEL_ACTIONS))
def test_every_host_model_action_uses_its_original_reply_contract(tmp_path, monkeypatch, action):
    provider = JSONProvider(config("chat_completions", "http://127.0.0.1:1/v1"), tmp_path)
    def complete(system, payload, *, request_id, validate):
        assert payload == packet(action) and request_id == payload["work_id"]
        value = {"work_id": request_id, "output": {"authored_action": action}}
        validate(value)
        return value
    monkeypatch.setattr(provider, "complete_json", complete)
    assert ModelAgents(provider)(packet(action))["output"]["authored_action"] == action
    with pytest.raises(ValueError, match="Host model-action"):
        ModelAgents(provider)(packet("dispatch"))


def test_disabled_missing_key_and_config_check_do_not_create_requests(tmp_path, monkeypatch):
    cfg = config("chat_completions", "http://127.0.0.1:1/v1")
    monkeypatch.delenv("QF_TEST_KEY", raising=False)
    provider = JSONProvider(dict(cfg, enabled=False), tmp_path / "receipts")
    assert provider.readiness()["network_calls"] == 0
    with pytest.raises(RuntimeError, match="disabled"):
        ModelAgents(provider)(packet())
    with pytest.raises(ValueError, match="environment variable"):
        ModelAgents(JSONProvider(cfg, provider.root))(packet())
    assert not provider.root.exists()
    with pytest.raises(ValueError, match="credentials"):
        JSONProvider(dict(cfg, api_key="do-not-store-secrets"), provider.root)


@pytest.mark.parametrize("status,bad_id,usage", [(401, False, False), (200, True, True)])
def test_http_or_envelope_failure_keeps_raw_response_and_does_not_retry(tmp_path, monkeypatch, status, bad_id, usage):
    monkeypatch.setenv("QF_TEST_KEY", "fixture")
    value = {"work_id": "wrong" if bad_id else packet()["work_id"], "output": {}}
    with server(lambda body: (status, wire("chat_completions", value, usage=usage))) as (base, calls):
        agents = ModelAgents(JSONProvider(config("chat_completions", base), tmp_path))
        with pytest.raises(RuntimeError, match="no automatic retry"):
            agents(packet())
        with pytest.raises(RuntimeError, match="do not auto-retry"):
            agents(packet())
        receipt = json.loads(next(tmp_path.glob("*.receipt.json")).read_text())
        assert receipt["state"] == "request_unresolved" and len(calls) == 1
        assert bool(receipt["usage"]) == usage and receipt["actual_cost"] is None
        assert next(tmp_path.glob("*.response.json")).is_file()


def test_timeout_keeps_request_and_never_tries_paid_post_again(tmp_path, monkeypatch):
    monkeypatch.setenv("QF_TEST_KEY", "fixture")
    calls = []
    def timeout(*a, **k):
        calls.append(1)
        raise requests.Timeout("authored timeout")
    monkeypatch.setattr(requests.Session, "post", timeout)
    agents = ModelAgents(JSONProvider(config("openai_responses", "http://127.0.0.1:1/v1"), tmp_path))
    for _ in range(2):
        with pytest.raises(RuntimeError):
            agents(packet())
    assert len(calls) == 1


@pytest.mark.parametrize("protocol", ["openai_responses", "chat_completions", "anthropic_messages"])
def test_valid_json_inside_a_truncated_answer_does_not_count_as_completion(tmp_path, monkeypatch, protocol):
    monkeypatch.setenv("QF_TEST_KEY", "fixture")
    value = {"work_id": packet()["work_id"], "output": {}}
    data = wire(protocol, value)
    if protocol == "openai_responses":
        data["status"] = "incomplete"
    elif protocol == "anthropic_messages":
        data["stop_reason"] = "max_tokens"
    else:
        data["choices"][0]["finish_reason"] = "length"
    with server(lambda body: (200, data)) as (base, calls):
        agents = ModelAgents(JSONProvider(config(protocol, base), tmp_path))
        with pytest.raises(RuntimeError, match="no automatic retry"):
            agents(packet())
        with pytest.raises(RuntimeError, match="do not auto-retry"):
            agents(packet())
        assert len(calls) == 1


def test_planner_supports_generation_and_reflection_through_real_http(tmp_path, monkeypatch):
    monkeypatch.setenv("QF_TEST_KEY", "fixture")
    def respond(body):
        context = json.loads(body["input"][-1]["content"])
        if context["stage"] == "reflect":
            value = {"action": "new_source", "failure_type": "source_pending", "explanation": "Authored fixture", "required_tests": ["Verify source"]}
        else:
            value = {"candidates": [{"hypothesis": "Authored planning fixture", "mechanism": "Interface only",
                    "formula": "RANK(close)", "source": "synthetic_unvalidated"}]}
        return 200, wire("openai_responses", value)
    with server(respond) as (base, calls):
        backend = ProviderPlanner(JSONProvider(config("openai_responses", base), tmp_path))
        assert len(ResearchEngine(backend=backend, memory={}).propose(count=1)) == 1
        assert backend.complete_json("reflect", {"failure": "source_pending"})["action"] == "new_source"
        assert len(calls) == 2


def test_full_existing_host_rehearsal_uses_authored_provider_json(tmp_path, monkeypatch):
    from panda_alpha.team import rehearsal
    original = rehearsal.ScriptedRoles
    actions = set()
    def factory(proof, repair):
        authored = original(proof, repair)
        def post(session, url, **kwargs):
            p = json.loads(kwargs["json"]["messages"][-1]["content"])
            actions.add(p["action"])
            r = requests.Response()
            r.status_code = 200
            r._content = json.dumps(wire("chat_completions", authored(p))).encode()
            return r
        monkeypatch.setattr(requests.Session, "post", post)
        cfg = config("chat_completions", "http://127.0.0.1:1/v1")
        cfg["api_key_env"] = ""
        return ModelAgents(JSONProvider(cfg, tmp_path / "authored-protocol-receipts"))
    monkeypatch.setattr(rehearsal, "ScriptedRoles", factory)
    result = rehearsal.run_rehearsal(tmp_path / "host")
    assert result["status"] == "OFFLINE_LIFECYCLE_VERIFIED"
    assert {"propose", "challenge", "response", "review", "freeze", "result_review", "conclude"} <= actions


def test_worker_through_command_agents_uses_same_response_after_process_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("QF_TEST_KEY", "fixture")
    p = packet()
    expected = {"work_id": p["work_id"], "output": {"decision": "no_candidate", "reason": "Authored fixture"}}
    with server(lambda body: (200, wire("anthropic_messages", expected))) as (base, calls):
        cfg = config("anthropic_messages", base)
        cfg["receipts_dir"] = "private receipts"
        path = tmp_path / "model.local.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        command = [sys.executable, "-m", "quantfactra.model_worker", "--config", str(path)]
        commands = {role: command for role in ["lead", "market", "fundamental", "skeptic"]}
        assert CommandAgents(commands, tmp_path / "first-command")(p) == expected
        # New transport cache and process still recover provider's original request.
        assert CommandAgents(commands, tmp_path / "second-command")(p) == expected
        assert len(calls) == 1


def test_worker_check_is_offline_and_leaves_no_receipts(tmp_path, monkeypatch, capsys):
    from quantfactra.model_worker import main
    def blocked(*a, **k):
        pytest.fail("Configuration check made a network call")
    monkeypatch.setattr(requests.Session, "post", blocked)
    cfg = config("openai_responses", "https://api.openai.com/v1")
    cfg.update(enabled=False, receipts_dir="runs/receipts")
    path = tmp_path / "model.local.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    main(["--config", str(path), "--check"])
    assert json.loads(capsys.readouterr().out)["enabled"] is False
    assert not (tmp_path / "runs").exists()


