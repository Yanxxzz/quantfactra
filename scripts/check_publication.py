"""Default-deny checks for approved source, every Git commit and build artifacts."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile
import tempfile
import zipfile

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
MANIFEST='publication/allowlist.json'
CLASSES={'framework','synthetic_test','documentation','public_example','site_asset','configuration','publication_control'}
FORBIDDEN_PARTS={'.git','.runtime','.venv','research_runs','research_bootstrap','private','data','_data_','work','__pycache__'}
FORBIDDEN_SUFFIXES=('.sqlite3','.db','.pyd','.bundle','.pem','.key','.log','.pyc','.env')
PATTERNS={
    'machine-specific path':re.compile(r'(?i)\b[A-Z]:[\\/](?:Users|PandaAI[-_]Alpha)[\\/]|/Users/\d{4,}/'),
    'access token':re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{25,}|github_pat_[A-Za-z0-9_]{40,}|sk-proj-[A-Za-z0-9_-]{35,})\b'),
    'private key':re.compile(r'-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----'),
    'authenticated database URL':re.compile(r'(?i)mongodb(?:\+srv)?://[^\s/]+:[^\s@]+@'),
    'personal platform run ID':re.compile(r'(?<![0-9a-fA-F])6a[0-9a-fA-F]{22}(?![0-9a-fA-F])'),
    'personal platform factor ID':re.compile(r'\bPA-[0-9a-f]{16}\b'),
}

def sha(data):return hashlib.sha256(data).hexdigest()
def git(*args):return subprocess.check_output(['git',*args],cwd=ROOT,stderr=subprocess.DEVNULL)

def check_path(name):
    path=PurePosixPath(name)
    if (path.is_absolute() or '..' in path.parts or '\\' in name or FORBIDDEN_PARTS.intersection(path.parts)
        or name.endswith(FORBIDDEN_SUFFIXES) or '.local.' in name or path.name.startswith('.env')):
        raise ValueError('Private or unsafe publication path: '+name)

def check_content(name,data):
    if name.endswith(('.png',)):
        return
    try:text=data.decode('utf-8-sig')
    except UnicodeDecodeError as error:raise ValueError('Unreviewed binary content: '+name) from error
    for label,pattern in PATTERNS.items():
        if pattern.search(text):
            raise ValueError(label+' detected in '+name+'; value suppressed')

def validate_snapshot(contents):
    if MANIFEST not in contents:raise ValueError('Missing public approval manifest')
    policy=json.loads(contents[MANIFEST])
    if policy.get('schema')!='quantfactra-public-allowlist-v1':raise ValueError('Unknown public approval schema')
    approved=policy.get('files',{})
    if set(contents)!={*approved,MANIFEST}:
        extra=set(contents)-set(approved)-{MANIFEST}
        missing=set(approved)-set(contents)
        raise ValueError('Unapproved or missing paths: '+', '.join(sorted(extra|missing)))
    for name,data in contents.items():
        check_path(name);check_content(name,data)
        if name==MANIFEST:continue
        row=approved[name]
        if row.get('classification') not in CLASSES:raise ValueError('Missing reviewed classification: '+name)
        if row.get('sha256')!=sha(data):raise ValueError('File changed since public approval: '+name)
    return len(contents)

def filesystem_snapshot():
    try:names=git('ls-files','-z').decode().split('\0')
    except (subprocess.CalledProcessError,FileNotFoundError):
        policy=json.loads((ROOT/MANIFEST).read_text(encoding='utf-8'))
        names=[*policy['files'],MANIFEST]
    result={}
    for name in filter(None,names):
        p=ROOT/name
        if p.is_symlink() or not p.resolve().is_relative_to(ROOT):raise ValueError('Symlink/escaped publication path: '+name)
        result[name]=p.read_bytes()
    return result

def revision_snapshot(revision):
    result={}
    entries=git('ls-tree','-rz',revision).split(b'\0')
    for entry in filter(None,entries):
        meta,path=entry.split(b'\t',1)
        mode,kind,oid=meta.decode().split()
        name=path.decode()
        if mode not in {'100644','100755'} or kind!='blob':raise ValueError('Only regular approved files may be published: '+name)
        result[name]=git('cat-file','blob',oid)
    return result

def validate_history(extra_roots=()):
    try:git('rev-parse','--git-dir')
    except subprocess.CalledProcessError:
        if extra_roots:raise ValueError('Push validation requires a Git repository')
        return 0
    try:commits=set(git('rev-list','--all',*extra_roots).decode().splitlines())
    except subprocess.CalledProcessError as error:
        raise ValueError('Git history could not be completely inspected') from error
    for revision in commits:
        try:validate_snapshot(revision_snapshot(revision))
        except ValueError as error:raise ValueError('Unapproved Git history at '+revision[:12]+': '+str(error)) from error
    return len(commits)

def _archive_content(path):
    if path.suffix in {'.whl','.zip'}:
        with zipfile.ZipFile(path) as archive:
            if archive.testzip() is not None:raise ValueError('Broken archive: '+path.name)
            return {n:archive.read(n) for n in archive.namelist() if not n.endswith('/')}
    with tarfile.open(path) as archive:
        files=[m for m in archive.getmembers() if m.isfile()]
        if any(m.issym() or m.islnk() for m in archive.getmembers()):raise ValueError('Archive links are not publishable')
        return {m.name:archive.extractfile(m).read() for m in files}

def validate_artifact(path,approved):
    contents=_archive_content(path)
    if path.name.endswith('.zip') and 'site-manifest.json' in contents:
        from scripts.build_site import build
        manifest=json.loads(contents['site-manifest.json'])
        with tempfile.TemporaryDirectory(prefix='quantfactra-public-demo-') as temp:
            destination=Path(temp)/'site';build(destination,revision=manifest['revision'])
            expected={p.relative_to(destination).as_posix():p.read_bytes() for p in destination.rglob('*') if p.is_file()}
        if contents!=expected:raise ValueError('Demo archive differs from approved public rendering')
        return len(contents)
    sdist=path.name.endswith(('.tar.gz','.tgz'))
    for member,data in contents.items():
        check_path(member)
        name=member.split('/',1)[1] if sdist and '/' in member else member
        check_path(name);check_content(name,data)
        if name==MANIFEST:
            if data!=(ROOT/MANIFEST).read_bytes():raise ValueError('Artifact approval manifest differs from reviewed source')
        elif name in approved:
            if sha(data)!=approved[name]['sha256']:raise ValueError('Unapproved artifact bytes: '+name)
        elif not (name=='PKG-INFO' or re.fullmatch(r'quantfactra[^/]*\.(?:egg-info|dist-info)/(?:METADATA|WHEEL|RECORD|top_level\.txt|entry_points\.txt|dependency_links\.txt|requires\.txt|SOURCES\.txt|PKG-INFO|licenses/LICENSE)',name)
                  or name=='setup.cfg'):
            raise ValueError('Unapproved artifact member: '+name)
    return len(contents)

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--all-refs',action='store_true')
    parser.add_argument('--pre-push',action='store_true')
    parser.add_argument('--staged',action='store_true')
    parser.add_argument('--artifacts',type=Path)
    args=parser.parse_args(argv)
    try:
        if args.staged:
            names=git('diff','--cached','--name-only','--diff-filter=ACMRTUXB','-z').decode().split('\0')
            for name in filter(None,names):check_path(name)
            staged={name:git('show',':'+name) for name in git('ls-files','-z').decode().split('\0') if name}
            count=validate_snapshot(staged)
        else:count=validate_snapshot(filesystem_snapshot())
        extra=[]
        if args.pre_push:
            import sys
            for line in sys.stdin:
                fields=line.split()
                if len(fields)!=4:raise ValueError('Invalid pre-push ref input')
                if not re.fullmatch(r'[0-9a-fA-F]{40}',fields[1]):raise ValueError('Invalid pre-push commit ID')
                if fields[1]!='0'*40:extra.append(fields[1])
        history=validate_history(extra) if args.all_refs or args.pre_push else 0
        artifacts=[]
        if args.artifacts:
            approved=json.loads((ROOT/MANIFEST).read_text(encoding='utf-8'))['files']
            for path in sorted(args.artifacts.iterdir()):
                if path.name.endswith(('.whl','.tar.gz','.tgz','.zip')):
                    artifacts.append({'name':path.name,'files':validate_artifact(path,approved),'sha256':sha(path.read_bytes())})
            if not artifacts:raise ValueError('No build artifacts found')
        print(json.dumps({'passed':True,'approved_files':count,'history_commits':history,'artifacts':artifacts}))
    except (ValueError,KeyError,OSError,subprocess.CalledProcessError) as error:
        parser.exit(1,'Publication blocked: '+str(error)+'\n')

if __name__=='__main__':main()
