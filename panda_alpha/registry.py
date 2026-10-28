"""Append-only research facts, unique strategy registration and audited migration.

The denominator is the immutable historical baseline plus unique registered
definitions. A cumulative total is an assertion to reconcile, never an increment.
Source repairs, execution retries and cost scenarios are evidence events, not new
strategies. Import planning is read-only and application verifies its source hashes
and database head again, so a reviewable plan cannot silently import changed data.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def verify_artifact(artifact: Mapping[str, Any]) -> dict[str, str]:
    path = Path(artifact.get("path", "")).resolve()
    expected = str(artifact.get("sha256", "")).lower()
    if not path.is_file() or len(expected) != 64:
        raise ValueError("An import artifact needs an existing path and SHA256")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected:
        raise ValueError(f"Import artifact SHA256 mismatch: {path}")
    return {"path": str(path), "sha256": actual}


def strategy_definition(candidate: Mapping[str, Any], window: Mapping[str, Any],
                        cycle: int, groups: int) -> dict[str, Any]:
    from .diversity import canonical_signature
    evaluation_type = candidate.get("economic_evaluation_type", "factor")
    if evaluation_type not in ("factor", "stateful_account"):
        raise ValueError("Unknown economic evaluation type")
    expression = candidate.get("formula") or candidate.get("code")
    specification = candidate.get("strategy_spec")
    if not expression and not isinstance(specification, Mapping):
        raise ValueError("A strategy registration needs its formula, code or frozen structured strategy_spec")
    signature = canonical_signature(str(expression)) if expression else _digest(specification)
    definition = {"signature": signature, "direction": candidate["direction"],
                  "window": window, "cycle": cycle, "groups": groups}
    if evaluation_type == "stateful_account":
        definition["evaluation_type"] = evaluation_type
    return normalize_definition(definition)


def normalize_definition(definition: Mapping[str, Any]) -> dict[str, Any]:
    evaluation_type = definition.get("evaluation_type", "factor")
    if evaluation_type not in ("factor", "stateful_account"):
        raise ValueError("Unknown economic evaluation type")
    signature = str(definition.get("signature", ""))
    if len(signature) != 64 or any(c not in "0123456789abcdef" for c in signature):
        raise ValueError("Definition needs a canonical SHA256 signature")
    direction = definition.get("direction")
    if isinstance(direction, bool) or direction not in (0, 1):
        raise ValueError("Definition needs direction 0 or 1")
    window = definition.get("window")
    if not isinstance(window, Mapping) or not window.get("start") or not window.get("end"):
        raise ValueError("Definition needs the frozen start and end window")
    cycle, groups = definition.get("cycle"), definition.get("groups")
    if evaluation_type == "stateful_account":
        if type(cycle) is not int or cycle != 1 or type(groups) is not int or groups != 1:
            raise ValueError("Stateful account definitions require cycle 1 and groups 1")
        return {"signature": signature, "direction": direction, "window": dict(window),
                "cycle": cycle, "groups": groups, "evaluation_type": evaluation_type}
    if (isinstance(cycle, bool) or not isinstance(cycle, int) or cycle < 1 or
            isinstance(groups, bool) or not isinstance(groups, int) or groups < 2):
        raise ValueError("Definition needs positive cycle and at least two groups")
    return {"signature": signature, "direction": direction, "window": dict(window),
            "cycle": cycle, "groups": groups}


def trial_key(definition: Mapping[str, Any]) -> str:
    # Preserve the old CLI TrialLedger's exact serialization and identity.
    return hashlib.sha256(json.dumps(normalize_definition(definition), sort_keys=True).encode()).hexdigest()


class ResearchRegistry:
    """SQLite event store; compatible with the former CLI TrialLedger interface."""

    _KINDS = {"baseline", "registration", "identity_alias", "source_pending", "source_repair",
              "execution_repair", "evaluation", "economic_rejected", "evidence_reopened", "human_retired",
              "human_reopened", "reconciliation", "import_receipt", "qualification_reconciliation"}

    def __init__(self, path: str | Path, historical: int = 0, *, read_only: bool = False) -> None:
        path = Path(path)
        if read_only:
            self.db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
            try:
                self.historical = int(next(e["payload"]["count"] for e in self.events() if e["kind"] == "baseline"))
                self.verify_integrity()
            except BaseException:
                self.db.close()
                raise
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.execute("CREATE TABLE IF NOT EXISTS research_events (sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL, kind TEXT NOT NULL, subject TEXT NOT NULL, payload TEXT NOT NULL, observed_at TEXT NOT NULL, previous_hash TEXT NOT NULL, event_hash TEXT NOT NULL)")
        self.db.execute("CREATE TRIGGER IF NOT EXISTS research_events_no_update BEFORE UPDATE ON research_events BEGIN SELECT RAISE(ABORT, 'research events are immutable'); END")
        self.db.execute("CREATE TRIGGER IF NOT EXISTS research_events_no_delete BEFORE DELETE ON research_events BEGIN SELECT RAISE(ABORT, 'research events are immutable'); END")
        self.db.commit()
        baseline = [e for e in self.events() if e["kind"] == "baseline"]
        if not baseline:
            tables = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            old = None
            if "metadata" in tables:
                old = self.db.execute("SELECT value FROM metadata WHERE name='historical'").fetchone()
            value = max(int(historical), int(old[0]) if old else 0)
            if value < 0:
                raise ValueError("Historical baseline must be nonnegative")
            self.append_event("baseline", "historical", {"count": value, "origin": "legacy_metadata" if old else "explicit_baseline"})
            baseline = [e for e in self.events() if e["kind"] == "baseline"]
        self.historical = int(baseline[0]["payload"]["count"])
        if int(historical) > self.historical:
            self.db.close()
            raise ValueError("Historical baseline is immutable; reconcile cumulative totals instead of adding them")
        try:
            self._migrate_legacy_trials()
            self.verify_integrity()
        except Exception:
            self.db.close()
            raise

    @property
    def head(self) -> str:
        row = self.db.execute("SELECT event_hash FROM research_events ORDER BY sequence DESC LIMIT 1").fetchone()
        return row[0] if row else ""

    def events(self) -> list[dict[str, Any]]:
        return [{"sequence": row[0], "event_id": row[1], "kind": row[2], "subject": row[3],
                 "payload": json.loads(row[4]), "observed_at": row[5], "previous_hash": row[6], "event_hash": row[7]}
                for row in self.db.execute("SELECT * FROM research_events ORDER BY sequence")]

    def append_event(self, kind: str, subject: str, payload: Mapping[str, Any], *,
                     event_id: str | None = None, observed_at: str | None = None,
                     commit: bool = True) -> bool:
        owns_transaction = not self.db.in_transaction
        if owns_transaction:
            self.db.execute("BEGIN IMMEDIATE")
        try:
            added = self._append_locked(kind, subject, payload, event_id=event_id,
                                        observed_at=observed_at)
            if commit:
                self.db.commit()
            return added
        except Exception:
            if owns_transaction:
                self.db.rollback()
            raise

    def _append_locked(self, kind: str, subject: str, payload: Mapping[str, Any], *,
                       event_id: str | None = None, observed_at: str | None = None) -> bool:
        if kind not in self._KINDS or not subject:
            raise ValueError("Unknown research event kind or empty subject")
        payload = dict(payload)
        if kind in {"human_retired", "human_reopened"}:
            if payload.get("actor") != "human" or not str(payload.get("instruction", "")).strip():
                raise ValueError("Human retirement/reopening needs explicit human instruction")
        if kind == "economic_rejected" and payload.get("data_complete") is False:
            raise ValueError("Missing source data cannot be registered as an economic rejection")
        if kind == "evidence_reopened":
            self._verify_evidence_reopening(payload)
        if kind == "baseline":
            prior = self.db.execute("SELECT payload FROM research_events WHERE kind='baseline' LIMIT 1").fetchone()
            if prior:
                if json.loads(prior[0])["count"] != payload.get("count"):
                    raise ValueError("Historical baseline conflicts with an immutable fact")
                return False
        if kind in {"source_pending", "source_repair", "execution_repair", "evaluation", "economic_rejected", "evidence_reopened"}:
            names = self.aliases()
            if subject in names:
                subject = names[subject]
            elif kind in {"economic_rejected", "evidence_reopened"}:
                raise ValueError("Economic rejection/reopening needs a registered strategy identity")
        identity = event_id or _digest({"kind": kind, "subject": subject, "payload": payload})
        old = self.db.execute("SELECT kind, subject, payload FROM research_events WHERE event_id=?", (identity,)).fetchone()
        if old:
            if old != (kind, subject, _json(payload)):
                raise ValueError("Event identity conflicts with an immutable fact")
            return False
        if kind == "qualification_reconciliation":
            self._verify_qualification_reconciliation(subject, payload, identity)
        observed = observed_at or datetime.now(timezone.utc).isoformat()
        previous = self.head
        fact = {"event_id": identity, "kind": kind, "subject": subject, "payload": payload,
                "observed_at": observed, "previous_hash": previous}
        self.db.execute("INSERT INTO research_events(event_id,kind,subject,payload,observed_at,previous_hash,event_hash) VALUES (?,?,?,?,?,?,?)",
                        (identity, kind, subject, _json(payload), observed, previous, _digest(fact)))
        return True

    def _verify_qualification_reconciliation(self, subject, payload, identity):
        from .qualifications import legacy_registry_state, replace, validate
        events = [e for e in self.events() if e["subject"] == subject
                  and e["kind"] in {"source_pending", "source_repair", "qualification_reconciliation", "evaluation"}]
        base = events[-1] if events else None
        if not base or payload.get("base_event") != {k: base[k] for k in ("event_id", "event_hash")}:
            raise ValueError("Qualification migration must bind the latest registry fact")
        prior = legacy_registry_state(base)
        before = validate(payload["before"], prior["scope"])
        rows = {r["id"]: r for r in before["items"]}
        for old in prior["items"]:
            row = rows.get(old["id"])
            if not row or row["text"] != old["text"] or row["status"] != old["status"]:
                raise ValueError("Qualification migration omitted or rewrote a prior qualification")
        for proof in payload.get("artifacts", []):
            verify_artifact(proof)
        expected = (replace(before, payload["changes"], event_id=identity, artifacts=payload["artifacts"],
                            preserve_required=True) if payload.get("changes") else before)
        if payload["qualifications"] != expected:
            raise ValueError("Qualification migration projection differs from its bound replacements")
        if payload.get("source_status_changed") is not False or payload.get("pool_admission") is not False:
            raise ValueError("Qualification migration cannot promote source/economic status or admission")

    @staticmethod
    def _verify_evidence_reopening(payload: Mapping[str, Any]) -> None:
        if (payload.get("change_type") not in {"verified_source_repair", "material_information_change"} or
            not str(payload.get("description", "")).strip() or not str(payload.get("falsifier", "")).strip()):
            raise ValueError("Evidence reopening needs a verified source/material information change and falsifier")
        required = "verification_summary" if payload["change_type"] == "verified_source_repair" else "changed_information"
        if not str(payload.get(required, "")).strip() or not payload.get("artifacts"):
            raise ValueError("Evidence reopening needs verifiable artifacts and the change verification")
        for artifact in payload["artifacts"]:
            verify_artifact(artifact)

    def verify_integrity(self) -> None:
        previous = ""
        for event in self.events():
            fact = {k: event[k] for k in ("event_id", "kind", "subject", "payload", "observed_at", "previous_hash")}
            if event["previous_hash"] != previous or event["event_hash"] != _digest(fact):
                raise ValueError("Research event hash chain is corrupt")
            previous = event["event_hash"]

    def _migrate_legacy_trials(self) -> None:
        tables = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "trials" not in tables:
            return
        for key, candidate, serialized, observed in self.db.execute("SELECT trial_key,candidate_id,definition,observed_at FROM trials ORDER BY observed_at,trial_key").fetchall():
            definition = normalize_definition(json.loads(serialized))
            if trial_key(definition) != key:
                raise ValueError("Legacy trial key disagrees with its stored definition")
            self._register(definition, str(candidate), str(key),
                           {"origin": "legacy_trials", "registration_before_labels": "unknown_legacy"}, observed)

    def registrations(self) -> dict[str, dict[str, Any]]:
        return {e["subject"]: e["payload"] for e in self.events() if e["kind"] == "registration"}

    def aliases(self) -> dict[str, str]:
        aliases = {key: key for key in self.registrations()}
        events = self.events()
        by_id = {event["event_id"]: event for event in events}
        corrected = set()
        for event in events:
            if event["kind"] != "reconciliation" or "alias_event_kind_corrections" not in event["payload"]:
                continue
            corrections = event["payload"]["alias_event_kind_corrections"]
            if not isinstance(corrections, list):
                raise ValueError("Alias event corrections must be an explicit list")
            for correction in corrections:
                target = by_id.get(correction.get("event_id")) if isinstance(correction, Mapping) else None
                if (not target or target["kind"] != "identity_alias"
                        or "trial_key" in target["payload"]
                        or target["sequence"] >= event["sequence"]
                        or correction.get("event_hash") != target["event_hash"]
                        or correction.get("replacement_kind") != "execution_repair"
                        or correction.get("reason") != "source_literal_materialization_not_identity_alias"):
                    raise ValueError("Alias event correction does not exactly bind a malformed materialization event")
                corrected.add(target["event_id"])
        for event in events:
            if event["kind"] == "identity_alias":
                if "trial_key" not in event["payload"]:
                    if event["event_id"] in corrected:
                        continue
                    raise ValueError("Identity alias lacks trial_key; exact append-only reconciliation required")
                aliases[event["subject"]] = event["payload"]["trial_key"]
        return aliases

    def _register(self, definition: Mapping[str, Any], candidate_id: str, unique_identity: str,
                  provenance: Mapping[str, Any], observed_at: str | None = None, *, commit: bool = True) -> bool:
        definition = normalize_definition(definition)
        key = trial_key(definition)
        owns_transaction = not self.db.in_transaction
        if owns_transaction:
            self.db.execute("BEGIN IMMEDIATE")
        try:
            aliases = self.aliases()
            if unique_identity in aliases and aliases[unique_identity] != key:
                raise ValueError("Unique identity already names a different strategy")
            current = self.registrations().get(key)
            added = False
            if current is None:
                added = self.append_event("registration", key, {"definition": definition,
                          "candidate_id": candidate_id, "unique_identity": unique_identity,
                          **dict(provenance)}, observed_at=observed_at, commit=False)
            self.append_event("identity_alias", unique_identity, {"trial_key": key}, commit=False)
            if commit:
                self.db.commit()
            return added
        except Exception:
            if owns_transaction:
                self.db.rollback()
            raise

    def record(self, candidate: Mapping[str, Any], window: Mapping[str, Any], cycle: int, groups: int) -> bool:
        definition = strategy_definition(candidate, window, cycle, groups)
        return self._register(definition, str(candidate["candidate_id"]), trial_key(definition),
                              {"origin": "cli", "registration_before_labels": True})

    @property
    def new_tested(self) -> int:
        return len(self.registrations())

    @property
    def total(self) -> int:
        return self.historical + self.new_tested

    def reconcile(self, cumulative_total: int, included_identities: list[str] | None = None,
                  *, registrations: Mapping[str, Mapping[str, Any]] | None = None,
                  aliases: Mapping[str, str] | None = None) -> dict[str, Any]:
        registered = dict(registrations or self.registrations())
        names = dict(aliases or self.aliases())
        unknown = []
        if included_identities is None:
            included = set(registered)
        else:
            unknown = sorted(set(included_identities) - set(names))
            included = {names[name] for name in included_identities if name in names}
        missing = sorted(set(registered) - included)
        expected = self.historical + len(included)
        aligned = not unknown and not missing and int(cumulative_total) == expected
        return {"status": "ALIGNED" if aligned else "UNRESOLVED", "historical_baseline": self.historical,
                "unique_registered": len(registered), "included_unique": len(included),
                "expected_total": expected, "asserted_total": int(cumulative_total),
                "unmatched_identities": unknown, "not_in_checkpoint": missing,
                "increment_inferred": False, "cumulative_total_is_not_an_addition": True}

    def plan_import(self, manifest: str | Path) -> dict[str, Any]:
        path = Path(manifest).resolve()
        raw = path.read_bytes()
        document = json.loads(raw.decode("utf-8-sig"))
        if document.get("schema_version") != 1 or not document.get("manifest_id"):
            raise ValueError("Import manifest needs schema_version 1 and manifest_id")
        if int(document.get("historical_baseline", self.historical)) != self.historical:
            raise ValueError("Manifest cannot replace the historical baseline")
        registered, aliases = dict(self.registrations()), dict(self.aliases())
        rows = []
        artifacts = []
        for item in document.get("registrations", []):
            if not item.get("unique_identity") or not item.get("candidate_id"):
                raise ValueError("Special study registration needs explicit identity and candidate ID")
            if not isinstance(item.get("registered_before_labels"), bool):
                raise ValueError("Declare whether registration preceded labels; never infer preregistration")
            proof = verify_artifact(item["protocol"])
            artifacts.append(proof)
            if item.get("registration_artifact"):
                artifacts.append(verify_artifact(item["registration_artifact"]))
            definition = (normalize_definition(item["definition"]) if "definition" in item else
                          strategy_definition(item["candidate"], item["window"], item["cycle"], item["groups"]))
            key = trial_key(definition)
            identity = item["unique_identity"]
            if identity in aliases and aliases[identity] != key:
                raise ValueError("Manifest identity conflicts with an existing definition")
            added = key not in registered
            row = {"trial_key": key, "definition": definition, "candidate_id": item["candidate_id"],
                   "unique_identity": identity, "protocol": proof,
                   "registration_before_labels": item["registered_before_labels"],
                   "original_registered_at": item.get("registered_at"), "new_unique": added}
            registered[key] = row
            aliases[identity] = aliases[key] = key
            rows.append(row)
        event_rows = []
        for event in document.get("events", []):
            kind, subject = event["kind"], event["subject"]
            if kind in {"baseline", "registration", "identity_alias", "import_receipt", "reconciliation", "qualification_reconciliation"}:
                raise ValueError("Manifest evidence events cannot alter trial accounting")
            if kind not in self._KINDS:
                raise ValueError("Unknown manifest event kind")
            payload = dict(event.get("payload", {}))
            if kind in {"human_retired", "human_reopened"}:
                if payload.get("actor") != "human" or not str(payload.get("instruction", "")).strip():
                    raise ValueError("Human retirement/reopening needs explicit human instruction")
            if kind == "economic_rejected" and payload.get("data_complete") is False:
                raise ValueError("Missing source data cannot be registered as an economic rejection")
            if kind == "evidence_reopened":
                self._verify_evidence_reopening(payload)
            if kind in {"source_pending", "source_repair", "execution_repair", "evaluation", "economic_rejected", "evidence_reopened"}:
                if subject not in aliases:
                    raise ValueError("Evidence event must resolve to a registered strategy identity")
                subject = aliases[subject]
            for artifact in payload.get("artifacts", []):
                artifacts.append(verify_artifact(artifact))
            event_rows.append({"kind": kind, "subject": subject, "payload": payload,
                               "event_id": event.get("event_id"), "observed_at": event.get("observed_at")})
        checkpoint = document.get("checkpoint")
        alignment = (self.reconcile(checkpoint["cumulative_total"], checkpoint.get("included_identities"),
                                    registrations=registered, aliases=aliases) if checkpoint else
                     self.reconcile(self.historical + len(registered), registrations=registered, aliases=aliases))
        return {"schema_version": 1, "manifest_id": document["manifest_id"],
                "manifest": {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()},
                "registry_head": self.head, "registrations": rows, "events": event_rows,
                "artifacts": artifacts, "new_unique": sum(row["new_unique"] for row in rows),
                "projected_total": self.historical + len(registered), "reconciliation": alignment,
                "status": "READY" if alignment["status"] == "ALIGNED" else "UNRESOLVED"}

    def import_manifest(self, plan: Mapping[str, Any]) -> dict[str, Any]:
        verify_artifact(plan["manifest"])
        self.db.execute("BEGIN IMMEDIATE")
        try:
            current = self.plan_import(plan["manifest"]["path"])
            if _digest(current) != _digest(dict(plan)):
                raise ValueError("Import plan is stale or modified; produce a new reviewable plan")
            if current["status"] != "READY":
                raise ValueError("Unresolved cumulative totals cannot be imported")
            for row in current["registrations"]:
                self._register(row["definition"], row["candidate_id"], row["unique_identity"],
                               {"origin": "special_study_manifest", "protocol": row["protocol"],
                                "registration_before_labels": row["registration_before_labels"],
                                "manifest": current["manifest"], "original_registered_at": row["original_registered_at"]},
                               commit=False)
            for event in current["events"]:
                self.append_event(event["kind"], event["subject"], event["payload"],
                                  event_id=event["event_id"], observed_at=event["observed_at"], commit=False)
            self.append_event("reconciliation", current["manifest_id"], current["reconciliation"], commit=False)
            self.append_event("import_receipt", current["manifest_id"],
                              {"manifest": current["manifest"], "new_unique": current["new_unique"],
                               "total": current["projected_total"]}, commit=False)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return {"status": "IMPORTED", "manifest_id": current["manifest_id"],
                "new_unique": current["new_unique"], "total": self.total, "registry_head": self.head}

    def export_hot_memory(self, base: Mapping[str, Any] | None = None) -> dict[str, Any]:
        self.verify_integrity()
        memory = dict(base or {})
        claims = [memory.get("multiple_testing_total")]
        for batch in memory.get("recent_research", []):
            if isinstance(batch, Mapping):
                claims.extend(batch.get(name) for name in ("trial_total", "trial_total_at_close", "trial_total_after"))
        latest_claim = max((int(value) for value in claims if isinstance(value, int) and not isinstance(value, bool)), default=self.historical)
        retired: dict[str, dict[str, Any]] = {str(row["direction"]): dict(row)
                    for row in memory.get("human_retired_directions", [])
                    if isinstance(row, Mapping) and row.get("direction")}
        rejected: dict[str, dict[str, Any]] = {}
        reopened: dict[str, dict[str, Any]] = {}
        pending: dict[str, dict[str, Any]] = {}
        evaluations: dict[str, dict[str, Any]] = {}
        registered = self.registrations()
        for event in self.events():
            kind, key, payload = event["kind"], event["subject"], event["payload"]
            if kind == "human_retired":
                retired[key] = {**payload, "direction": key, "event_id": event["event_id"]}
            elif kind == "human_reopened":
                retired.pop(key, None)
            elif kind == "economic_rejected":
                rejected[key] = {**payload, **registered[key], "trial_key": key, "event_id": event["event_id"]}
                reopened.pop(key, None)
            elif kind == "evidence_reopened":
                rejected.pop(key, None)
                reopened[key] = {**payload, **registered[key], "trial_key": key, "event_id": event["event_id"]}
            elif kind == "source_pending":
                from .qualifications import legacy_registry_state
                pending[key] = {**payload, **registered.get(key, {}), "trial_key": key,
                                "qualifications": legacy_registry_state(event)}
            elif kind == "source_repair":
                pending.pop(key, None)
            if kind == "evaluation":
                evaluations.pop(key, None)
                evaluations[key] = {"trial_key": key, "event_id": event["event_id"], **{
                    name: payload[name] for name in ("task_id", "fingerprint", "run_id", "assessment", "decision",
                        "pending", "resolved_qualifications", "artifacts", "evidence_contract", "pool_admission", "qualifications") if name in payload}}
            if kind == "qualification_reconciliation":
                from .qualifications import texts, validate
                qualified = validate(payload["qualifications"])
                for target in (pending.get(key), evaluations.get(key)):
                    if target is not None:
                        target.update(qualifications=qualified, pending=texts(qualified),
                                      resolved_qualifications=texts(qualified, "resolved"),
                                      qualification_reconciliation_event=event["event_id"])
        memory["registry_constraints"] = {"human_retired": list(retired.values()),
                    "fixed_rejected": list(rejected.values()), "source_pending": list(pending.values()),
                    "evidence_reopened": list(reopened.values()),
                    "derived_from_registry_head": self.head}
        memory["human_retired_directions"] = list(retired.values())
        # A qualified positive evaluation must also reach the next research
        # packet. This bounded view is evidence, never automatic pool admission.
        memory["recent_evaluations"] = list(evaluations.values())[-8:]
        memory["multiple_testing_total"] = self.total
        memory["research_registry"] = {"schema_version": 1, "historical_baseline": self.historical,
                                       "unique_registered": self.new_tested, "total": self.total,
                                       "head": self.head, "accounting_scope": "unique_registered_definitions",
                                       "memory_reconciliation": {"status": "UNRESOLVED" if latest_claim > self.total else "ALIGNED",
                                            "latest_asserted_total": latest_claim, "registered_total": self.total,
                                            "increment_inferred": False}}
        memory["multiple_testing_denominator"] = self.historical
        return memory

    def reconciled_memory(self, base: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Shared gate for both research entry points before new experiments."""
        memory = self.export_hot_memory(base)
        if memory["research_registry"]["memory_reconciliation"].get("status") == "UNRESOLVED":
            raise ValueError("Research memory is ahead of registered facts; reconcile the registry before new research")
        return memory

    def close(self) -> None:
        self.db.close()
