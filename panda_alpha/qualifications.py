"""Stable, scoped qualification identities and explicit evidence-bound replacements.

Legacy text is assigned an exact-content ID, never a semantic/substring alias.
Snapshots are projections; immutable board/registry events remain the history.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def fixed_id(scope, text):
    return "Q-" + digest([scope, text])[:24]


def snapshot(scope, items=()):
    value = {"schema": "qualifications-v1", "scope": scope,
             "items": sorted(deepcopy(list(items)), key=lambda x: x["id"])}
    return dict(value, head=digest(value))


def validate(state, scope=None):
    if (not isinstance(state, dict) or state.get("schema") != "qualifications-v1"
            or (scope is not None and state.get("scope") != scope)
            or state != snapshot(state["scope"], state["items"])):
        raise ValueError("Qualification snapshot identity/hash differs")
    seen = set()
    for row in state["items"]:
        if (row["id"] in seen or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", row["id"])
                or not isinstance(row["text"], str) or not row["text"].strip()
                or row["status"] not in {"pending", "resolved", "superseded"}
                or type(row["required"]) is not bool
                or row["revision"] != digest({k: v for k, v in row.items() if k != "revision"})):
            raise ValueError("Invalid qualification record")
        seen.add(row["id"])
    return state


def _row(identity, text, status, event_id, required=False, **extra):
    row = dict(id=identity, text=text, status=status, event_id=event_id, required=required, **extra)
    return dict(row, revision=digest(row))


def observe(state, pending=(), resolved=(), *, event_id, required=()):
    """Exact legacy replay/initialization. Not used to close items in new reviews."""
    rows = {r["id"]: r for r in validate(state)["items"]}
    for status, texts in (("resolved", resolved), ("pending", pending)):
        for text in sorted(set(texts)):
            identity = fixed_id(state["scope"], text)
            old = rows.get(identity)
            if old and old["status"] == "superseded":
                continue  # An old wording cannot silently revive a replaced assertion.
            gate = text in required or bool(old and old["required"])
            if not old or old["status"] != status or gate != old["required"]:
                rows[identity] = _row(identity, text, status, event_id, gate)
    return snapshot(state["scope"], rows.values())


def texts(state, status="pending"):
    return sorted({r["text"] for r in state["items"] if r["status"] == status})


def replace(state, changes, *, event_id, artifacts, verify=True, preserve_required=False):
    """Replace exact active revisions; permit splits and merges, never implicit matches."""
    validate(state)
    if not isinstance(changes, dict) or changes.get("base_head") != state["head"]:
        raise ValueError("Stale qualification base; read the current IDs and revisions")
    groups, additions = changes.get("replacements", []), changes.get("additions", [])
    if (set(changes) - {"base_head", "replacements", "additions"}
            or not isinstance(groups, list) or not isinstance(additions, list)):
        raise ValueError("Qualification replacements and additions must be lists")
    def evidence_for(item):
        proof = item.get("evidence")
        if (not isinstance(proof, list) or not proof
                or not isinstance(item.get("reason"), str) or not item["reason"].strip()):
            raise ValueError("Qualification change needs a reason and bound evidence")
        if any(a not in artifacts for a in proof):
            raise ValueError("Replacement evidence is not bound to the review")
        if verify:
            from .registry import verify_artifact
            for a in proof:
                verify_artifact(a)
        return proof
    rows = {r["id"]: deepcopy(r) for r in state["items"]}
    consumed, produced = set(), set()
    for group in groups:
        targets, outputs = group.get("supersedes"), group.get("items")
        if not isinstance(targets, list) or not targets or not isinstance(outputs, list) or not outputs:
            raise ValueError("Replacement needs exact supersedes, items, reason and bound evidence")
        proof = evidence_for(group)
        bound = {}
        for ref in targets:
            old = rows.get(ref.get("id"))
            if (not old or old["status"] == "superseded" or old["revision"] != ref.get("revision")
                    or old["id"] in consumed or old["id"] in produced):
                raise ValueError("Supersession must bind a unique current qualification revision")
            consumed.add(old["id"])
            bound[old["id"]] = old
        fresh = []
        for item in outputs:
            identity = item.get("id", "")
            if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", identity)
                    or identity in produced or (identity in rows and identity not in bound)
                    or item.get("status") not in {"pending", "resolved"}
                    or not isinstance(item.get("text"), str) or not item["text"].strip()):
                raise ValueError("Replacement IDs must be unique; existing IDs must be superseded explicitly")
            produced.add(identity)
            fresh.append(_row(identity, item["text"], item["status"], event_id,
                              bound.get(identity, {}).get("required", False),
                              supersedes=deepcopy(targets), evidence=deepcopy(proof), reason=group["reason"]))
        for old in bound.values():
            same = next((r for r in fresh if r["id"] == old["id"]), None)
            if old["required"] and (same is None or (preserve_required and same["status"] != old["status"])):
                raise ValueError("Frozen/observed gate must retain its ID and migration status")
            if same is None:
                rows[old["id"]] = _row(old["id"], old["text"], "superseded", event_id,
                    old["required"], superseded_by=[{"id": r["id"], "revision": r["revision"]} for r in fresh])
        rows.update({r["id"]: r for r in fresh})
    for item in additions:
        identity = item.get("id", "")
        if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", identity) or identity in rows
                or item.get("status") not in {"pending", "resolved"}
                or not isinstance(item.get("text"), str) or not item["text"].strip()):
            raise ValueError("New qualification observations need a unique ID, text and status")
        proof = evidence_for(item)
        rows[identity] = _row(identity, item["text"], item["status"], event_id,
                              evidence=deepcopy(proof), reason=item["reason"])
    return validate(snapshot(state["scope"], rows.values()))


def legacy_registry_state(event):
    payload = event["payload"]
    if payload.get("qualifications"):
        return validate(payload["qualifications"])
    scope = payload.get("task_id") or event["subject"]
    pending = payload.get("pending") or ["Legacy source qualification: " + event["event_id"]]
    return observe(snapshot(scope), pending, payload.get("resolved_qualifications", []), event_id=event["event_id"])


def board_state(task, events):
    required = set(task["request"].get("source_requirements", [])) | {
        x for x in task["request"]["candidate"].get("data_requirements", []) if x.endswith("_pending")}
    state = observe(snapshot(task["task_id"]), required, event_id="request:" + task["request_hash"], required=required)
    # A checkpoint makes archived evidence files unnecessary for reading history.
    checkpoint = next((i for i in range(len(events) - 1, -1, -1)
                       if events[i]["payload"].get("qualifications")), -1)
    if checkpoint >= 0:
        state = validate(events[checkpoint]["payload"]["qualifications"], task["task_id"])
    for event in events[checkpoint + 1:]:
        payload = event["payload"]
        if event["kind"] in {"result", "result_review"} and payload.get("evidence_contract"):
            from .team.evidence import read_artifact
            required = set(read_artifact(payload["evidence_contract"])["unknown"])
            # Mark observed gates without re-opening any later explicit resolution.
            rows = {r["id"]: r for r in state["items"]}
            for key in required:
                identity = fixed_id(task["task_id"], key)
                old = rows.get(identity)
                if old and not old["required"]:
                    marked = dict(old, required=True)
                    marked["revision"] = digest({k: v for k, v in marked.items() if k != "revision"})
                    rows[identity] = marked
            state = snapshot(task["task_id"], rows.values())
            state = observe(state, [key for key in required if fixed_id(task["task_id"], key) not in rows],
                            event_id=event["event_id"], required=required)
        if event["kind"] == "result_review":
            a = payload.get("assessment", {})
            state = observe(state, a.get("pending", []), a.get("resolved", []), event_id=event["event_id"])
    return state


def review_state(task, events, message, contract):
    from .platform import SOFT_RESEARCH_PENDING
    state = board_state(task, events)
    required = set(contract["unknown"]) | set(task["request"].get("source_requirements", [])) | {
        x for x in task["request"]["candidate"].get("data_requirements", []) if x.endswith("_pending")}
    missing = [x for x in required if fixed_id(task["task_id"], x) not in {r["id"] for r in state["items"]}]
    state = observe(state, missing, event_id="requirements:" + message["event_id"], required=required)
    explicit = "qualification_changes" in message
    structured = explicit or any(e["payload"].get("qualifications") for e in events)
    if explicit:
        state = replace(state, message["qualification_changes"], event_id=message["event_id"], artifacts=message["artifacts"])
    assessment = message["assessment"]
    # Free text remains explanatory. It cannot close an existing pending ID.
    active = {r["text"]: r for r in state["items"] if r["status"] != "superseded"}
    by_id = {r["id"]: r for r in state["items"]}
    def existing(text):
        return active.get(text) or by_id.get(fixed_id(task["task_id"], text))
    if any(existing(x) and existing(x)["status"] == "pending" for x in assessment["resolved"]):
        raise ValueError("Closing a pending qualification requires fixed IDs and evidence-bound supersession")
    if any(existing(x) and existing(x)["status"] == "resolved" for x in assessment["pending"]):
        raise ValueError("Reopening a qualification requires explicit supersession")
    unknown = {key: [x for x in assessment[key] if not existing(x)] for key in ("pending", "resolved")}
    if structured:
        # In numbered reviews prose explains decisions; only explicit additions
        # create new items. An ID plus a paraphrase is not a new qualification.
        if not explicit and any(unknown.values()):
            raise ValueError("Numbered reviews require qualification_changes: use evidence-bound additions for new "
                             "items, or current base_head and empty replacements for explanation only")
    else:
        # Bootstrap old, unnumbered first reviews without changing their history.
        state = observe(state, unknown["pending"], unknown["resolved"], event_id=message["event_id"])
    if assessment["source_status"] == "verified" and set(texts(state)) - SOFT_RESEARCH_PENDING:
        raise ValueError("Unresolved historical source qualifications cannot be silently cleared")
    return state
