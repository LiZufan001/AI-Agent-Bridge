#!/usr/bin/env python3
"""Read-only State validation using this accepted Engine's schemas and Protocol v2.

This is NOT a recovery consumer or publication authority. Runtime structural
inspection does not prove settlement; --require-quiescent adds fail-closed gates.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ENGINE/'worker'), str(ENGINE/'supervisor'), str(ENGINE/'tools')]
import state_roots
import project_view
import protocol_core
import recovery_journal
import schema_check
import privacy_scan


def load(path: Path):
    if path.is_symlink():
        raise ValueError('linked evidence refused: ' + path.name)
    return schema_check.strict_json(path.read_bytes())


def canonical_hashes(root: Path) -> dict[str,str]:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((root/'projects').rglob('*')) if p.is_file()}




def _owner_selected_projects(root: Path) -> set[str]:
    """Return current owner-selected project ids; invalid structure fails closed."""
    value = load(root/'supervisor/portfolio.json')
    projects = value.get('projects') if isinstance(value, dict) else None
    if not isinstance(projects, list):
        raise ValueError('portfolio projects invalid')
    selected: set[str] = set()
    for entry in projects:
        if not isinstance(entry, dict) or not isinstance(entry.get('project_id'), str):
            raise ValueError('portfolio project invalid')
        if entry.get('owner_selected') is True:
            selected.add(entry['project_id'])
    return selected


def _explicitly_disabled_projects(root: Path) -> set[str]:
    """Return projects present in the registry and disabled on every configured host.

    Absence from the registry is not treated as disabled. Any non-False enabled
    value keeps the project on the operational validation tier (fail closed).
    """
    value = load(root/'worker/remote-projects.json')
    hosts = value.get('hosts') if isinstance(value, dict) else None
    if not isinstance(hosts, dict):
        raise ValueError('remote project hosts invalid')
    seen: set[str] = set()
    not_disabled: set[str] = set()
    for host in hosts.values():
        if not isinstance(host, dict):
            raise ValueError('remote project host invalid')
        projects = host.get('projects', {})
        if not isinstance(projects, dict):
            raise ValueError('remote project registry invalid')
        for pid, entry in projects.items():
            if not isinstance(pid, str) or not isinstance(entry, dict):
                raise ValueError('remote project entry invalid')
            seen.add(pid)
            if entry.get('enabled') is not False:
                not_disabled.add(pid)
    return seen - not_disabled


def _is_terminal_archive(
    state: dict, project_id: str, *, selected: set[str], disabled: set[str]
) -> bool:
    """True only for an explicitly non-executable terminal historical project."""
    return (
        state.get('status') in protocol_core.TERMINAL_STATES
        and state.get('active_run') is None
        and project_id not in selected
        and project_id in disabled
    )


def validate_state(root: Path, *, require_quiescent: bool=False, include_runtime: bool=False) -> dict:
    root = state_roots.resolve_state_root(root)
    errors=[];warnings=[];states=[];runtime_blockers=[]
    def check_file(relative: str, schema_relative: str):
        try:
            return schema_check.validate(load(root/relative),load(ENGINE/schema_relative),relative)
        except (OSError, ValueError) as exc:
            return [relative + ': ' + type(exc).__name__]
    errors += check_file('bridge-state.json','schemas/engine-state.schema.json')
    errors += check_file('supervisor/bootstrap.json','supervisor/schemas/bootstrap.schema.json')
    errors += check_file('supervisor/portfolio.json','supervisor/schemas/portfolio.schema.json')
    archival_projects: set[str] = set()
    try:
        selected_projects = _owner_selected_projects(root)
        disabled_projects = _explicitly_disabled_projects(root)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # Classification uncertainty must never relax validation. Every project
        # stays on the operational tier and the malformed authority is reported.
        selected_projects = set()
        disabled_projects = set()
        errors.append('archival classification unavailable: '+type(exc).__name__)
    for path in sorted((root/'projects').glob('*/state.json')):
        pid=path.parent.name
        try:
            state=load(path)
            errors += schema_check.validate(state,load(ENGINE/'protocol/v2/state.schema.json'),f'projects/{pid}/state.json')
            archival = _is_terminal_archive(
                state, pid, selected=selected_projects, disabled=disabled_projects
            )
            view=project_view.project_view(root,pid,archival=archival)
            if archival and view.get('active_goal') is not None:
                errors.append('project '+pid+': TERMINAL_ARCHIVE_ACTIVE_GOAL')
            if archival:
                archival_projects.add(pid)
            states.append({'project_id':pid,'validation_tier':'archival' if archival else 'operational',**view['canonical']})
            if state.get('active_run') or state.get('status') in {'CODEX_RUNNING','COMMAND_READY','FINALIZE_PENDING','FINALIZING','RECOVERY_REQUIRED'}:
                runtime_blockers.append('non-quiescent canonical state: '+pid)
        except (OSError,ValueError,RuntimeError) as exc:
            errors.append('project '+pid+': '+str(exc))
    try:
        overview=project_view.overview(root)
        if not overview['healthy']:
            errors.append('selected portfolio contains invalid/missing project')
    except (OSError,ValueError,KeyError,TypeError,RuntimeError) as exc:
        errors.append('portfolio: '+type(exc).__name__)
    try:
        completed=subprocess.run([sys.executable,'-B',str(ENGINE/'protocol/v2/check_conformance.py'),'--state-root',str(root)],
             capture_output=True,text=True,encoding='utf-8',timeout=60,check=False)
        if completed.returncode:
            errors.append('existing Protocol v2 conformance failed')
        conformance=completed.stdout+completed.stderr
    except (OSError,subprocess.SubprocessError) as exc:
        conformance=type(exc).__name__;errors.append('conformance unavailable')
    requests=root/'worker/staged-publications/requests'
    for p in sorted(requests.glob('*')):
        if p.name in {'.gitkeep','.keep','README.md'}:
            continue
        if not p.is_file() or p.suffix!='.json':
            errors.append('unexpected inbox entry: '+p.name);continue
        try:
            request=load(p)
            errors += schema_check.validate(request,load(ENGINE/'protocol/v2/staged-publication.schema.json'),p.name)
            if p.name!='request-'+request['request_id']+'.json':
                errors.append('request filename identity mismatch: '+p.name)
            if hashlib.sha256(request['command_content'].encode('utf-8')).hexdigest()!=request['command_sha256']:
                errors.append('request byte hash mismatch: '+p.name)
            if request.get('project_id') in archival_projects:
                errors.append('archived terminal project has staged request: '+p.name)
            runtime_blockers.append('staged request remains: '+p.name)
        except (OSError,ValueError,KeyError,TypeError):
            errors.append('invalid staged request: '+p.name)
    if include_runtime:
        runtime=root/'worker/runtime'
        for p in sorted(runtime.rglob('*.json')):
            try:
                value=load(p)
                if 'recovery' in p.relative_to(runtime).parts and isinstance(value,dict) and 'journal_status' in value:
                    recovery_journal.validate_journal(value)
                    if value.get('journal_status') not in {'reconciled','superseded'} or value.get('remote_publish_pending'):
                        runtime_blockers.append('unsettled recovery: '+p.relative_to(root).as_posix())
                elif any(term in p.name for term in ('handoff','pending','recovery')):
                    runtime_blockers.append('runtime evidence needs adjudication: '+p.relative_to(root).as_posix())
            except (OSError,ValueError,RuntimeError):
                errors.append('invalid runtime JSON: '+p.relative_to(root).as_posix())
        for p in runtime.rglob('pending-report-*.md'):
            runtime_blockers.append('pending report bytes remain: '+p.relative_to(root).as_posix())
        for p in runtime.rglob('*.jsonl'):
            runtime_blockers.append('WAL requires settled-runtime proof: '+p.relative_to(root).as_posix())
    if require_quiescent:
        errors+=runtime_blockers
        if not include_runtime:
            errors.append('quiescence requires explicit --include-runtime')
    try:
        privacy=privacy_scan.audit(root,mode='private')
        if not privacy['pass']:
            errors.append('tracked secret-value/local-config audit failed')
    except (OSError,ValueError,subprocess.SubprocessError) as exc:
        privacy={'pass':False,'scanner_error':type(exc).__name__};errors.append('private audit unavailable')
    # The State repository must not become a second source-code authority.
    for p in privacy_scan.files(root,tree=not (root/'.git').exists()):
        rel=p.relative_to(root).as_posix()
        if rel.startswith(('worker/runtime/','worker/logs/')) and (root/'.git').exists():
            errors.append('runtime/log tracked in State: '+rel)
        if p.suffix.lower() in {'.py','.ps1','.cmd','.js','.cs'}:
            errors.append('executable implementation in State: '+rel)
    hashes=canonical_hashes(root)
    return {'schema_version':1,'pass':not errors,'errors':errors,'warnings':warnings,
            'projects':states,'canonical_evidence_files':len(hashes),
            'canonical_evidence_sha256':hashlib.sha256(json.dumps(hashes,sort_keys=True).encode()).hexdigest(),
            'conformance_output':conformance,'runtime_inspected':include_runtime,
            'runtime_blockers':runtime_blockers,'privacy':privacy}


def main() -> int:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--state-root',type=Path,required=True)
    p.add_argument('--output',type=Path)
    p.add_argument('--include-runtime',action='store_true')
    p.add_argument('--require-quiescent',action='store_true')
    a=p.parse_args()
    try:
        result=validate_state(a.state_root.resolve(),require_quiescent=a.require_quiescent,include_runtime=a.include_runtime)
    except (OSError,ValueError,RuntimeError) as exc:
        result={'pass':False,'errors':[str(exc)]}
    text=json.dumps(result,indent=2,ensure_ascii=False)+'\n'
    if a.output:
        a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(text,encoding='utf-8')
    print(text,end='')
    return 0 if result['pass'] else 1

if __name__=='__main__':
    raise SystemExit(main())
