"""把任务推进到下一个有信息价值的动作，并持久化交接与停止原因。

程序负责计划、冻结与结案；模型只负责提案和必要的独立判断。
实际官网调用与累计用量仍由 coordinator 和 ExperimentLedger 管理。
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import tomllib

from .contracts import INFORMATION, ROLES, canonical, digest, text_fields, timestamp, strings, verify_artifacts, validate_request


class Host:
    """One cooperative host writer. Agents only return JSON; they do not own DBs.

    Delivery to an external agent can be repeated after a lost acknowledgement.
    work_id is the backend idempotency key; board effects and recorded responses
    are idempotent. No exactly-once external-model execution claim is made.
    """

    def __init__(self, coordinator, registry, session_id, spec=None):
        self.c, self.registry, self.session_id = coordinator, registry, session_id
        self.db = coordinator.board.db
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS team_host_sessions (
                session_id TEXT PRIMARY KEY, spec TEXT NOT NULL, round INTEGER NOT NULL,
                plan TEXT, actions INTEGER NOT NULL, stop_reason TEXT);
            CREATE TABLE IF NOT EXISTS team_host_work (
                work_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, packet TEXT NOT NULL,
                response TEXT, state TEXT NOT NULL);
        """)
        row = self.db.execute("SELECT spec FROM team_host_sessions WHERE session_id=?", (session_id,)).fetchone()
        if spec is not None:
            text_fields(spec, ("objective", "deadline"))
            timestamp(spec["deadline"])
            for key in ("max_rounds", "max_actions"):
                if type(spec.get(key)) is not int or spec[key] <= 0:
                    raise ValueError(f"Host {key} must be a positive integer")
            if not isinstance(spec.get("seed_tasks", []), list):
                raise ValueError("seed_tasks must be a list of existing task IDs")
            if type(spec.get("seed_only", False)) is not bool or (spec.get("seed_only") and not spec.get("seed_tasks")):
                raise ValueError("seed_only requires a boolean and existing seed tasks")
            if type(spec.get("independent_source_review", False)) is not bool:
                raise ValueError("independent_source_review must be a boolean")
            if spec.get("workflow", "four_role") not in {"four_role", "lean"}:
                raise ValueError("Unknown host workflow")
            if spec.get("workflow") == "lean":
                if spec.get("researcher", "market") not in INFORMATION:
                    raise ValueError("Lean researcher must be market or fundamental")
                if type(spec.get("max_model_attempts")) is not int or spec["max_model_attempts"] <= 0:
                    raise ValueError("Lean workflow requires a positive max_model_attempts")
            for task_id in spec.get("seed_tasks", []):
                self.c.board.task(task_id)
            from .sources import validate_watches
            validate_watches(spec)
            from .data_sources import validate
            validate(spec)
            if row and row[0] != canonical(spec):
                raise ValueError("Host session bounds are immutable; do not reset a session on resume")
            with self.c.board.transaction():
                self.db.execute("INSERT OR IGNORE INTO team_host_sessions VALUES(?,?,0,NULL,0,NULL)", (session_id, canonical(spec)))
        elif row is None:
            raise ValueError("Unknown host session; initialize with an explicit bounded spec")

    def state(self):
        row = dict(self.db.execute("SELECT * FROM team_host_sessions WHERE session_id=?", (self.session_id,)).fetchone())
        row["spec"] = json.loads(row["spec"])
        row["plan"] = json.loads(row["plan"]) if row["plan"] else None
        return row

    def owned_tasks(self):
        packets = [json.loads(r[0]) for r in self.db.execute(
            "SELECT packet FROM team_host_work WHERE session_id=?", (self.session_id,))]
        return {p["task_id"] for p in packets if p["action"] == "propose"} | set(self.state()["spec"].get("seed_tasks", []))

    def stop(self, reason):
        with self.c.board.transaction():
            self.db.execute("UPDATE team_host_sessions SET stop_reason=? WHERE session_id=?", (reason, self.session_id))
        return {"status": "stopped", "reason": reason, "session_id": self.session_id}

    def _issue(self, state, role, action, task_id=None, **extra):
        if state["spec"].get("workflow") == "lean" and state["actions"] >= state["spec"]["max_actions"]:
            return self.stop("action_limit")
        task = self.c.board.task_packet(task_id) if task_id and action != "propose" else None
        identity = {"session_id": self.session_id, "round": state["round"], "role": role, "action": action,
                    "task_id": task_id, "version": task["version"] if task else None}
        rejected = [json.loads(r[0]) for r in self.db.execute(
            "SELECT packet FROM team_host_work WHERE session_id=? AND state IN ('rejected','stale')", (self.session_id,))]
        prior = [p for p in rejected if all(p[k] == v for k, v in identity.items())]
        identity["attempt"] = len(prior) + 1
        work_id = "host-" + digest(identity)
        packet = dict(identity, work_id=work_id, task=task, objective=state["spec"]["objective"],
                      memory=self.registry.reconciled_memory(), **extra)
        if action == "freeze":
            # A source reopen or rejected reply can move required controls
            # outside task_packet's recent-event window. Every old control
            # still requires an explicit disposition on this same request.
            packet["cross_line_reviews"] = [e for e in self.c.board.events(task_id)
                                             if e["kind"] == "cross_review"]
        if action == "plan_round":
            # Unexecuted controls have no scientific result/conclusion event.
            # Their completed design review must still reach the lead's summary.
            outcomes = []
            for selected in self.c.board.tasks():
                if selected["task_id"] not in self.owned_tasks():
                    continue
                events = self.c.board.events(selected["task_id"])
                reviews = [e for e in events if e["kind"] in {"review", "result_review"}]
                decisions = [e for e in events if e["kind"] in {"decision", "conclusion"}]
                def summary(event):
                    payload = event["payload"]
                    return {"event_id": event["event_id"], "kind": event["kind"],
                            **{k: payload[k] for k in ("decision", "claim", "reason", "artifacts") if k in payload},
                            "assessment": {k: payload.get("assessment", {}).get(k)
                                           for k in ("scope", "source_status", "economic_status")}}
                outcomes.append({"task_id": selected["task_id"], "state": selected["state"],
                                 "version": selected["version"], "request_hash": selected["request_hash"],
                                 "latest_review": summary(reviews[-1]) if reviews else None,
                                 "latest_decision": summary(decisions[-1]) if decisions else None})
            packet["task_outcomes"] = outcomes
        from .sources import material
        packet["source_material"] = material(self.c.board, {task_id} if task else self.owned_tasks())
        if state["spec"].get("data_requests"):
            from .data_sources import material as data_material
            packet["data_material"] = data_material(self.c.board, self.session_id, state["spec"])
        if task and action == "result_review":
            from .evidence import read_artifact
            result = task["review_targets"]["result"]
            binding = result["payload"].get("evidence_contract") if result else None
            if result and not binding:
                # Older reconciled Runs predate typed report observations. Build
                # a supplemental cache-only snapshot, never edit their event or
                # repeat the website execution to obtain the newer schema.
                root = Path(self.db.execute("PRAGMA database_list").fetchone()[2]).resolve().parent
                binding = self.c.evidence(task_id, root / "source-material" / (digest(result["payload"]) + ".evidence-v2.json"))
            if binding:
                packet["delivery_observation"] = {"artifact": binding, "observation": read_artifact(binding)}
        if prior:
            packet["previous_response"] = json.loads(self.db.execute(
                "SELECT response FROM team_host_work WHERE work_id=?", (prior[-1]["work_id"],)).fetchone()[0] or "null")
            error = self.db.execute("SELECT payload FROM team_events WHERE event_id=?", (prior[-1]["work_id"] + ":invalid",)).fetchone()
            packet["previous_error"] = json.loads(error[0])["error"] if error else "stale task version"
        if role:
            packet["role_packet"] = self.c.board.packet(role)
            if action == "propose":
                # Sequential delivery must not expose the first author's idea
                # to the second author before their own proposal is committed.
                peers = {f"{self.session_id}.r{state['round']}.{a['owner']}"
                         for a in (state["plan"] or {}).get("assignments", []) if a["owner"] != role}
                packet["role_packet"]["other_line"] = [p for p in packet["role_packet"]["other_line"]
                                                       if p["task_id"] not in peers]
                packet["role_packet"]["withheld_current_round_peer_tasks"] = sorted(peers)
            # Public defaults are packaged with the framework. Application-specific
            # instructions must be explicitly supplied in the immutable session spec.
            defaults = json.loads(Path(__file__).with_name("roles.json").read_text(encoding="utf-8"))
            packet["role_instructions"] = state["spec"].get("role_instructions", {}).get(role, defaults[role])
        packet["reply_contract"] = {
            "envelope": "{work_id: exact work_id, output: action-specific JSON}",
            "plan_round": "decision=research|stop, reason; research supplies one brief per market/fundamental in assignments; optional reopen list binds existing seed tasks",
            "propose": "request with the assigned task_id/owner, or decision=no_candidate and reason; never invent evidence",
            "challenge_response_review": "claim/test/decision/artifacts; result_review adds scoped assessment and evidence_contract; host binds IDs/version/reply_to",
            "cross_review": "claim/test/decision/artifacts; compare peer brief and propose a discriminating control",
            "source_analysis": "claim/test, decision=analysis_only, artifacts, resolved/pending/limitations string lists; no qualification changes",
            "freeze": "priority/allocation_reason/diversity_check/cross_line_decisions; each cross_line_decisions row requires event_id=exact task cross_review event ID, decision=accept|reject, and nonempty reason; resolve every cross_review, or decision=park with reason",
            "conclude": "decision/reason agreeing with independent review; host supplies stable operation and review IDs"}
        if action == "source_analysis":
            # First opinions contain evidence and frozen definitions, not earlier
            # verdicts, peer notes, or reconciled scientific-memory conclusions.
            packet["task"] = {k: task[k] for k in ("task_id", "owner", "request", "request_hash", "fingerprint", "version", "state")}
            packet["memory"] = {}
            packet["role_packet"] = {"role": role, "funding_authorization": False}
        elif task and action in {"review", "result_review", "conclude"}:
            from .sources import receipts
            active_sources = {r["event_id"] for r in receipts(self.c.board, task_id)
                              if r["status"] in {"review_requested", "awaiting_conclusion"}}
            packet["independent_source_analyses"] = [e for e in self.c.board.events(task_id)
                if e["kind"] == "source_analysis" and set(e["payload"]["source_receipt_ids"]) == active_sources]
        if state["spec"].get("workflow") == "lean":
            packet["context_policy"] = "task_scoped_v1"
            if task:
                screens = [e for e in self.c.board.events(task_id) if e["kind"] == "local_screen"]
                if screens and "local_screen_observation" not in packet:
                    from .evidence import read_artifact
                    report = read_artifact(screens[-1]["payload"]["report"])
                    packet["local_screen_observation"] = {"artifact": screens[-1]["payload"]["report"],
                        **{k: report[k] for k in ("route", "local_status", "reason", "scope", "quality") if k in report}}
            packet["workflow_instructions"] = (
                "One researcher and an on-demand independent skeptic. Do not request ceremonial lead/cross-line turns. "
                "Routine scheduling, freeze and exact review-conclusion mapping are deterministic host actions. "
                "A proposal-mode review judges the frozen proposal and supplied local screen in one turn; "
                "approve or park with concrete reasons, without inventing an author response. "
                "Keep replies concise; preserve source/PIT, cost, exposed-window and pool-increment limitations.")
        with self.c.board.transaction():
            old = self.db.execute("SELECT state FROM team_host_work WHERE work_id=?", (work_id,)).fetchone()
            if old:
                raise ValueError("Completed host action cannot be reissued without a state change")
            self.db.execute("INSERT INTO team_host_work VALUES(?,?,?,NULL,'pending')", (work_id, self.session_id, canonical(packet)))
            self.db.execute("UPDATE team_host_sessions SET actions=actions+1 WHERE session_id=?", (self.session_id,))
        return {"status": "work", "packet": packet}

    def next(self, *, execution=False, should_continue=None):
        self.c.recover_conclusions(self.registry)
        state = self.state()
        if state["stop_reason"]:
            return {"status": "stopped", "reason": state["stop_reason"], "session_id": self.session_id}
        if datetime.now(timezone.utc) >= timestamp(state["spec"]["deadline"]):
            return self.stop("deadline")
        if state["spec"].get("data_requests"):
            from .data_sources import refresh
            try:
                captures = refresh(self.c.board, self.session_id, state["spec"], should_continue)
            except InterruptedError:
                return {"status": "paused", "reason": "operator_stop", "session_id": self.session_id}
            if any(value["status"] != "captured" for value in captures):
                return {"status": "waiting", "reason": "data_source_failed",
                        "session_id": self.session_id,
                        "errors": [value.get("error") for value in captures if value["status"] != "captured"]}
        # Finish recorded answers before creating work or checking action count.
        for row in self.db.execute("SELECT * FROM team_host_work WHERE session_id=? AND state IN ('pending','answered') ORDER BY rowid", (self.session_id,)).fetchall():
            row = dict(row)
            packet = json.loads(row["packet"])
            if row["state"] == "answered":
                self._apply_recorded(packet, json.loads(row["response"]))
                return self.next(execution=execution, should_continue=should_continue)
            if packet["action"] == "dispatch" and any(j["state"] not in {"SETTLED", "CANCELLED"} for j in self.c.ledger.jobs()):
                # Another task may have obtained the single inflight slot since
                # this work was issued. Do not pin a failing dispatch ahead of
                # the original Run that must be reconciled to release it.
                self.db.execute("UPDATE team_host_work SET state='stale' WHERE work_id=?", (row["work_id"],))
                continue
            if packet["action"] == "freeze" and any(t["owner"] == packet["task"]["owner"] and
                    t["state"] == "ready" and t["task_id"] != packet["task_id"] for t in self.c.board.tasks()):
                self.db.execute("UPDATE team_host_work SET state='stale' WHERE work_id=?", (row["work_id"],))
                continue
            if packet["task"] and self.c.board.task(packet["task_id"])["version"] != packet["version"]:
                self.db.execute("UPDATE team_host_work SET state='stale' WHERE work_id=?", (row["work_id"],))
                continue
            return {"status": "work", "packet": packet}
        state = self.state()
        if state["actions"] >= state["spec"]["max_actions"] and state["spec"].get("workflow") != "lean":
            return self.stop("action_limit")
        # External intake can append material while a role is running. Finish
        # its immutable packet first, then reopen only explicitly owned tasks.
        from .sources import apply_pending, refresh_watches
        refresh_watches(self.c.board, self.session_id, state["spec"])
        if datetime.now(timezone.utc) >= timestamp(state["spec"]["deadline"]):
            return self.stop("deadline")
        apply_pending(self.c.board, self.owned_tasks())
        if state["spec"].get("independent_source_review"):
            # Finish all blind opinions before exposing any cross-role synthesis.
            from .sources import receipts
            for task_id in sorted(self.owned_tasks()):
                active_sources = sorted(r["event_id"] for r in receipts(self.c.board, task_id)
                                        if r["status"] == "review_requested")
                if not active_sources:
                    continue
                notes = [e for e in self.c.board.events(task_id) if e["kind"] == "source_analysis"
                         and e["payload"]["source_receipt_ids"] == active_sources]
                for role in ("market", "fundamental", "skeptic"):
                    if not any(e["actor"] == role for e in notes):
                        return self._issue(state, role, "source_analysis", task_id, source_receipt_ids=active_sources)
        assignments = (state["plan"] or {}).get("assignments", [])
        current_ids = [f"{self.session_id}.r{state['round']}.{a['owner']}" for a in assignments]
        selected = set(current_ids) | self.owned_tasks()
        tasks = {t["task_id"]: t for t in self.c.board.tasks()}
        # A frozen local configuration can run the existing study engine even
        # in an offline host. It never enables a remote executor or a grant.
        if state["spec"].get("local_screen_config"):
            from .evidence import read_artifact
            from .local_screen import local_first
            local_config = read_artifact(state["spec"]["local_screen_config"])
            if local_config.get("local_screen", {}).get("enabled") is not True:
                raise ValueError("Bound local-screen configuration must enable local-first routing")
            for task_id in sorted(selected):
                if task_id in tasks and tasks[task_id]["state"] == "ready" and self.c.ledger.get(tasks[task_id]["fingerprint"]) is None:
                    try:
                        self.c.check_research(tasks[task_id], local_config, self.registry)
                        local_first(self.c.board, tasks[task_id], local_config, self.registry,
                                    require_semantics=state["spec"].get("workflow") == "lean")
                    except ValueError as error:
                        return {"status": "waiting", "reason": "local_screen_blocked", "task_id": task_id, "error": str(error)}
        for assignment, task_id in zip(assignments, current_ids):
            completed = any(json.loads(r[0])["task_id"] == task_id and json.loads(r[0])["action"] == "propose"
                            for r in self.db.execute("SELECT packet FROM team_host_work WHERE session_id=? AND state='applied'", (self.session_id,)))
            if task_id not in tasks and not completed:
                return self._issue(state, assignment["owner"], "propose", task_id, brief=assignment["brief"])
        work = [w for w in self.c.plan()["work"] if w["task_id"] in selected]
        existing_work = sorted(work, key=lambda w: self.c.ledger.get(tasks[w["task_id"]]["fingerprint"]) is None)
        inflight = [j for j in self.c.ledger.jobs() if j["state"] not in {"SETTLED", "CANCELLED"}]
        selected_fingerprints = {tasks[w["task_id"]]["fingerprint"] for w in work}
        for item in existing_work:
            action, task_id = item["action"], item["task_id"]
            if action in {"await_grant_or_dispatch", "resume_original_run"}:
                continue
            task = tasks[task_id]
            if state["spec"].get("workflow") == "lean":
                if action == "challenge":
                    screened = self._lean_screen(state, task)
                    if screened.get("status") == "progress":
                        return self.next(execution=execution, should_continue=should_continue)
                    if screened.get("status"):
                        return screened
                    return self._issue(state, "skeptic", "review", task_id, review_mode="proposal",
                                       local_screen_observation=screened)
                if action == "review" and not any(e["kind"] == "response" for e in self.c.board.events(task_id)):
                    return self._issue(state, "skeptic", "review", task_id, review_mode="proposal")
                if action == "conclude":
                    review = self.c.board.task_packet(task_id)["review_targets"]["result_review"]
                    self.c.conclude(task_id, task["version"], {
                        "operation_id": "lean-conclude:" + digest({"session": self.session_id, "review": review["event_id"]}),
                        "review_event_id": review["event_id"],
                        "decision": "close" if review["payload"]["decision"] == "reject" else review["payload"]["decision"],
                        "reason": review["payload"]["claim"], "decider": "deterministic_host"}, self.registry)
                    return self.next(execution=execution, should_continue=should_continue)
            if action == "freeze":
                if any(t["owner"] == task["owner"] and t["state"] == "ready" for t in tasks.values()):
                    continue  # Free the existing ready slot before asking for an impossible freeze.
                cross = [e for e in self.c.board.events(task_id) if e["kind"] == "cross_review"]
                if state["spec"].get("workflow") == "lean" and not cross:
                    review = [e for e in self.c.board.events(task_id) if e["kind"] == "review"][-1]
                    self.c.board.freeze(task_id, task["version"], {"request_hash": task["request_hash"], "priority": 50,
                        "allocation_reason": "Independent design approval; execution still requires local/source/funding gates",
                        "diversity_check": "No pool-increment or independence claim from design approval",
                        "review_event_id": review["event_id"], "decider": "deterministic_host", "cross_line_decisions": []},
                        event_id="lean-freeze:" + digest({"session": self.session_id, "review": review["event_id"]}))
                    return self.next(execution=execution, should_continue=should_continue)
                if state["spec"].get("workflow") == "lean":
                    # Existing explicit cross-line objections still need an actual disposition.
                    return {"status": "waiting", "reason": "cross_line_disposition_required", "task_id": task_id}
                peers = [t for t in reversed(list(tasks.values())) if t["owner"] != task["owner"]]
                peer = next((t for t in peers if
                             set(t["request"]["risk_exposures"]) & set(task["request"]["risk_exposures"])),
                            peers[0] if peers else None)
                if peer and not any(e["kind"] == "cross_review" for e in self.c.board.events(task_id)):
                    return self._issue(state, peer["owner"], "cross_review", task_id,
                                       peer_task_id=peer["task_id"], peer_request_hash=peer["request_hash"])
            return self._issue(state, item["role"], action, task_id)
        if any(j["fingerprint"] not in selected_fingerprints for j in inflight):
            return {"status": "waiting", "reason": "other_job_reconciliation", "fingerprints": [j["fingerprint"] for j in inflight]}
        for item in existing_work:
            if item["action"] not in {"await_grant_or_dispatch", "resume_original_run"}:
                continue
            task = tasks[item["task_id"]]
            job = self.c.ledger.get(task["fingerprint"])
            if job and not job["run_id"]:
                return {"status": "waiting", "reason": "operator_reconciliation", "task_id": task["task_id"], "job_state": job["state"]}
            if state["spec"].get("workflow") == "lean" and not job:
                screened = self._lean_screen(state, task)
                if screened.get("status") == "progress":
                    return self.next(execution=execution, should_continue=should_continue)
                if screened.get("status"):
                    return screened
            if execution:
                action = "resume" if job else "dispatch"
                return self._issue(state, None, action, task["task_id"])
        if work:
            return {"status": "waiting", "reason": "execution_not_enabled", "tasks": [w["task_id"] for w in work]}
        if state["spec"].get("seed_only"):
            return self.stop("seed_tasks_complete")
        if state["round"] >= state["spec"]["max_rounds"]:
            return self.stop("round_limit")
        if state["spec"].get("workflow") == "lean":
            owner = state["spec"].get("researcher", "market")
            plan = {"decision": "research", "reason": "Frozen single-researcher objective",
                    "assignments": [{"owner": owner, "brief": state["spec"]["objective"]}]}
            with self.c.board.transaction():
                self.db.execute("UPDATE team_host_sessions SET round=?,plan=? WHERE session_id=?",
                                (state["round"] + 1, canonical(plan), self.session_id))
            return self.next(execution=execution, should_continue=should_continue)
        return self._issue(state, "lead", "plan_round", next_round=state["round"] + 1)

    def _lean_screen(self, state, task):
        """Cheap fixed-definition evidence before spending an independent model turn."""
        from .evidence import read_artifact
        from .local_screen import local_first, LocalScreenBlocked
        binding = state["spec"].get("local_screen_config")
        if not binding:
            attachments = [e for e in self.c.board.events(task["task_id"]) if e["kind"] == "local_screen_bound"]
            binding = attachments[-1]["payload"]["config"] if attachments else None
        if not binding:
            return {"status": "waiting", "reason": "local_screen_required", "task_id": task["task_id"]}
        try:
            config = read_artifact(binding)
            if config.get("local_screen", {}).get("enabled") is not True:
                raise ValueError("Lean workflow requires local-first routing")
            self.c.check_research(task, config, self.registry)
            local_first(self.c.board, task, config, self.registry, require_semantics=True)
        except LocalScreenBlocked as error:
            events = [e for e in self.c.board.events(task["task_id"]) if e["kind"] == "local_screen"]
            if str(error) == "Local-first route: local_reject" and events and events[-1]["payload"]["route"] == "local_reject":
                # This is a scoped fixed-definition screen, never a global mechanism rejection.
                self.c.board.decide(task["task_id"], task["version"], {
                    "decision": "park", "reason": str(error), "decider": "deterministic_host",
                    "local_report": events[-1]["payload"]["report"]},
                    event_id="lean-local-stop:" + digest({"session": self.session_id, "screen": events[-1]["event_id"], "version": task["version"]}))
                return {"status": "progress", "reason": "local_candidate_parked", "task_id": task["task_id"]}
            return {"status": "waiting", "reason": "local_screen_blocked", "task_id": task["task_id"], "error": str(error)}
        except ValueError as error:
            return {"status": "waiting", "reason": "local_screen_blocked", "task_id": task["task_id"], "error": str(error)}
        event = [e for e in self.c.board.events(task["task_id"]) if e["kind"] == "local_screen"][-1]
        report = read_artifact(event["payload"]["report"])
        return {"artifact": event["payload"]["report"], **{k: report[k] for k in
                ("route", "local_status", "reason", "scope", "quality") if k in report}}

    def respond(self, work_id, response):
        if response.get("work_id") != work_id or not isinstance(response.get("output"), dict):
            raise ValueError("Agent response must bind its work_id and JSON output")
        row = self.db.execute("SELECT * FROM team_host_work WHERE work_id=? AND session_id=?", (work_id, self.session_id)).fetchone()
        if row is None:
            raise ValueError("Unknown host work")
        row = dict(row)
        if row["response"] and row["response"] != canonical(response):
            raise ValueError("Host response identity already has different content")
        if row["state"] == "applied":
            return {"status": "applied", "work_id": work_id}
        if row["state"] in {"stale", "rejected"}:
            raise ValueError("Stale host work; request the current packet")
        state = self.state()
        if state["stop_reason"] or datetime.now(timezone.utc) >= timestamp(state["spec"]["deadline"]):
            raise ValueError("Host session stopped or expired")
        with self.c.board.transaction():
            self.db.execute("UPDATE team_host_work SET response=?,state='answered' WHERE work_id=?", (canonical(response), work_id))
        self._apply_recorded(json.loads(row["packet"]), response)
        return {"status": "applied", "work_id": work_id}

    def _apply_recorded(self, packet, response):
        try:
            self._apply(packet, response)
        except (ValueError, KeyError, TypeError) as exc:
            # Keep the invalid answer as evidence; a corrected response gets a
            # fresh bounded delivery ID and sees the precise validation error.
            # A partially committed conclusion must recover its original intent.
            finalization = self.db.execute("SELECT 1 FROM team_finalizations WHERE operation_id=?", (packet["work_id"],)).fetchone()
            if finalization:
                raise
            with self.c.board.transaction():
                self.db.execute("UPDATE team_host_work SET state='rejected' WHERE work_id=?", (packet["work_id"],))
                self.c.board.event(packet["task_id"] or "*", "host", "invalid_response",
                    {"work_id": packet["work_id"], "error": str(exc)}, packet["work_id"] + ":invalid")
            raise

    def _apply(self, p, response):
        out, action, task_id = response["output"], p["action"], p["task_id"]
        if action == "plan_round":
            text_fields(out, ("decision", "reason"))
            if out["decision"] == "stop":
                self.stop(out["reason"])
            elif out["decision"] == "research":
                assignments = out.get("assignments", [])
                if not isinstance(assignments, list) or any(not isinstance(a, dict) for a in assignments):
                    raise ValueError("Assignments must be a list of research brief objects")
                if sorted(a.get("owner", "") for a in assignments) != sorted(INFORMATION):
                    raise ValueError("A round assigns distinct market and fundamental briefs")
                for a in assignments:
                    text_fields(a, ("brief",))
                reopens = out.get("reopen", [])
                if not isinstance(reopens, list) or any(not isinstance(r, dict) for r in reopens):
                    raise ValueError("Reopen must be a list of task evidence requests")
                for r in reopens:
                    if r["task_id"] not in self.owned_tasks():
                        raise ValueError("Re-review must name an explicitly selected seed task")
                    self.c.board.reopen_evidence(r["task_id"], r["version"], r["request"])
                with self.c.board.transaction():
                    self.db.execute("UPDATE team_host_sessions SET round=?,plan=? WHERE session_id=?",
                                    (p["next_round"], canonical(out), self.session_id))
            else:
                raise ValueError("Plan decision is research or stop")
        elif action == "propose":
            if out.get("decision") == "no_candidate":
                text_fields(out, ("reason",))
                if self.state()["spec"].get("workflow") == "lean":
                    self.stop("no_informative_candidate")
            else:
                request = out["request"]
                if request["owner"] != p["role"] or request["task_id"] != task_id:
                    raise ValueError("Proposal must use its assigned role and task identity")
                # Report a contract error before dereferencing candidate/mechanism;
                # otherwise models spend successive turns discovering opaque KeyErrors.
                validate_request(request)
                from ..evolution import Candidate, ResearchEngine
                engine = ResearchEngine(registry=self.registry, memory=self.registry.reconciled_memory(),
                    research_context={k: request[k] for k in ("window", "cycle", "groups")})
                candidate = Candidate.from_dict(dict(request["candidate"], hypothesis=request["hypothesis"], mechanism=request["mechanism_id"]))
                if not engine.allows_research(candidate):
                    raise ValueError("Scientific memory blocks this proposal; use a different mechanism or explicit evidence-reopening path")
                self.c.board.submit(request)
        elif action == "freeze":
            if out.get("decision") == "park":
                self.c.board.decide(task_id, p["version"], out, event_id=p["work_id"])
            else:
                self.c.board.freeze(task_id, p["version"], dict(out, request_hash=p["task"]["request_hash"]), event_id=p["work_id"])
        elif action == "conclude":
            review = p["task"]["review_targets"]["result_review"]
            self.c.conclude(task_id, p["version"], dict(out, operation_id=p["work_id"], review_event_id=review["event_id"]), self.registry)
        elif action == "source_analysis":
            text_fields(out, ("claim", "test"))
            if out.get("decision") != "analysis_only":
                raise ValueError("Independent source notes are analysis_only, never qualification decisions")
            for key in ("resolved", "pending", "limitations"):
                if out.get(key) != []:
                    strings(out.get(key), key)
            verify_artifacts(out.get("artifacts"))
            expected = {canonical(i["artifact"]) for i in p["source_material"]["items"]}
            if not expected or not expected <= {canonical(a) for a in out["artifacts"]}:
                raise ValueError("Independent source analysis must bind supplied new evidence")
            payload = dict(out, source_receipt_ids=p["source_receipt_ids"], request_hash=p["task"]["request_hash"],
                           version=p["version"], session_id=self.session_id)
            with self.c.board.transaction():
                current = self.c.board.task(task_id)
                if current["version"] != p["version"] or current["request_hash"] != payload["request_hash"]:
                    raise ValueError("Independent source analysis has a stale task binding")
                if not self.c.board._retry(p["work_id"], task_id, "source_analysis", payload):
                    self.c.board.event(task_id, p["role"], "source_analysis", payload, p["work_id"])
        elif action in {"dispatch", "resume"}:
            raise ValueError("Agent responses cannot execute or acknowledge platform work")
        else:
            message = dict(out, event_id=p["work_id"], task_id=task_id, actor=p["role"], kind=action,
                           version=p["version"], request_hash=p["task"]["request_hash"])
            parent_kind = {"response": "challenge", "review": "response", "result_review": "result"}.get(action)
            if action == "review" and p.get("review_mode") == "proposal":
                parent_kind = "proposal"
                message["review_mode"] = "proposal"
            if parent_kind:
                message["reply_to"] = p["task"]["review_targets"][parent_kind]["event_id"]
            if action == "cross_review":
                message.update(peer_task_id=p["peer_task_id"], peer_request_hash=p["peer_request_hash"])
            self.c.board.message(message)
        with self.c.board.transaction():
            self.db.execute("UPDATE team_host_work SET state='applied' WHERE work_id=?", (p["work_id"],))

    def run(self, agents, executor=None, *, should_continue=None):
        """Run until bounded completion or a real wait. No polling/sleep loop."""
        while True:
            if should_continue is not None and not should_continue():
                return {"status": "paused", "reason": "operator_stop"}
            next_work = self.next(execution=executor is not None, should_continue=should_continue)
            if next_work["status"] != "work":
                return next_work
            packet = next_work["packet"]
            if packet["role"] is None:
                if executor is None:
                    return {"status": "waiting", "reason": "execution_not_enabled"}
                try:
                    result = executor(packet["action"], packet["task_id"])
                except InterruptedError:
                    return {"status": "paused", "reason": "operator_stop", "work_id": packet["work_id"]}
                except (ValueError, RuntimeError, TimeoutError) as exc:
                    return {"status": "waiting", "reason": "execution_blocked", "work_id": packet["work_id"], "error": str(exc)}
                task = self.c.board.task(packet["task_id"])
                if task["version"] == packet["version"]:
                    return {"status": "waiting", "reason": "original_run", "result": result}
                self.db.execute("UPDATE team_host_work SET state='applied' WHERE work_id=?", (packet["work_id"],))
                continue
            response = agents(packet)
            if response is None:
                return {"status": "waiting", "reason": "role_response", "work_id": packet["work_id"]}
            try:
                self.respond(packet["work_id"], response)
            except (ValueError, KeyError, TypeError):
                row = self.db.execute("SELECT state FROM team_host_work WHERE work_id=?", (packet["work_id"],)).fetchone()
                if row[0] != "rejected":
                    raise  # A partial conclusion or transport failure needs its original recovery path.
                # Validation recorded the bad answer. Give the role a new,
                # bounded delivery with the specific error, without host repair.
                continue


class CommandAgents:
    """Explicit trusted local transport. No model/provider or payment default.

    Each argv receives one packet on stdin and returns an envelope on stdout.
    Configure a host-owned role adapter, not arbitrary commands from agent text.
    The backend should cache work_id before acknowledging; this cache avoids
    repeat delivery once stdout has been recorded by this process.
    """

    def __init__(self, commands, output, timeout=300):
        if set(commands) != set(ROLES) or any(not isinstance(a, list) or not a or
                any(not isinstance(s, str) or not s for s in a) for a in commands.values()):
            raise ValueError("Configure an explicit argv for each of the four roles")
        self.commands, self.output, self.timeout = commands, Path(output), timeout
        self.output.mkdir(parents=True, exist_ok=True)

    def __call__(self, packet):
        path = self.output / (packet["work_id"] + ".json")
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        result = subprocess.run(self.commands[packet["role"]], input=canonical(packet), capture_output=True,
                                text=True, encoding="utf-8", timeout=self.timeout, shell=False)
        if result.returncode:
            raise RuntimeError("Role transport failed; retained work_id for backend reconciliation")
        value = json.loads(result.stdout)
        if value.get("work_id") != packet["work_id"] or not isinstance(value.get("output"), dict):
            raise ValueError("Role transport returned an unbound response")
        temporary = path.with_suffix(".tmp")
        temporary.write_text(canonical(value), encoding="utf-8")
        temporary.replace(path)
        return value


class MailboxAgents:
    """File handoff for an already running tool host (e.g. Codex role agents).

    The host starts/wakes the actual agents once. They consume their request
    directory and atomically write envelopes to responses/. No model is faked
    or implicitly purchased; absent agents leave an explicit role_response wait.
    """

    def __init__(self, directory):
        self.directory = Path(directory)

    def __call__(self, packet):
        root = self.directory / packet["role"]
        from .runtime_io import read, write
        requests, responses = root / "requests", root / "responses"
        requests.mkdir(parents=True, exist_ok=True)
        responses.mkdir(parents=True, exist_ok=True)
        path = requests / (packet["work_id"] + ".json")
        encoded = canonical(packet)
        previous = read(path)
        if previous is not None and canonical(previous) != encoded:
            raise ValueError("Mailbox work identity already has different content")
        if previous is None:
            write(path, packet)
        response = responses / path.name
        return read(response)


class ExistingExecutor:
    """Only the existing coordinator can reach the platform/compute steward."""

    def __init__(self, coordinator, registry, client, config, output, *, before_remote=None):
        self.c, self.registry, self.client, self.config, self.output = coordinator, registry, client, config, Path(output)
        self.before_remote = before_remote

    def __call__(self, action, task_id):
        if self.before_remote is not None:
            self.before_remote()
        if action == "dispatch":
            result = self.c.dispatch(task_id, self.config, self.client, self.registry, execute=True,
                                     before_remote=self.before_remote)
        elif action == "resume":
            result = self.c.resume(task_id, self.client, self.output)
        else:
            raise ValueError("Unknown execution action")
        return {k: result.get(k) for k in ("state", "run_id", "factor_id", "action")}
