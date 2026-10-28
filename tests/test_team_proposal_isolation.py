from copy import deepcopy

import pytest

from panda_alpha.team.demo import synthetic_request
from panda_alpha.team.host import Host
from test_team_lifecycle import lifecycle, spec


@pytest.mark.parametrize('order', [('market', 'fundamental'), ('fundamental', 'market')])
def test_current_round_peer_is_hidden_until_proposals_commit(lifecycle, order):
    c, registry, proof, _ = lifecycle
    for owner in order:
        past = synthetic_request(owner, proof)
        past['task_id'] = 'past-' + owner
        past['window'] = {'start': '2020-01-01', 'end': '2020-06-30'}
        task = c.board.submit(past)
        c.board.decide(task['task_id'], task['version'], {'decision': 'park', 'reason': 'Synthetic past context'})
    before_memory, before_control = registry.reconciled_memory(), c.board.mode()
    host = Host(c, registry, 'independent-proposals', spec())
    plan = host.next()['packet']
    host.respond(plan['work_id'], {'work_id': plan['work_id'], 'output': {
        'decision': 'research', 'reason': 'Two synthetic independent information lines',
        'assignments': [{'owner': owner, 'brief': 'Independent ' + owner} for owner in order]}})
    committed = []
    for owner in order:
        packet = host.next()['packet']
        assert packet['action'] == 'propose' and packet['role'] == owner
        visible = {p['task_id'] for p in packet['role_packet']['other_line']}
        assert not visible.intersection(committed)
        assert 'past-' + next(x for x in order if x != owner) in visible
        assert packet['memory'] == before_memory
        assert Host(c, registry, host.session_id).next()['packet'] == packet
        request = synthetic_request(owner, proof)
        request['task_id'] = packet['task_id']
        host.respond(packet['work_id'], {'work_id': packet['work_id'], 'output': {'request': request}})
        committed.append(packet['task_id'])
    original_tasks = [deepcopy(c.board.task(tid)) for tid in committed]
    challenge = host.next()['packet']
    assert challenge['action'] == 'challenge' and challenge['role'] == 'skeptic'
    assert set(committed) <= {t['task_id'] for t in challenge['role_packet']['tasks']}
    assert [c.board.task(tid) for tid in committed] == original_tasks
    assert registry.reconciled_memory() == before_memory and c.board.mode() == before_control
    assert c.ledger.jobs() == []
