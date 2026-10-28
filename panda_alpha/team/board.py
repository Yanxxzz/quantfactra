"""任务板的唯一可写状态在 SQLite；消息与历史事件只追加。

JSON 工作包是阅读视图，不是第二份任务状态或独立账户余额。
"""
from __future__ import annotations

from contextlib import contextmanager
import json
from uuid import uuid4

from .contracts import INFORMATION, ROLES, canonical, digest, now, text_fields, validate_request, verify_artifacts


class Board:
    def __init__(self, ledger, *, initialize=True):
        self.ledger, self.db = ledger, ledger.db
        if not initialize:
            tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"team_control", "team_tasks", "team_events", "team_grants"}.issubset(tables):
                raise ValueError("Team board is not initialized; use init on the authoritative ledger")
            return
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS team_control (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), mode TEXT NOT NULL, reason TEXT NOT NULL, grant_id TEXT);
            INSERT OR IGNORE INTO team_control VALUES(1,'offline','Formal pilot has not been authorized',NULL);
            CREATE TABLE IF NOT EXISTS team_tasks (
                task_id TEXT PRIMARY KEY, owner TEXT NOT NULL, state TEXT NOT NULL,
                version INTEGER NOT NULL, fingerprint TEXT UNIQUE NOT NULL,
                request_hash TEXT NOT NULL, request TEXT NOT NULL, freeze TEXT);
            CREATE TABLE IF NOT EXISTS team_events (
                event_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, actor TEXT NOT NULL,
                kind TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TRIGGER IF NOT EXISTS team_events_no_update BEFORE UPDATE ON team_events
                BEGIN SELECT RAISE(ABORT, 'team events are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS team_events_no_delete BEFORE DELETE ON team_events
                BEGIN SELECT RAISE(ABORT, 'team events are immutable'); END;
            CREATE TABLE IF NOT EXISTS team_grants (
                grant_id TEXT PRIMARY KEY, grant_hash TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS team_finalizations (
                operation_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, registry_path TEXT NOT NULL,
                payload TEXT NOT NULL, state TEXT NOT NULL);
        """)

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def mode(self):
        return dict(self.db.execute("SELECT mode,reason,grant_id FROM team_control WHERE singleton=1").fetchone())

    def task(self, task_id):
        row = self.db.execute("SELECT * FROM team_tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown task")
        value = dict(row)
        value["request"] = json.loads(value["request"])
        value["freeze"] = json.loads(value["freeze"]) if value["freeze"] else None
        return value

    def tasks(self):
        return [self.task(r[0]) for r in self.db.execute("SELECT task_id FROM team_tasks ORDER BY rowid")]

    def events(self, task_id):
        return [dict(r, payload=json.loads(r["payload"])) for r in self.db.execute(
            "SELECT * FROM team_events WHERE task_id=? ORDER BY rowid", (task_id,))]

    def event(self, task_id, actor, kind, payload, event_id=None):
        event_id = event_id or uuid4().hex
        self.db.execute("INSERT INTO team_events VALUES(?,?,?,?,?,?)",
                        (event_id, task_id, actor, kind, canonical(payload), now()))
        return event_id

    def expect(self, task_id, version, states):
        task = self.task(task_id)
        if task["version"] != version:
            raise ValueError("Stale task version; read the new packet before replying")
        if task["state"] not in states:
            raise ValueError(f"Action is invalid in state {task['state']}")
        return task

    def advance(self, task_id, state):
        self.db.execute("UPDATE team_tasks SET state=?,version=version+1 WHERE task_id=?", (state, task_id))

    def submit(self, request):
        identity = validate_request(request)
        with self.transaction():
            existing = self.db.execute("SELECT task_id,request_hash FROM team_tasks WHERE task_id=? OR fingerprint=?",
                                       (request["task_id"], identity["fingerprint"])).fetchone()
            if existing:
                if existing["request_hash"] != identity["request_hash"]:
                    raise ValueError(f"Duplicate definition or changed task; reference {existing['task_id']}")
                return self.task(existing["task_id"])
            active = [t for t in self.tasks() if t["owner"] == request["owner"] and t["state"] not in {"closed", "parked"}]
            if len(active) >= 2:
                raise ValueError("Researcher WIP limit is two; finish or park an existing task")
            self.db.execute("INSERT INTO team_tasks VALUES(?,?, 'proposed',1,?,?,?,NULL)",
                            (request["task_id"], request["owner"], identity["fingerprint"], identity["request_hash"], canonical(request)))
            self.event(request["task_id"], request["owner"], "proposal", identity)
        return self.task(request["task_id"])

    def message(self, message):
        """Retry is idempotent by event_id; every new reply binds a task version."""
        if "qualifications" in message:
            raise ValueError("Qualification snapshots are computed by the coordinator")
        text_fields(message, ("event_id", "task_id", "actor", "kind", "request_hash", "claim", "test", "decision"))
        actor, kind = message["actor"], message["kind"]
        if actor not in ROLES or kind not in {"challenge", "response", "review", "result_review", "note", "cross_review"}:
            raise ValueError("Unknown role or message kind")
        verify_artifacts(message.get("artifacts", []), required=kind == "result_review")
        with self.transaction():
            prior = self.db.execute("SELECT payload FROM team_events WHERE event_id=?", (message["event_id"],)).fetchone()
            if prior:
                original = json.loads(prior[0])
                original.pop("qualifications", None)
                if canonical(original) != canonical(message):
                    raise ValueError("Event identity already has different content")
                return self.task(message["task_id"])
            task = self.task(message["task_id"])
            if task["version"] != message.get("version") or task["request_hash"] != message["request_hash"]:
                raise ValueError("Stale version or request hash")
            events = self.events(task["task_id"])
            parent = next((e for e in events if e["event_id"] == message.get("reply_to")), None)
            direct = kind == "review" and message.get("review_mode") == "proposal"
            states = {"challenge": {"proposed"}, "response": {"challenged"}, "review": {"proposed", "answered"} if direct else {"answered"},
                      "result_review": {"result_ready"}, "note": {"proposed", "challenged", "answered", "reviewed"},
                      "cross_review": {"reviewed"}}
            if task["state"] not in states[kind]:
                raise ValueError("Message does not match current research stage")
            if kind in {"challenge", "review", "result_review"} and actor != "skeptic":
                raise ValueError("Independent skeptic must challenge and review")
            if kind == "response" and (actor != task["owner"] or not parent or parent["kind"] != "challenge"):
                raise ValueError("Author response must reference the challenge")
            if kind == "review" and (not parent or parent["kind"] != ("proposal" if direct else "response")):
                raise ValueError("Review must reference the frozen proposal" if direct else "Review must reference the author response")
            if kind == "result_review" and (not parent or parent["kind"] != "result"):
                raise ValueError("Result review must reference the reconciled result")
            if kind == "result_review" and (not parent["payload"]["artifacts"] or
                                           parent["payload"]["artifacts"][0] not in message["artifacts"]):
                raise ValueError("Review must bind the original reconciled result artifact")
            if kind == "review" and message["decision"] not in {"approve", "park"}:
                raise ValueError("Review decision is approve or park")
            if kind == "result_review" and message["decision"] not in {"continue", "confirm", "source_pending", "reject"}:
                raise ValueError("Result review requires an explicit next decision")
            if kind == "result_review":
                from .evidence import validate_assessment, assessment_contract, read_artifact
                from ..qualifications import review_state
                job = self.db.execute("SELECT definition FROM jobs WHERE fingerprint=?", (task["fingerprint"],)).fetchone()
                environment = json.loads(job[0] or "{}").get("team_permit", {}).get("environment", "unknown") if job else "unknown"
                validate_assessment(message, task, parent, expected_environment=environment)
                qualified = review_state(task, events, message, read_artifact(assessment_contract(message, parent)))
            if kind == "cross_review":
                if actor not in INFORMATION or actor == task["owner"]:
                    raise ValueError("Cross review requires the other research line")
                peer = self.task(message.get("peer_task_id"))
                if peer["owner"] != actor or peer["request_hash"] != message.get("peer_request_hash"):
                    raise ValueError("Cross review must bind the other line's frozen brief")
            reopened = next((e for e in reversed(events) if e["kind"] == "evidence_review_requested"), None)
            # A completed design-source review must not constrain the first
            # later Run's result review to its old author-response target.
            reopen_done = reopened and any(
                e["kind"] in {"review", "result_review"}
                and e["payload"].get("reply_to") == reopened["payload"]["target_event_id"]
                for e in events[events.index(reopened) + 1:])
            if reopened and not reopen_done and kind in {"review", "result_review"}:
                if message.get("reply_to") != reopened["payload"]["target_event_id"] or any(
                        a not in message["artifacts"] for a in reopened["payload"]["artifacts"]):
                    raise ValueError("Evidence re-review must bind its original target and new artifacts")
            recorded = dict(message, qualifications=qualified) if kind == "result_review" else message
            self.event(task["task_id"], actor, kind, recorded, message["event_id"])
            state = {"challenge": "challenged", "response": "answered", "review": "reviewed",
                     "result_review": "result_reviewed", "note": task["state"], "cross_review": task["state"]}[kind]
            if kind == "review" and message["decision"] == "park":
                state = "parked"
            self.advance(task["task_id"], state)
        return self.task(task["task_id"])

    def freeze(self, task_id, version, decision, *, event_id=None):
        text_fields(decision, ("allocation_reason", "diversity_check"))
        if type(decision.get("priority")) is not int or not 0 <= decision["priority"] <= 100:
            raise ValueError("Priority must be an integer in [0,100]")
        with self.transaction():
            if event_id and self._retry(event_id, task_id, "freeze", decision):
                return self.task(task_id)
            task = self.expect(task_id, version, {"reviewed"})
            if validate_request(task["request"])["request_hash"] != task["request_hash"]:
                raise ValueError("Request changed after review")
            if decision.get("request_hash") != task["request_hash"]:
                raise ValueError("Freeze must bind the reviewed request hash")
            if any(t["owner"] == task["owner"] and t["state"] == "ready" for t in self.tasks()):
                raise ValueError("One ready task per research line")
            cross = [e for e in self.events(task_id) if e["kind"] == "cross_review"]
            dispositions = decision.get("cross_line_decisions", [])
            if not isinstance(dispositions, list) or any(not isinstance(d, dict) for d in dispositions):
                raise ValueError("Cross-line decisions must be a list of objects")
            for e in cross:
                row = next((d for d in dispositions if d.get("event_id") == e["event_id"]), None)
                if not row or row.get("decision") not in {"accept", "reject"} or not isinstance(row.get("reason"), str) or not row["reason"].strip():
                    raise ValueError(f"Freeze must resolve cross-line control {e['event_id']}: expected event_id, "
                                     "decision=accept|reject and nonempty reason; pending controls require parking")
            self.event(task_id, "lead", "freeze", decision, event_id)
            self.db.execute("UPDATE team_tasks SET freeze=? WHERE task_id=?", (canonical(decision), task_id))
            self.advance(task_id, "ready")
        return self.task(task_id)

    def decide(self, task_id, version, decision, *, event_id=None):
        text_fields(decision, ("decision", "reason"))
        if decision["decision"] not in {"close", "park", "continue", "confirm", "source_pending"}:
            raise ValueError("Unknown lead decision")
        with self.transaction():
            if event_id and self._retry(event_id, task_id, "decision", decision):
                return self.task(task_id)
            task = self.expect(task_id, version, {"proposed", "challenged", "answered", "reviewed", "ready", "result_reviewed"})
            if task["state"] == "result_reviewed":
                raise ValueError("Use conclude with the scientific registry for result decisions")
            if task["state"] != "result_reviewed" and decision["decision"] != "park":
                raise ValueError("Only park is available before independent result review")
            self.event(task_id, "lead", "decision", decision, event_id)
            self.advance(task_id, "parked" if decision["decision"] in {"park", "source_pending"} else "closed")
        return self.task(task_id)

    def _retry(self, event_id, task_id, kind, payload):
        prior = self.db.execute("SELECT task_id,kind,payload FROM team_events WHERE event_id=?", (event_id,)).fetchone()
        if prior and tuple(prior) != (task_id, kind, canonical(payload)):
            raise ValueError("Event identity already has different content")
        return prior is not None

    def reopen_evidence(self, task_id, version, request):
        """A source-only re-review never alters identity, execution or authority."""
        text_fields(request, ("operation_id", "request_hash", "prior_decision_event_id", "target_event_id", "claim_delta"))
        verify_artifacts(request.get("artifacts"))
        with self.transaction():
            if self._retry(request["operation_id"], task_id, "evidence_review_requested", request):
                return self.task(task_id)
            task = self.expect(task_id, version, {"parked", "closed"})
            if task["request_hash"] != request["request_hash"]:
                raise ValueError("Evidence cannot change the original request")
            events = self.events(task_id)
            decisions = [e for e in events if e["kind"] in {"decision", "review", "conclusion"}]
            prior = next((e for e in decisions if e["event_id"] == request["prior_decision_event_id"]), None)
            if not prior or prior != decisions[-1]:
                raise ValueError("Re-review must reference the latest closure decision")
            if any(e["kind"] == "result_review" and e["payload"].get("assessment", {}).get("economic_status") == "rejected"
                   for e in events):
                raise ValueError("Source-only review cannot reopen an economic rejection")
            target = next((e for e in events if e["event_id"] == request["target_event_id"]), None)
            results = [e for e in events if e["kind"] == "result"]
            responses = [e for e in events if e["kind"] == "response"]
            kind = "result" if results else "response" if responses else "proposal"
            targets = [e for e in events if e["kind"] == kind]
            if not target or not targets or target != targets[-1]:
                raise ValueError("Re-review requires the original result or latest author response")
            known = {a["sha256"] for e in events for a in e["payload"].get("artifacts", [])}
            known.update(a["sha256"] for a in task["request"]["artifacts"])
            if not any(a["sha256"] not in known for a in request["artifacts"]):
                raise ValueError("Re-review needs materially new evidence, not a renamed artifact")
            if sum(t["owner"] == task["owner"] and t["state"] not in {"closed", "parked"} for t in self.tasks()) >= 2:
                raise ValueError("Researcher WIP limit is two")
            self.event(task_id, "lead", "evidence_review_requested", request, request["operation_id"])
            self.advance(task_id, "result_ready" if kind == "result" else "answered")
        return self.task(task_id)

    def task_packet(self, task_id):
        from ..qualifications import board_state
        task = self.task(task_id)
        events = self.events(task_id)
        # Explicit targets survive a long append-only history; the eight-event
        # reading window must not hide the original Run or reopened response.
        targets = {kind: next((e for e in reversed(events) if e["kind"] == kind), None)
                   for kind in ("proposal", "challenge", "response", "result", "result_review", "evidence_review_requested")}
        return dict(task, events=events[-8:], review_targets=targets, qualifications=board_state(task, events))

    def pause(self, reason):
        if not reason.strip():
            raise ValueError("Pause reason is required")
        with self.transaction():
            self.db.execute("UPDATE team_control SET mode='paused',reason=? WHERE singleton=1", (reason,))
            self.event("*", "lead", "pause", {"reason": reason})

    def packet(self, role):
        if role not in ROLES:
            raise ValueError("Unknown role")
        all_tasks = self.tasks()
        relevant = [t for t in all_tasks if role in {"lead", "skeptic"} or t["owner"] == role]
        finished = [t for t in relevant if t["state"] in {"closed", "parked"}]
        tasks = [t for t in relevant if t["state"] not in {"closed", "parked"}] + finished[-2:]
        control = self.mode()
        row = self.db.execute("SELECT payload FROM team_grants WHERE grant_id=?", (control["grant_id"],)).fetchone()
        grant = json.loads(row[0]) if row else None
        scope = ({k: grant[k] for k in ("grant_id", "environment", "valid_until", "scope")} if grant else None)
        return {"role": role, "control": self.mode(), "tasks": [self.task_packet(t["task_id"]) for t in tasks],
                "declared_grant_scope": scope, "funding_authorization": False,
                "recent_history": [{"task_id": t["task_id"], "mechanism_id": t["request"]["mechanism_id"], "state": t["state"]}
                                   for t in finished[-20:]], "older_history_count": max(0, len(finished) - 20),
                "other_line": [{"task_id": t["task_id"], "request_hash": t["request_hash"], "state": t["state"],
                                **{k: t["request"][k] for k in ("mechanism_id", "information_family", "hypothesis",
                                    "primary_fields", "availability_clock", "risk_exposures", "control", "falsifier")}}
                               for t in all_tasks[-20:] if role in INFORMATION and t["owner"] != role],
                "instruction": "Reply with version, request_hash and reply_to; propose changes as a new task. Do not write SQLite directly."}
