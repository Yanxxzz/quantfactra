"""Lossless prompt-only aliases; canonical work packets stay unchanged."""
from __future__ import annotations

from copy import deepcopy

from .contracts import canonical

MARKER = "$panda_same_as"
FORMAT = "panda-context-refs-v1"
PRIORITY = ("packet", "source_material", "independent_source_analyses", "task", "qualifications", "review_targets")


def focus_packet(packet):
    """Prompt-only projection; preserve immutable packets and actual task evidence.

    Global retirement/economic constraints stay complete. Unrelated source-only
    history keeps every qualification ID/text/status, without repeating its old
    definitions, evidence and supersession history in each new model turn.
    """
    if packet.get("context_policy") != "task_scoped_v1":
        return packet
    value = deepcopy(packet)
    task = value.get("task") or {}
    task_id, fingerprint = task.get("task_id"), task.get("fingerprint")
    roles = value.get("role_packet", {})
    # The exact current task is already present at packet.task.
    roles["tasks"] = []
    roles["other_line"] = [r for r in roles.get("other_line", []) if r["task_id"] == value.get("peer_task_id")]
    memory = value.get("memory", {})
    constraints = memory.get("registry_constraints", {})
    pending = []
    for row in constraints.get("source_pending", []):
        if (task_id and row.get("task_id") == task_id) or (fingerprint and row.get("fingerprint") == fingerprint):
            pending.append(row)
            continue
        summary = {k: row[k] for k in ("trial_key", "event_id", "task_id", "fingerprint", "pending", "resolved_qualifications") if k in row}
        qualified = row.get("qualifications")
        if qualified:
            summary["qualifications"] = {"head": qualified.get("head"), "items": [
                {k: item[k] for k in ("id", "revision", "text", "status") if k in item}
                for item in qualified.get("items", [])]}
        summary["historical_evidence_omitted"] = True
        pending.append(summary)
    if "source_pending" in constraints:
        constraints["source_pending"] = pending
    # Historical evaluations carry the same long qualification revision graphs.
    # Keep their exact claims, verdicts, evidence bindings and every current item;
    # the current task's full graph is already supplied for an actual re-review.
    evaluations = []
    for row in memory.get("recent_evaluations", []):
        item = dict(row)
        qualified = item.get("qualifications")
        if qualified:
            item["qualifications"] = {"head": qualified.get("head"), "items": [
                {k: entry[k] for k in ("id", "revision", "text", "status") if k in entry}
                for entry in qualified.get("items", [])]}
        evaluations.append(item)
    if "recent_evaluations" in memory:
        memory["recent_evaluations"] = evaluations
    value["context_scope"] = "Current task evidence is complete; unrelated source history is an obligation index, not proof read by this model."
    return value


def encode(value, *, minimum_bytes=128):
    """Alias exact repeated values, never summarize or drop a qualification.

    Literal marker keys cause a full-format fallback, so source text cannot be
    confused with a generated reference. Small/non-beneficial inputs stay full.
    """
    def collision(node):
        if isinstance(node, dict):
            return MARKER in node or any(collision(v) for v in node.values())
        return isinstance(node, list) and any(collision(v) for v in node)

    full = {"format": "full", "content": value}
    if collision(value):
        return full
    seen = {}

    def visit(node, pointer):
        serialized = canonical(node)
        if len(serialized.encode("utf-8")) >= minimum_bytes:
            if serialized in seen:
                alias = {MARKER: seen[serialized]}
                # Small evidence bindings repeat often, but a deep pointer can
                # cost more than the original. Keep those occurrences inline.
                if len(canonical(alias).encode("utf-8")) < len(serialized.encode("utf-8")):
                    return alias
            else:
                seen[serialized] = pointer
        if isinstance(node, dict):
            keys = sorted(node, key=lambda k: (PRIORITY.index(k) if k in PRIORITY else len(PRIORITY), k))
            return {k: visit(node[k], pointer + "/" + k.replace("~", "~0").replace("/", "~1")) for k in keys}
        if isinstance(node, list):
            return [visit(v, pointer + "/" + str(i)) for i, v in enumerate(node)]
        return node

    reduced = {"format": FORMAT, "content": visit(value, "#")}
    return reduced if len(canonical(reduced).encode("utf-8")) < len(canonical(full).encode("utf-8")) else full


def decode(encoded):
    """Reconstruct a recorded prompt payload for evidence/recovery audits."""
    root = encoded["content"]
    if encoded["format"] == "full":
        return root
    if encoded["format"] != FORMAT:
        raise ValueError("Unknown context encoding")

    def expand(node, trail):
        if isinstance(node, dict) and set(node) == {MARKER}:
            pointer = node[MARKER]
            if not isinstance(pointer, str) or not pointer.startswith("#/") or pointer in trail:
                raise ValueError("Invalid or cyclic context reference")
            target = root
            try:
                for key in pointer[2:].split("/"):
                    key = key.replace("~1", "/").replace("~0", "~")
                    target = target[int(key)] if isinstance(target, list) else target[key]
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise ValueError("Missing context reference") from exc
            return expand(target, trail | {pointer})
        if isinstance(node, dict):
            return {k: expand(v, trail) for k, v in node.items()}
        if isinstance(node, list):
            return [expand(v, trail) for v in node]
        return node

    return expand(root, set())
