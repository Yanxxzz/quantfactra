import json

import pytest

from panda_alpha.team.codex_worker import answer
from panda_alpha.team.contracts import digest
from panda_alpha.team.runtime_io import read, write


def test_completed_reply_repairs_only_literal_string_controls_and_preserves_raw(tmp_path):
    raw = '{"claim":"第一行\n第二行\r\n第三行\t尾部", "decision":"park", "test":"escaped \\n and \\\"quote\\\""}'
    write(tmp_path / 'answer.json', {'work_id': 'original-work', 'output_json': raw})
    before = (tmp_path / 'answer.json').read_bytes()
    result = answer(tmp_path, {'work_id': 'original-work'})
    assert result['output'] == json.loads(raw, strict=False)
    assert result['output']['claim'] == '第一行\n第二行\r\n第三行\t尾部'
    assert result['output']['decision'] == 'park'
    assert (tmp_path / 'answer.json').read_bytes() == before
    receipt = read(tmp_path / 'output-json-repair.json')
    assert receipt['work_id'] == 'original-work' and receipt['changed_control_characters'] == 4
    assert receipt['output_digest'] == digest(result['output'])
    assert answer(tmp_path, {'work_id': 'original-work'}) == result


@pytest.mark.parametrize('raw', [
    '{"claim":"line\nnext", "decision":}',
    '{"claim":"line\nnext\x00", "decision":"park"}',
    '{"claim":"line\nnext", "decision":"park",}',
])
def test_escaping_never_repairs_structure_or_other_control_characters(tmp_path, raw):
    write(tmp_path / 'answer.json', {'work_id': 'original-work', 'output_json': raw})
    with pytest.raises(json.JSONDecodeError):
        answer(tmp_path, {'work_id': 'original-work'})
    assert not (tmp_path / 'output-json-repair.json').exists()


def test_valid_reply_unchanged_and_work_identity_remains_strict(tmp_path):
    raw = {'claim': 'literal \\n and real\nnewline', 'decision': 'park'}
    write(tmp_path / 'answer.json', {'work_id': 'original-work', 'output_json': json.dumps(raw)})
    assert answer(tmp_path, {'work_id': 'original-work'})['output'] == raw
    assert not (tmp_path / 'output-json-repair.json').exists()
    with pytest.raises(ValueError, match='Unbound'):
        answer(tmp_path, {'work_id': 'different-work'})
