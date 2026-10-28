"""Publication checks fail closed on tracked files, approved bytes and history."""
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

import pytest
from scripts import check_publication as guard


def snapshot(name='README.md',data=b'Generic framework documentation\n'):
    policy={'schema':'quantfactra-public-allowlist-v1','files':{name:{'classification':'documentation','sha256':hashlib.sha256(data).hexdigest()}}}
    return {name:data,guard.MANIFEST:json.dumps(policy).encode()}


def test_known_public_files_are_accepted_and_changed_bytes_are_rejected():
    files=snapshot();assert guard.validate_snapshot(files)==2
    files['README.md']+=b'unreviewed change'
    with pytest.raises(ValueError,match='changed since public approval'):guard.validate_snapshot(files)


def test_new_file_is_not_automatically_public():
    files=snapshot();files['my_strategy.py']=b'# application code'
    with pytest.raises(ValueError,match='Unapproved'):guard.validate_snapshot(files)


@pytest.mark.parametrize('name',['research_runs/session/report.json','research_bootstrap/memory.json','config/model.local.json','account.sqlite3','private/file.py','../escape.py'])
def test_private_paths_are_rejected_even_if_added_to_the_manifest(name):
    with pytest.raises(ValueError,match='Private or unsafe'):guard.validate_snapshot(snapshot(name))


@pytest.mark.parametrize('data',[('C:'+chr(47)+'Users'+chr(47)+'private-owner'+chr(47)+'file').encode(),('ghp_'+'a'*35).encode(),('6a'+'1'*22).encode()])
def test_sensitive_content_is_rejected_even_in_an_approved_path(data):
    with pytest.raises(ValueError,match='detected'):guard.validate_snapshot(snapshot(data=data))


def test_research_history_cannot_be_hidden_by_a_clean_latest_commit(tmp_path,monkeypatch):
    def run(*args):subprocess.run(['git',*args],cwd=tmp_path,check=True,capture_output=True)
    run('init','-b','master');run('config','user.name','Synthetic Test');run('config','user.email','fixture@example.invalid')
    (tmp_path/'old-application.txt').write_text('Synthetic private-history fixture')
    run('add','.');run('commit','-m','synthetic old application')
    (tmp_path/'old-application.txt').unlink()
    for name,data in snapshot().items():
        path=tmp_path/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
    run('add','-A');run('commit','-m','synthetic clean snapshot')
    monkeypatch.setattr(guard,'ROOT',tmp_path)
    with pytest.raises(ValueError,match='Unapproved Git history'):guard.validate_history()


def test_history_inspection_failure_does_not_become_a_pass(monkeypatch):
    def broken(*args):
        if args[0]=='rev-parse':return b'.git\n'
        raise subprocess.CalledProcessError(1,args)
    monkeypatch.setattr(guard,'git',broken)
    with pytest.raises(ValueError,match='could not be completely inspected'):guard.validate_history()


def test_archive_cannot_reintroduce_private_files(tmp_path):
    path=tmp_path/'package.whl'
    with zipfile.ZipFile(path,'w') as archive:archive.writestr('research_bootstrap/memory.json','{}')
    with pytest.raises(ValueError,match='Private or unsafe'):guard.validate_artifact(path,{})


def test_public_planner_starts_without_a_personal_factor_bank():
    from panda_alpha.evolution import deterministic_candidates,ResearchEngine
    assert deterministic_candidates()==[]
    assert ResearchEngine(memory={}).propose(2)==[]


def test_public_framework_has_no_application_collectors():
    from panda_alpha.team.data_sources import validate
    validate({})
    with pytest.raises(ValueError,match='own worker'):validate({'data_requests':[{'id':'synthetic'}]})


def test_default_role_instructions_are_packaged_and_contain_no_model_or_application_config():
    from importlib.resources import files
    roles=json.loads(files('panda_alpha.team').joinpath('roles.json').read_text(encoding='utf-8'))
    assert set(roles)=={'lead','market','fundamental','skeptic'}
    assert all(isinstance(value,str) and value for value in roles.values())
