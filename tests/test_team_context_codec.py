from copy import deepcopy
import json
import sys

import pytest

from panda_alpha.team.context_codec import FORMAT, MARKER, decode, encode
from panda_alpha.team.codex_worker import prompt
from panda_alpha.team.contracts import canonical, digest
from panda_alpha.team.runtime import worker_config


def test_duplicate_evidence_and_qualifications_roundtrip_without_changing_work():
    evidence = {"id": "B.PIT.001", "revision": 3, "status": "pending", "text": "原生时点尚未验证" * 500,
                "evidence": [{"path": "original.json", "sha256": "a" * 64}]}
    packet = {"work_id": "immutable-work", "action": "review", "task": {"qualifications": evidence},
              "memory": {"qualifications": evidence}, "role_packet": {"task": {"qualifications": evidence}}}
    before, identity = deepcopy(packet), digest(packet)
    value = {"packet": packet, "context": [evidence]}
    encoded = encode(value)
    assert encoded["format"] == FORMAT
    assert len(canonical(encoded).encode()) < len(canonical(value).encode()) / 2
    assert decode(encoded) == value
    assert packet == before and digest(packet) == identity


def test_escaped_paths_lists_and_forward_references():
    repeated = {"statement": "keep the entire statement" * 500}
    value = {"z": [repeated], "task": {"a/b~c": repeated}, "a": repeated}
    encoded = encode(value)
    assert decode(encoded) == value
    assert "~1" in canonical(encoded) and "~0" in canonical(encoded)


@pytest.mark.parametrize("literal", [{MARKER: "#/literal"}, {MARKER: "text", "other": 1}])
def test_literal_reference_key_falls_back_to_full(literal):
    value = {"source": literal, "duplicates": ["x" * 9000] * 2}
    encoded = encode(value)
    assert encoded["format"] == "full"
    assert decode(encoded) == value


def test_nonbeneficial_and_small_input_stays_full():
    value = {"context": ["small", "small"], "task": {"pending": ["A.1"]}}
    assert encode(value) == {"format": "full", "content": value}


def test_repeated_short_evidence_keeps_distinct_qualification_revisions():
    binding = {"path": "D:/research/originals/financial-report-2025.pdf", "sha256": "a" * 64}
    pending = {"id": "B.PIT.001", "revision": 1, "status": "pending", "text": "Announcement boundary unverified", "evidence": [binding]}
    resolved = dict(pending, revision=2, status="resolved")
    value = {"task": {"qualifications": [pending, resolved]},
             "events": [{"id": i, "artifacts": [binding], "qualifications": [pending, resolved]} for i in range(20)]}
    before = deepcopy(value)
    encoded = encode(value)
    assert decode(encoded) == before and value == before
    assert len(canonical(encoded)) < .8 * len(canonical(value))
    assert decode(encoded)["task"]["qualifications"][0]["status"] == "pending"
    assert decode(encoded)["task"]["qualifications"][1]["revision"] == 2


def test_deep_pointer_does_not_expand_a_short_repeated_value():
    repeated = {"proof": "retain this exact source evidence " * 6}
    nested = repeated
    for i in range(30):
        nested = {"long-qualified-evidence-path-" + str(i): nested}
    value = {"a": nested, "z": [repeated, "large original " * 1000, "large original " * 1000]}
    encoded = encode(value)
    assert decode(encoded) == value
    assert encoded["content"]["z"][0] == repeated
    assert len(canonical(encoded)) < len(canonical(value))


@pytest.mark.parametrize("content", [
    {"a": {MARKER: "#/missing"}},
    {"a": {MARKER: "#/a"}},
    {"a": {MARKER: "#/b"}, "b": {MARKER: "#/a"}},
])
def test_bad_reference_cannot_be_mistaken_for_restored_evidence(content):
    with pytest.raises(ValueError):
        decode({"format": FORMAT, "content": content})


def test_prompt_format_opt_in_and_reply_contract_unchanged():
    packet = {"work_id": "work", "action": "propose", "evidence": "x" * 5000}
    context = [packet["evidence"]]
    old = prompt(packet, context)
    assert prompt(packet, context, "full") == old
    reduced = prompt(packet, context, "lossless_refs_v1")
    restored = decode(json.loads(reduced[reduced.index('{"content"'):]))
    original = json.loads(old[old.index('{"context"'):])
    assert restored == original
    assert restored["packet"]["work_id"] == "work"
    assert "never aliases in your reply" in reduced
    with pytest.raises(ValueError):
        prompt(packet, context, "summarize")


def test_format_is_explicit_frozen_worker_configuration(tmp_path):
    path = tmp_path / "workers.json"
    config = {"codex_command": [sys.executable], "model": "gpt-6.1-sol", "reasoning_effort": "max"}
    path.write_text(json.dumps(config), encoding="utf-8")
    original, _ = worker_config(path)
    assert original.get("prompt_format", "full") == "full"
    config["prompt_format"] = "lossless_refs_v1"
    path.write_text(json.dumps(config), encoding="utf-8")
    frozen, _ = worker_config(path)
    config["prompt_format"] = "unknown"
    path.write_text(json.dumps(config), encoding="utf-8")
    assert frozen["prompt_format"] == "lossless_refs_v1"
    with pytest.raises(ValueError, match="prompt_format"):
        worker_config(path)
