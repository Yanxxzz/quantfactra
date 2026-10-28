"""监管已冻结流程的 worker 与单写入协调器，按原身份有限恢复。

部署启停交给 operator/service；本层只负责子进程、超时和工作恢复。
lean 仅启动所需角色，旧四角色 manifest 仍按其原绑定执行。
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import tomllib
import uuid

from .contracts import ROLES, digest, timestamp
from .runtime_io import active, identity, lease, owned, read, terminate, write

COMPONENTS = (*ROLES, "coordinator")
DEFAULTS = {"turn_timeout": 600, "max_attempts": 3, "retry_backoff": 5,
            "poll_interval": 1, "orphan_timeout": 30, "max_component_restarts": 3}


def worker_config(path):
    config = read(path)
    allowed = set(DEFAULTS) | {"codex_command", "model", "reasoning_effort", "context_files", "role_overrides", "prompt_format"}
    allowed.update({"max_prompt_bytes", "session_scope"})
    if not isinstance(config, dict) or set(config) - allowed:
        raise ValueError("Unknown worker configuration fields")
    value = dict(DEFAULTS, **config)
    if value.get("prompt_format", "full") not in {"full", "lossless_refs_v1"}:
        raise ValueError("Unknown prompt_format")
    if value.get("session_scope", "role") not in {"role", "task"}:
        raise ValueError("Unknown session_scope")
    argv = value.get("codex_command")
    if not isinstance(argv, list) or not argv or any(not isinstance(a, str) or not a for a in argv):
        raise ValueError("An explicit trusted codex_command argv is required")
    executable = shutil.which(argv[0])
    if not executable:
        raise ValueError("Codex executable not found; install/login before starting the host")
    value["codex_command"] = [str(Path(executable).resolve()), *argv[1:]]
    for key, bounds in {"turn_timeout": (1, 3600), "max_attempts": (1, 5), "retry_backoff": (0, 60),
                        "poll_interval": (.05, 5), "orphan_timeout": (5, 120), "max_component_restarts": (0, 5)}.items():
        if type(value[key]) not in (int, float) or not bounds[0] <= value[key] <= bounds[1]:
            raise ValueError(f"Invalid worker limit: {key}")
    for key in ("max_attempts", "max_component_restarts"):
        if type(value[key]) is not int:
            raise ValueError(f"{key} must be an integer")
    if "max_prompt_bytes" in value and (type(value["max_prompt_bytes"]) is not int or value["max_prompt_bytes"] <= 0):
        raise ValueError("max_prompt_bytes must be a positive integer")
    # Read only non-secret model choices, never the login/auth file. Snapshot once.
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    user_config = home / "config.toml"
    inherited = tomllib.loads(user_config.read_text(encoding="utf-8")) if user_config.exists() else {}
    for key, source in (("model", "model"), ("reasoning_effort", "model_reasoning_effort")):
        value.setdefault(key, inherited.get(source))
        if value[key] is not None and (not isinstance(value[key], str) or not value[key]):
            raise ValueError(f"Invalid {key}")
    overrides = value.get("role_overrides", {})
    if not isinstance(overrides, dict) or set(overrides) - set(ROLES):
        raise ValueError("Model overrides must name existing roles")
    for role, choices in overrides.items():
        if (not isinstance(choices, dict) or set(choices) - {"model", "reasoning_effort"}
                or any(not isinstance(v, str) or not v.strip() for v in choices.values())):
            raise ValueError(f"Only nonempty model/reasoning choices may be overridden: {role}")
    context = []
    for name in value.pop("context_files", []):
        file = Path(name).resolve()
        data = file.read_bytes()
        if len(data) > 256_000:
            raise ValueError("Each explicit context file must be at most 256 KB")
        context.append({"path": str(file), "sha256": hashlib.sha256(data).hexdigest(), "text": data.decode("utf-8-sig")})
    if sum(len(item["text"]) for item in context) > 512_000:
        raise ValueError("Explicit context is limited to 512 KB")
    return value, context


def prepare(ledger, registry, session, config, output, execution_config=None):
    root = Path(output).resolve()
    ledger, registry, config = (Path(p).resolve() for p in (ledger, registry, config))
    if not ledger.is_file() or not registry.is_file():
        raise ValueError("Existing ledger and scientific registry required; never reset history")
    # Read-only discovery; even a failed startup must not create a new session.
    with sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True) as db:
        row = db.execute("SELECT spec FROM team_host_sessions WHERE session_id=?", (session,)).fetchone()
        seed_authors = set()
        if row:
            seeds = set(json.loads(row[0]).get("seed_tasks", []))
            seed_authors = {owner for task_id, owner in db.execute("SELECT task_id,owner FROM team_tasks WHERE state='challenged'") if task_id in seeds}
    if row is None:
        raise ValueError("Initialize an explicitly bounded host session first")
    spec = json.loads(row[0])
    # Services resume the frozen value, never reload a mutable external file.
    execution = read(execution_config) if isinstance(execution_config, (str, Path)) else execution_config
    if execution_config is not None and (not isinstance(execution, dict)
            or not isinstance(execution.get("research"), dict) or not isinstance(execution.get("compute"), dict)):
        raise ValueError("Execution config must contain research and compute settings")
    binding = {"ledger": str(ledger), "registry": str(registry), "session": session,
               "spec": spec, "config_hash": digest(read(config)), "execution_config": execution}
    previous = read(root / "manifest.json")
    if previous:
        if previous["binding"] != binding:
            raise ValueError("Runtime binding changed; reuse original bounds/config/session on recovery")
        return root, previous
    workers, context = worker_config(config)
    value = {"binding": binding, "workers": workers, "context": context,
             "active_roles": [role for role in ROLES if role in set(active_roles(spec)) | seed_authors],
             "deadline_epoch": timestamp(spec["deadline"]).timestamp(), "runtime_id": uuid.uuid4().hex}
    write(root / "manifest.json", value)
    return root, value


def coordinator(root, manifest):
    from ..registry import ResearchRegistry
    from .coordinator import Coordinator
    from .host import ExistingExecutor, Host, MailboxAgents
    binding = manifest["binding"]
    # This lock also excludes a second output directory targeting the same ledger.
    with lease(binding["ledger"] + ".host-writer.lock"):
        c, registry = Coordinator(binding["ledger"]), ResearchRegistry(binding["registry"])
        try:
            executor = None
            def check_active():
                if not active(root, manifest):
                    raise InterruptedError("Host paused or original deadline reached")
            if binding["execution_config"] is not None:
                from ..backends import create_backend
                config = binding["execution_config"]
                executor = ExistingExecutor(c, registry, create_backend(config.get("platform", {}), before_call=check_active),
                                            config, root / "results", before_remote=check_active)
            host = Host(c, registry, binding["session"])
            agents = MailboxAgents(root / "mailbox")
            while active(root, manifest):
                result = host.run(agents, executor, should_continue=lambda: active(root, manifest))
                write(root / "host-status.json", result)
                if result.get("status") == "paused":
                    return result
                if result.get("reason") not in {"role_response", "original_run", "local_screen_required"}:
                    return dict(result, terminal=True)
                time.sleep(manifest["workers"]["poll_interval"])
            return {"status": "paused"}
        finally:
            c.close()
            registry.close()


def component(root, name, launch):
    manifest = read(root / "manifest.json")
    folder = root / "components" / name
    with lease(folder / "process.lock"):
        # Self-register before consuming any work. Parent can die before Popen returns.
        write(folder / "process.json", {"launch": launch, "process": identity()})
        try:
            if name == "coordinator":
                result = coordinator(root, manifest)
            else:
                from .codex_worker import worker
                result = worker(root, manifest, name)
        except Exception as exc:
            # No arbitrary exception text in shared status (could include account data).
            result = {"status": "attention", "reason": "component_error", "error_type": type(exc).__name__}
            import traceback
            traceback.print_exc()  # Local private component log only.
        write(folder / "result.json", dict(result, launch=launch))


def supervise(root, manifest, stop_event=None):
    config = manifest["workers"]
    state_path = root / "supervisor.json"
    state = read(state_path, {"components": {name: {"launches": []} for name in COMPONENTS}})
    if state.get("status") in {"complete", "attention"}:
        return state  # Retry limits/terminal failures survive restarts.
    children = {}
    result = None
    def persist():
        write(state_path, state)
    try:
        while time.time() < manifest["deadline_epoch"]:
            if stop_event is not None and stop_event.is_set():
                result = {"status": "paused", "reason": "service_stop"}
                break
            write(root / "control.json", {"run": True, "heartbeat": time.time()})
            state.update(status="running", process=identity())
            persist()
            # Start the sole coordinator first; workers cannot spend or mutate DBs.
            for name in ("coordinator", *manifest.get("active_roles", ROLES)):
                record = state["components"][name]
                folder = root / "components" / name
                launch = record["launches"][-1] if record["launches"] else None
                receipt = read(folder / "process.json", {})
                component_result = read(folder / "result.json", {})
                if launch and component_result.get("launch") == launch["id"]:
                    if component_result.get("terminal") or component_result.get("status") == "attention":
                        result = component_result
                        break
                if launch and receipt.get("launch") == launch["id"] and owned(receipt.get("process")):
                    continue
                if launch and receipt.get("launch") != launch["id"]:
                    if time.time() - launch["started"] < 10:
                        continue
                    result = {"status": "attention", "reason": "ambiguous_component_launch", "component": name}
                    break
                if len(record["launches"]) > config["max_component_restarts"]:
                    result = {"status": "attention", "reason": "component_restart_limit", "component": name}
                    break
                if launch and time.time() - launch["started"] < config["retry_backoff"]:
                    continue
                launch = {"id": uuid.uuid4().hex, "started": time.time()}
                record["launches"].append(launch)
                persist()
                folder.mkdir(parents=True, exist_ok=True)
                with (folder / (launch["id"] + ".log")).open("ab") as log:
                    children[name] = subprocess.Popen([sys.executable, "-m", "panda_alpha.team.runtime",
                        "--root", str(root), "--component", name, "--launch", launch["id"]],
                        stdin=subprocess.DEVNULL, stdout=log, stderr=log, shell=False,
                        cwd=Path(__file__).resolve().parents[2], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            for child in children.values():
                child.poll()  # Reap dead children on POSIX.
            if result:
                break
            time.sleep(config["poll_interval"])
        if result is None:
            result = {"status": "stopped", "reason": "deadline"}
        status = result.get("status")
        state.update(status=status if status in {"attention", "paused"} else "complete", result=result)
    except KeyboardInterrupt:
        state.update(status="paused", result={"status": "paused", "reason": "operator_interrupt"})
    finally:
        write(root / "control.json", {"run": False, "heartbeat": time.time()})
        # Include adopted children from a previous supervisor, using their birth receipts.
        until = time.time() + 5
        while time.time() < until:
            alive = [read(root / "components" / n / "process.json", {}).get("process") for n in COMPONENTS]
            if not any(owned(p) for p in alive):
                break
            for child in children.values():
                child.poll()
            time.sleep(.1)
        for name in COMPONENTS:
            terminate(read(root / "components" / name / "process.json", {}).get("process"))
        # A killed worker may have an adopted model process outside its process tree.
        for path in (root / "workers").glob("*/work/*/state.json"):
            attempts = read(path)["attempts"]
            if attempts:
                terminate(attempts[-1].get("process"))
        for child in children.values():
            child.wait(timeout=5)
        persist()
    return state


def active_roles(spec):
    """Legacy deployments keep four roles; lean only launches usable roles."""
    if spec.get("workflow") != "lean":
        return ROLES
    if spec.get("independent_source_review"):
        return ("market", "fundamental", "skeptic")
    return (spec.get("researcher", "market"), "skeptic")


def serve(ledger, registry, session, workers, output, execution_config=None, stop_event=None):
    root = Path(output).resolve()
    with ExitStack() as stack:
        stack.enter_context(lease(root / "supervisor.lock"))
        # Acquire a separate ledger admission lease before creating a second runtime.
        stack.enter_context(lease(str(Path(ledger).resolve()) + ".host-service.lock"))
        root, manifest = prepare(ledger, registry, session, workers, root, execution_config)
        return supervise(root, manifest, stop_event)


def retry_attention(output, reason):
    """Operator retry after restoring login/quota; never resets attempt budgets."""
    root = Path(output).resolve()
    if not reason.strip():
        raise ValueError("Record why the model login/quota problem has been resolved")
    with lease(root / "supervisor.lock"):
        manifest = read(root / "manifest.json")
        state = read(root / "supervisor.json")
        if state.get("status") != "attention" or state["result"].get("reason") not in {"model_authentication", "model_quota"}:
            raise ValueError("Only a repaired model authentication/quota failure can be retried here")
        if time.time() >= manifest["deadline_epoch"]:
            raise ValueError("Original session deadline has expired")
        paths = [p for p in (root / "workers").glob("*/work/*/state.json") if read(p).get("status") == "attention"]
        for path in paths:
            work_state = read(path)
            if work_state.get("reason") not in {"model_authentication", "model_quota"} or len(work_state["attempts"]) >= manifest["workers"]["max_attempts"]:
                raise ValueError("Unreconciled failure or exhausted model attempts")
        # Journal intent first. Repeating an interrupted operator retry is safe.
        audit = {"time": time.time(), "reason": reason, "previous_result": state["result"], "work": [str(p.relative_to(root)) for p in paths]}
        write(root / "operator-retries" / (uuid.uuid4().hex + ".json"), audit)
        for path in paths:
            work_state = read(path)
            work_state.update(status="pending")
            write(path, work_state)
        for name in COMPONENTS:
            path = root / "components" / name / "result.json"
            previous = read(path, {})
            if previous.get("status") == "attention" and previous.get("reason") in {"model_authentication", "model_quota"}:
                write(path, dict(previous, status="retry_authorized"))
        state.update(status="paused", result={"status": "paused", "reason": "operator_model_retry"})
        write(root / "supervisor.json", state)
        return {"status": "paused", "reason": "operator_model_retry", "attempt_budgets_preserved": True}


def main():
    parser = argparse.ArgumentParser(description="Private runtime child entry point")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--component", choices=COMPONENTS, required=True)
    parser.add_argument("--launch", required=True)
    args = parser.parse_args()
    component(args.root, args.component, args.launch)


if __name__ == "__main__":
    main()
