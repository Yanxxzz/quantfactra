"""Offline smoke of the public framework, approved files and synthetic agents."""
from pathlib import Path
import json
import socket
import sys
import tempfile
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))

def blocked(*args,**kwargs):raise AssertionError('Public smoke must remain offline')

def main():
    from scripts.check_publication import filesystem_snapshot,validate_snapshot
    from quantfactra.demo import run_demo
    from panda_alpha.team.rehearsal import run_rehearsal
    from panda_alpha.evolution import deterministic_candidates
    count=validate_snapshot(filesystem_snapshot())
    if deterministic_candidates():raise AssertionError('Public distribution must not contain a personal factor bank')
    with tempfile.TemporaryDirectory(prefix='quantfactra-public-smoke-') as temp,patch.object(socket.socket,'connect',blocked),patch.object(socket.socket,'connect_ex',blocked):
        root=Path(temp)
        demo=run_demo(root/'adapter')
        team=run_rehearsal(root/'team')
        if demo['state']!='SETTLED' or team['official_runs']!=0:raise AssertionError('Unexpected public smoke result')
    print(json.dumps({'passed':True,'approved_files':count,'multi_agent_rounds':team['rounds'],'official_runs':0,'real_model_calls':0}))

if __name__=='__main__':main()
