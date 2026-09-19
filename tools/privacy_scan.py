#!/usr/bin/env python3
"""Bounded content/history lint; never authorizes public release.

CLI stdout, --json and --report are aggregate-only. In-process audit()/inspect()
results include private file locations and file-content identities: never publish
those results. Exact private exceptions bind path, line, rule and file bytes.
Known-identifier hashes are sensitive private policy, not anonymized identifiers.
This heuristic linter is not an independent approval authority or OS sandbox.
"""
from __future__ import annotations
import argparse
from collections import Counter
import hashlib
import json
import os
import re
import subprocess
import sys
import stat
from pathlib import Path

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_FILES = 20000
WINDOWS_PATH = re.compile(r'(?i)\b[A-Z]:(?:\\{1,2}|/)[^\'"<>|`\r\n]*')
USER_PATH = re.compile(r'(?i)(?:[\\/](?:Users|home)[\\/])([^\\/\s\'"<>]+)')
HOSTNAME = re.compile(r'\b(?:DESKTOP|LAPTOP)-[A-Za-z0-9-]{4,}\b', re.I)
TOKEN = re.compile(r'(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-(?:proj-)?[A-Za-z0-9_-]{24,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)')
ACCOUNT = re.compile(r'\b(?:ou_|on_)[A-Za-z0-9_-]{20,}\b')
ASSIGN = re.compile(r'''(?ix)\b(?:api[_-]?key|app[_-]?secret|password|access[_-]?token|refresh[_-]?token|cookie)\b["']?\s*[:=]\s*["']([^"'\r\n]{8,})["']''')
EMAIL = re.compile(r'\b[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b')
PUBLIC_FORBIDDEN = ('projects/', 'worker/runtime/', 'worker/logs/', 'worker/staged-publications/', 'docs/validation/')
PUBLIC_INSTANCE_FILES = {'supervisor/bootstrap.json', 'supervisor/portfolio.json', 'worker/remote-projects.json'}
SAFE_DOMAINS = {'example.test', 'example.invalid', 'example.com', 'example.org'}
SAFE_EMAILS = {'bridge-manual@users.noreply.github.com', 'git@github.com'}
SAFE_VALUE = re.compile(r'^(?:<[^>]+>|\$\{[A-Z_]+\}|REDACTED|__REDACTED__|CHANGEME|PLACEHOLDER|not-a-real-secret)$',re.I)
IDENTIFIER_TOKEN = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*')
IDENTIFIER_SEPARATOR = re.compile(r'[-_.]+')


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _identifier_forms(value: str) -> set[str]:
    """Return casefolded separator variants without retaining the value in findings."""

    folded = value.casefold()
    compact = IDENTIFIER_SEPARATOR.sub('', folded)
    forms = {folded, compact}
    for separator in '-_.':
        forms.add(IDENTIFIER_SEPARATOR.sub(separator, folded))
    forms.update(part for part in IDENTIFIER_SEPARATOR.split(folded) if part)
    return forms


def _identifier_digests(value: str) -> set[str]:
    return {sha(form.encode()) for form in _identifier_forms(value)}


def _load_identifier_policy(policy: Path | None) -> set[str]:
    """Load a PRIVATE hash-only policy; its hashes can be dictionary-matched."""

    if policy is None:
        return set()
    document = _read_json(policy)
    if not isinstance(document, dict) or set(document) != {'private_identifier_sha256'}:
        raise ValueError('invalid_identifier_policy')
    values = document.get('private_identifier_sha256', [])
    if not isinstance(values, list) or any(
        not isinstance(value, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', value)
        for value in values
    ):
        raise ValueError('identifier policy must contain only SHA-256 hex digests')
    return {value.casefold() for value in values}


def git(root: Path, *args: str) -> bytes:
    result = subprocess.run(['git','-C',str(root),*args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=60, check=True, creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0) if os.name=='nt' else 0)
    return result.stdout


def _linked(path: Path) -> bool:
    return path.is_symlink() or getattr(path, 'is_junction', lambda: False)()


def _read_json(path: Path):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError('duplicate_json_key')
            out[key] = value
        return out
    if any(_linked(x) for x in (path, *path.parents)):
        raise ValueError('linked_policy')
    if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError('invalid_policy_file')
    return json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite_json')))


def files(root: Path, tree: bool) -> list[Path]:
    if not root.is_dir() or any(_linked(x) for x in (root, *root.parents)):
        raise ValueError('invalid_or_linked_root')
    if _linked(root / '.git'):
        raise ValueError('linked_git_metadata')
    if (root / '.git').exists() and not tree:
        names = git(root, 'ls-files', '-z').decode('utf-8').split('\0')
        result = [root / n for n in names if n]
    else:
        result = []
        for directory, dirs, entries in os.walk(root, followlinks=False):
            for name in dirs:
                if _linked(Path(directory) / name):
                    raise ValueError('linked_input_directory')
            dirs[:] = [name for name in dirs if name != '.git']
            for name in entries:
                p = Path(directory) / name
                # Include broken symlinks rather than silently skipping them.
                if _linked(p) or not stat.S_ISREG(p.lstat().st_mode):
                    raise ValueError('nonregular_input')
                result.append(p)
                if len(result) > MAX_FILES:
                    raise ValueError('file_count_limit')
    if not result or len(result) > MAX_FILES:
        raise ValueError('empty_or_oversized_input')
    return sorted(result)


def safe_windows(value: str) -> bool:
    normalized=value.replace('\\\\','\\').replace('\\','/').lower()
    return normalized.startswith(('x:/synthetic/', 'c:/windows/', 'c:/program files/', 'c:/program files (x86)/', 'c:/programdata/ai-agent-bridge/phase86')) or normalized in {'c:/windows','c:/program files','c:/program files (x86)'}


def inspect(path: str, raw: bytes, *, mode: str, denied: set[str]) -> list[dict]:
    found=[];file_digest=sha(raw)
    def add(rule: str, value: str, offset: int=0, text: str='') -> None:
        found.append({'path':path,'line':text.count('\n',0,offset)+1 if text else 0,
                      'rule':rule,'file_sha256':file_digest})
    name=Path(path).name.lower()
    if name.startswith('.env') and name not in {'.env.example','.env.sample'} or name.endswith(('.pem','.pfx','.p12','.key')) or name in {'credentials.json','credentials.ini','secrets.json','secrets.yaml','secrets.yml','token.json','token.txt'} or name.endswith('.local.json'):
        add('secret-or-local-config-file',name)
    if mode=='public' and (path.startswith(PUBLIC_FORBIDDEN) or path in PUBLIC_INSTANCE_FILES):
        add('concrete-state-or-runtime-path',path)
    if len(raw)>MAX_FILE_BYTES:
        add('oversized-unscanned-file',path);return found
    try:text=raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        add('binary-unreviewed-file',path);return found
    for rx,rule in [(TOKEN,'credential-value'),(ASSIGN,'credential-assignment')] + ([(ACCOUNT,'account-identifier')] if mode=='public' else []):
        for match in rx.finditer(text):
            value=match.group(1) if rx is ASSIGN else match.group(0)
            if not SAFE_VALUE.fullmatch(value):add(rule,value,match.start(),text)
    if mode=='public':
        for rx,rule in [(WINDOWS_PATH,'absolute-windows-path'),(USER_PATH,'user-home-path'),(HOSTNAME,'machine-hostname'),(EMAIL,'personal-email')]:
            for match in rx.finditer(text):
                value=match.group(0)
                if rx is WINDOWS_PATH and safe_windows(value):continue
                if rx is EMAIL and (value.lower() in SAFE_EMAILS or value.rsplit('@',1)[-1].lower() in SAFE_DOMAINS):continue
                if rx is USER_PATH and match.group(1) in {'<user>','<username>','${USER}','%USERNAME%'}:continue
                add(rule,value,match.start(),text)
        # Known-identifier policy is PRIVATE: hashes can be dictionary-matched.
        for source,is_path in [(path,True),(text,False)]:
            seen=set()
            for match in IDENTIFIER_TOKEN.finditer(source):
                token=match.group(0).casefold();digests=_identifier_digests(token)
                if digests.intersection(denied) and (token,match.start()) not in seen:
                    seen.add((token,match.start()));add('known-private-identifier',token,match.start() if not is_path else 0,'' if is_path else text)
    return found


def audit(root: Path, *, mode: str='public', tree: bool=False, history: bool=False,
          require_clean_history: bool=False, allowlist: Path | None=None,
          identifier_policy: Path | None=None) -> dict:
    denied=_load_identifier_policy(identifier_policy)
    entries=[]
    if allowlist is not None:
        entries=_read_json(allowlist)
        if not isinstance(entries,list):raise ValueError('allowlist must be a list')
        for entry in entries:
            if (not isinstance(entry, dict)
                    or set(entry) != {'path','line','rule','file_sha256','reason'}
                    or not isinstance(entry['path'], str) or not entry['path']
                    or not isinstance(entry['rule'], str) or not entry['rule']
                    or type(entry['line']) is not int or entry['line'] < 0
                    or not isinstance(entry['file_sha256'], str)
                    or not re.fullmatch(r'[0-9a-f]{64}', entry['file_sha256'])
                    or not isinstance(entry['reason'], str) or not entry['reason'].strip()
                    or any(c in entry['path'] for c in '*?[')):
                raise ValueError('allowlist_must_be_exact_and_justified')
    findings=[];scanned=0;history_blobs=0;commits=None;used=set()
    for p in files(root,tree):
        rel=p.relative_to(root).as_posix()
        if p.is_symlink() or any(parent.is_symlink() for parent in p.parents if parent !=root):
            findings.append({'path':rel,'line':0,'rule':'symlink-refused','file_sha256':''});continue
        if p.stat().st_size > MAX_FILE_BYTES:
            findings.append({'path':rel,'line':0,'rule':'oversized-unscanned-file','file_sha256':''})
        else:
            findings+=inspect(rel,p.read_bytes(),mode=mode,denied=denied)
        scanned+=1
    if history or require_clean_history:
        if not (root/'.git').exists():raise ValueError('history audit requires Git repository')
        commit_lines=git(root,'rev-list','--all','--parents').decode().splitlines();commits=len(commit_lines)
        if require_clean_history and (len(commit_lines)!=1 or len(commit_lines[0].split())!=1):
            findings.append({'path':'.git','line':0,'rule':'initial-history-not-single-root','file_sha256':''})
        for commit_line in commit_lines:
            oid=commit_line.split()[0]
            findings += inspect('.git/commit/'+oid,git(root,'cat-file','commit',oid),mode=mode,denied=denied)
        for ref in git(root,'for-each-ref','--format=%(objectname) %(objecttype)','refs/tags').decode().splitlines():
            oid,kind=ref.split()
            if kind=='tag':findings += inspect('.git/tag/'+oid,git(root,'cat-file','tag',oid),mode=mode,denied=denied)
        for line in git(root,'rev-list','--objects','--all').decode().splitlines():
            parts=line.split(' ',1)
            if len(parts)!=2:continue
            oid,name=parts
            if git(root,'cat-file','-t',oid).strip()!=b'blob':continue
            size=int(git(root,'cat-file','-s',oid))
            if size>MAX_FILE_BYTES:raw=b'\0'*(MAX_FILE_BYTES+1)
            else:raw=git(root,'cat-file','blob',oid)
            items=inspect(name,raw,mode=mode,denied=denied)
            for item in items:item['git_object']=oid
            findings+=items;history_blobs+=1
    explained=[];unexplained=[]
    for item in findings:
        matched=False
        for i,entry in enumerate(entries):
            if all(item.get(k)==entry[k] for k in ('path','line','rule','file_sha256')):
                explained.append({**item,'explanation':entry['reason']});used.add(i);matched=True;break
        if not matched:unexplained.append(item)
    stale=[entry for i,entry in enumerate(entries) if i not in used]
    return {'schema_version':1,'mode':mode,'files_scanned':scanned,'history_blobs_scanned':history_blobs,
            'reachable_commits':commits,'unexplained_findings':unexplained,'explained_findings':explained,
            'unused_allowlist_entries':stale,'pass':not unexplained and not stale}


def summary(result: dict) -> dict:
    """No matched text, paths, hashes, exception text or exception reasons."""
    return {
        'schema_version': 2,
        'scope': 'content_lint_only',
        'publication_authorized': False,
        'pass': result['pass'],
        'files_scanned': result.get('files_scanned', 0),
        'history_blobs_scanned': result.get('history_blobs_scanned', 0),
        'reachable_commits': result.get('reachable_commits'),
        'finding_counts': dict(sorted(Counter(x['rule'] for x in result.get('unexplained_findings', [])).items())),
        'explained_count': len(result.get('explained_findings', [])),
        'unused_allowlist_count': len(result.get('unused_allowlist_entries', [])),
    }


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's usual message echoes supplied paths/values.
        self.exit(2, '{"pass":false,"publication_authorized":false,"reason_code":"invalid_arguments"}\n')


def _outputs(root: Path, values: list[Path | None]) -> list[Path]:
    result = []
    for value in values:
        if value is None:
            continue
        path = value.absolute()
        if any(_linked(p) for p in (path, *path.parents)):
            raise ValueError('linked_output')
        path = path.resolve()
        if path == root or root in path.parents or path in result or path.exists():
            raise ValueError('invalid_or_existing_output')
        result.append(path)
    return result


def main() -> int:
    p=_Parser(description=__doc__)
    p.add_argument('--root',type=Path,required=True);p.add_argument('--mode',choices=['public','private'],default='public')
    p.add_argument('--tree',action='store_true');p.add_argument('--history',action='store_true');p.add_argument('--require-clean-history',action='store_true')
    p.add_argument('--allowlist',type=Path);p.add_argument('--identifier-policy',type=Path)
    p.add_argument('--json',type=Path);p.add_argument('--report',type=Path)
    a=p.parse_args()
    try:
        root = a.root.absolute()
        if any(_linked(x) for x in (root, *root.parents)):
            raise ValueError('linked_root')
        root = root.resolve()
        outputs = _outputs(root, [a.json, a.report])
        r=summary(audit(root,mode=a.mode,tree=a.tree,history=a.history,
                        require_clean_history=a.require_clean_history,
                        allowlist=a.allowlist,identifier_policy=a.identifier_policy))
        rendered=json.dumps(r,indent=2)+'\n'
        texts=[]
        if a.json:
            texts.append(rendered)
        if a.report:
            lines=['# Content lint summary (not release approval)',
                   'Publication authorized: false', f'Lint pass: {r["pass"]}',
                   f'Files scanned: {r["files_scanned"]}']
            lines += [f'- {rule}: {count}' for rule,count in r['finding_counts'].items()]
            lines += [f'Unused exceptions: {r["unused_allowlist_count"]}']
            texts.append('\n'.join(lines)+'\n')
        for path,text in zip(outputs,texts):
            path.parent.mkdir(parents=True,exist_ok=True)
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
            with os.fdopen(fd,'w',encoding='utf-8') as stream:
                stream.write(text)
    except (OSError,ValueError,TypeError,KeyError,AttributeError,RecursionError,subprocess.SubprocessError):
        r={'pass':False,'publication_authorized':False,'reason_code':'scan_input_or_environment_error'}
        rendered=json.dumps(r,sort_keys=True)+'\n'
    print(rendered,end='');return 0 if r['pass'] else 1

if __name__=='__main__':raise SystemExit(main())
