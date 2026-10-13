#!/usr/bin/env python3
"""Deterministic, non-executing planning and task-transport helpers.

Preparation is not publication, authorization, claim, or execution. State and
Owner controls remain authoritative; the existing gateway revalidates every
input. A context view is disposable and never a second state machine.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

import git_store
import protocol_core
import state_roots
from request_privacy import contains_secret_value
from supervisor_publication_gateway_core import (
    MAX_COMMAND_BYTES, StagedPublicationError, StagedPublicationRequest,
    validate_exact_command_for_publication,
)

_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}')
_DIGEST = re.compile(r'[0-9a-f]{64}')
SUPPORTED_FEATURES = (
    'request_prepare_v1', 'fixed_owner_ring_v1', 'compact_context_v1',
    'recovery_advisory_v1',
)


class PreparationError(ValueError):
    """Safe fixed reason, never a source/body/credential echo."""


def _id(value: object) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise PreparationError('invalid_identifier')
    return value


def _integer(value: object, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise PreparationError('invalid_integer')
    return value


def _json(raw: bytes) -> dict[str, Any]:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise PreparationError('duplicate_json_key')
            result[key] = value
        return result
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (UnicodeError, ValueError) as exc:
        raise PreparationError('invalid_json') from exc
    if not isinstance(value, dict):
        raise PreparationError('json_object_required')
    return value


def choose_focus(projects: Sequence[Mapping[str, Any]], eligible_ids: Sequence[str],
                 *, hour_bucket: int, preempt_project: str | None = None,
                 preemption_reason: str | None = None) -> dict[str, Any]:
    """Anchor rotation in the selected-owner ring, then skip ineligible slots.

    Paused members stay in the anchor ring but cannot be selected. Only an
    Owner selection/rank change changes the ring. Safety preemption must have
    been justified by the caller; this function cannot authorize it.
    """
    bucket = _integer(hour_bucket)
    if len(projects) > 512:
        raise PreparationError('portfolio_too_large')
    seen: set[str] = set()
    selected = []
    for item in projects:
        if not isinstance(item, Mapping):
            raise PreparationError('project_object_required')
        project_id = _id(item.get('project_id'))
        if project_id in seen:
            raise PreparationError('duplicate_project')
        seen.add(project_id)
        if type(item.get('owner_selected')) is not bool or type(item.get('owner_paused', False)) is not bool:
            raise PreparationError('invalid_owner_selection')
        if item['owner_selected']:
            rank = _integer(item.get('priority_rank'))
            selected.append((rank, project_id, item.get('owner_paused', False)))
    selected.sort(key=lambda row: (row[0], row[1]))
    ring = [row[1] for row in selected]
    paused = {row[1] for row in selected if row[2]}
    eligible = {_id(item) for item in eligible_ids}
    if not eligible.issubset(set(ring)):
        raise PreparationError('eligible_outside_selected_ring')
    eligible -= paused
    start = bucket % len(ring) if ring else None
    ordered = ring[start:] + ring[:start] if start is not None else []
    focus = next((p for p in ordered if p in eligible), None)
    mode = 'ordinary_rotation'
    if preempt_project is not None:
        if (_id(preempt_project) not in ring or preempt_project in paused
            or preemption_reason not in {'fresh_recovery', 'control_plane_safety', 'new_owner_direction'}):
            raise PreparationError('preemption_not_eligible')
        focus, mode = preempt_project, 'caller_justified_preemption'
    return {'schema_version': 1, 'owner_ring': ring, 'anchor_index': start,
            'eligible_ring': [p for p in ring if p in eligible], 'focus_project': focus,
            'mode': mode, 'deferred': [p for p in ring if p in eligible and p != focus]}


@dataclass(frozen=True)
class PreparedRequest:
    request_id: str
    filename: str
    request_bytes: bytes
    command_bytes: bytes
    state_sha256: str
    expires_at: str

    def summary(self) -> dict[str, Any]:
        envelope = _json(self.request_bytes)
        return {'status': 'PREPARED_NOT_SUBMITTED', 'request_id': self.request_id,
                'filename': self.filename, 'project_id': envelope['project_id'],
                'command_id': envelope['command_id'],
                'based_on_report': envelope['based_on_report'],
                'expected_generation': envelope['expected_generation'],
                'state_sha256': self.state_sha256,
                'command_sha256': sha256(self.command_bytes).hexdigest(),
                'request_sha256': sha256(self.request_bytes).hexdigest(),
                'created_at': envelope['created_at'], 'expires_at': self.expires_at,
                'authorization_verified': False, 'execution_started': False}


def prepare_request(state_bytes: bytes, body: str, *, producer: str,
                    now: datetime | None = None, request_id: str | None = None,
                    executor: Mapping[str, str] | None = None,
                    max_lifetime_seconds: int = 600) -> PreparedRequest:
    """Pure builder. Producer is an attestation, never independent proof."""
    if not isinstance(state_bytes, bytes) or len(state_bytes) > 256 * 1024:
        raise PreparationError('state_size_invalid')
    state = _json(state_bytes)
    project_id = _id(state.get('project_id'))
    if state.get('protocol_version') != 2 or state.get('status') != 'REPORT_READY':
        raise PreparationError('state_not_report_ready')
    if state.get('active_run') is not None or state.get('finalized') is True or state.get('human_required') is True:
        raise PreparationError('state_not_eligible')
    if producer not in ('scheduled_chatgpt', 'finalizer'):
        raise PreparationError('unsupported_producer')
    if not isinstance(body, str) or not body.strip() or '\x00' in body:
        raise PreparationError('invalid_body')
    if re.search(r'bridge-command\s*:', body, re.IGNORECASE):
        raise PreparationError('body_must_not_supply_command_metadata')
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise PreparationError('timezone_required')
    lifetime = _integer(max_lifetime_seconds, minimum=1)
    if lifetime > 600:
        raise PreparationError('lifetime_exceeds_preparation_bound')
    request_id = _id(request_id or uuid.uuid4().hex)
    command_id = _integer(state.get('latest_command')) + 1
    based_on_report = _integer(state.get('latest_report'))
    expected_generation = _integer(state.get('generation')) + 1
    kind = 'FINALIZE' if producer == 'finalizer' else 'EXECUTE'
    metadata: dict[str, Any] = {'command_id': command_id, 'source': producer,
        'kind': kind, 'based_on_report': based_on_report,
        'expected_generation': expected_generation}
    if executor is not None:
        metadata['executor'] = dict(executor)
    header = json.dumps(metadata, ensure_ascii=False, separators=(',', ':'))
    try:
        command = (f'<!-- bridge-command: {header} -->\n' + body).encode('utf-8')
    except UnicodeError as exc:
        raise PreparationError('invalid_body_encoding') from exc
    if len(command) > MAX_COMMAND_BYTES:
        raise PreparationError('command_too_large')
    target_status = 'FINALIZING' if kind == 'FINALIZE' else 'COMMAND_READY'
    projected = {**state, 'status': target_status, 'generation': expected_generation,
        'latest_command': command_id, 'last_reviewed_report': based_on_report,
        'active_run': None}
    validate_exact_command_for_publication(command, state=projected,
        target_status=target_status, command_id=command_id)
    created_at = now.astimezone(timezone.utc).isoformat(timespec='seconds')
    envelope = {'schema_version': 1, 'request_id': request_id, 'project_id': project_id,
        **metadata, 'command_sha256': sha256(command).hexdigest(),
        'command_content': command.decode('utf-8'), 'created_at': created_at}
    # Executor belongs only to command metadata, never the transport schema.
    envelope.pop('executor', None)
    raw = (json.dumps(envelope, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    StagedPublicationRequest.from_bytes(raw, filename_request_id=request_id)
    return PreparedRequest(request_id, f'request-{request_id}.json', raw, command,
        sha256(state_bytes).hexdigest(),
        (now + timedelta(seconds=lifetime)).astimezone(timezone.utc).isoformat(timespec='seconds'))


def prepare_from_repository(root: Path, project_id: str, body: str, *, producer: str,
                            request_id: str | None = None, executor=None) -> tuple[PreparedRequest, str]:
    root = state_roots.resolve_state_root(root, for_write=False)
    project_id = _id(project_id)
    head = git_store.get_head(root)
    if head is None:
        raise PreparationError('state_git_identity_unavailable')
    if git_store.tracked_dirty(root):
        raise PreparationError('state_has_uncommitted_changes')
    raw = git_store.read_blob(root, f'projects/{project_id}/state.json', max_bytes=256 * 1024, revision=head)
    if _json(raw).get('project_id') != project_id:
        raise PreparationError('project_identity_mismatch')
    bootstrap = _json(git_store.read_blob(root, 'supervisor/bootstrap.json', max_bytes=64 * 1024, revision=head))
    lifetime = bootstrap.get('staged_publication', {}).get('max_lifetime_seconds', 600)
    prepared = prepare_request(raw, body, producer=producer, request_id=request_id,
                               executor=executor, max_lifetime_seconds=lifetime)
    if git_store.get_head(root) != head:
        raise PreparationError('state_snapshot_changed')
    return prepared, head


def write_staged(root: Path, prepared: PreparedRequest, *, expected_head: str,
                 producer_attestation: str) -> dict[str, Any]:
    """Create-only transport file; no commit, push, canonical mutation or model."""
    root = state_roots.resolve_state_root(root, for_write=True)
    parsed = StagedPublicationRequest.from_bytes(prepared.request_bytes,
                                                  filename_request_id=prepared.request_id)
    if producer_attestation != parsed.source:
        raise PreparationError('producer_attestation_mismatch')
    if prepared.filename != f'request-{parsed.request_id}.json':
        raise PreparationError('filename_identity_mismatch')
    if prepared.command_bytes != parsed.command_bytes:
        raise PreparationError('prepared_command_bytes_mismatch')
    if not isinstance(prepared.state_sha256, str) or not _DIGEST.fullmatch(prepared.state_sha256):
        raise PreparationError('state_digest_invalid')
    bootstrap = _json(git_store.read_blob(root, 'supervisor/bootstrap.json', max_bytes=64 * 1024))
    settings = bootstrap.get('staged_publication', {})
    if not isinstance(settings, dict):
        raise PreparationError('staging_configuration_invalid')
    lifetime = _integer(settings.get('max_lifetime_seconds', 600), minimum=1)
    skew = _integer(settings.get('max_future_skew_seconds', 60))
    if lifetime > 600 or skew > 60:
        raise PreparationError('staging_configuration_exceeds_supported_bound')
    created = datetime.fromisoformat(parsed.created_at.replace('Z', '+00:00'))
    age = (datetime.now(timezone.utc) - created).total_seconds()
    if age > lifetime or age < -skew:
        raise PreparationError('prepared_request_expired_or_future')
    with git_store.git_mutation_gate(root):
        if git_store.get_head(root) != expected_head or git_store.tracked_dirty(root):
            raise PreparationError('state_snapshot_changed')
        raw = git_store.read_blob(root, f'projects/{parsed.project_id}/state.json', max_bytes=256 * 1024)
        if sha256(raw).hexdigest() != prepared.state_sha256:
            raise PreparationError('state_snapshot_changed')
        # Rebuild identity from actual State, never from an untrusted summary.
        state = _json(raw)
        if (state.get('status') != 'REPORT_READY' or state.get('active_run') is not None
            or state.get('finalized') is True or state.get('human_required') is True
            or parsed.command_id != _integer(state.get('latest_command')) + 1
            or parsed.expected_generation != _integer(state.get('generation')) + 1
            or parsed.based_on_report != state.get('latest_report')):
            raise PreparationError('prepared_identity_stale')
        directory = root / 'worker' / 'staged-publications' / 'requests'
        state_roots.assert_no_links(directory)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / prepared.filename
        state_roots.assert_no_links(path)
        with path.open('xb') as stream:
            stream.write(prepared.request_bytes)
            stream.flush()
            import os
            os.fsync(stream.fileno())
    return {**prepared.summary(), 'status': 'STAGED_LOCAL_ONLY',
            'relative_path': path.relative_to(root).as_posix(),
            'git_commit_performed': False, 'gateway_acceptance_verified': False}


def recovery_advisory(state: Mapping[str, Any], journal: Mapping[str, Any] | None) -> dict[str, Any]:
    """No recovery permission is inferred from these classifications."""
    result = {'classification': 'evidence_required', 'automatic_retry_allowed': False,
              'canonical_state_changed': False}
    if not isinstance(journal, Mapping):
        return result
    keys = ('project_id', 'command_id', 'run_id', 'claim_generation')
    identity = {k: journal.get(k) for k in keys}
    result['run_identity'] = identity
    if (identity['project_id'] != state.get('project_id')
        or identity['command_id'] != state.get('latest_command')
        or not isinstance(identity['run_id'], str)
        or not identity['run_id'] or type(identity['claim_generation']) is not int):
        return {**result, 'classification': 'identity_mismatch'}
    if state.get('active_run') is not None:
        return {**result, 'classification': 'active_run_do_not_resume'}
    if journal.get('external_side_effects_unknown') is True:
        return {**result, 'classification': 'external_effects_require_review'}
    if (journal.get('remote_publish_pending') is True
        and journal.get('external_side_effects_unknown') is False
        and journal.get('process_exit_code') is not None
        and journal.get('pending_report_path')):
        return {**result, 'classification': 'report_publication_evidence_candidate'}
    return result


def _focus_excerpt(root: Path, relative: str, head: str, limit: int = 2048) -> dict[str, Any]:
    result = {'path': relative, 'state_commit': head, 'content_is_untrusted_data': True}
    try:
        raw = git_store.read_blob(root, relative, max_bytes=256 * 1024, revision=head)
        text = raw.decode('utf-8')
    except (git_store.WorkerError, UnicodeError):
        return {**result, 'available': False, 'reason': 'source_unavailable_or_invalid'}
    result.update(available=True, source_sha256=sha256(raw).hexdigest(), source_bytes=len(raw))
    # Do not carry stderr, transcripts or sensitive assignments into a summary.
    if contains_secret_value(text):
        return {**result, 'excerpt': None, 'reason': 'source_requires_direct_authorized_review'}
    cutoff = next((i for i, line in enumerate(text.splitlines())
                   if line.startswith(('## Worker diagnostics', '## Preserved Interrupted', '## Codex final response'))), None)
    excerpt_source = '\n'.join(text.splitlines()[:cutoff]) if cutoff is not None else text
    return {**result, 'excerpt': excerpt_source[:limit],
            'truncated': len(excerpt_source) > limit or cutoff is not None}


def compact_context(root: Path, focus: str | None = None) -> dict[str, Any]:
    """Bounded immutable-commit references; optional focus data, never a gate."""
    root = state_roots.resolve_state_root(root, for_write=False)
    head = git_store.get_head(root)
    if head is None:
        raise PreparationError('state_git_identity_unavailable')
    portfolio = _json(git_store.read_blob(root, 'supervisor/portfolio.json', max_bytes=64 * 1024, revision=head))
    projects = portfolio.get('projects')
    if not isinstance(projects, list):
        raise PreparationError('portfolio_invalid')
    # Validate all identity/rank/boolean fields with the same deterministic helper.
    choose_focus(projects, [], hour_bucket=0)
    selected = [p for p in projects if p['owner_selected']]
    if focus is not None and _id(focus) not in {p['project_id'] for p in selected}:
        raise PreparationError('focus_outside_selected_ring')
    rows = []
    for p in sorted(selected, key=lambda item: (item['priority_rank'], item['project_id'])):
        project_id = p['project_id']
        relative = f'projects/{project_id}/state.json'
        raw = git_store.read_blob(root, relative, max_bytes=256 * 1024, revision=head)
        state = _json(raw)
        if state.get('project_id') != project_id:
            raise PreparationError('project_identity_mismatch')
        row = {k: state.get(k) for k in ('project_id', 'status', 'generation',
            'latest_command', 'latest_report', 'last_reviewed_report', 'human_required')}
        for key in ('generation', 'latest_command', 'latest_report', 'last_reviewed_report'):
            _integer(row[key])
        row.update({'owner_paused': p['owner_paused'] if 'owner_paused' in p else False,
                    'state_path': relative, 'state_sha256': sha256(raw).hexdigest(),
                    'active_run_present': state.get('active_run') is not None,
                    'needs_report_review': row['latest_report'] > row['last_reviewed_report']})
        latest = row['latest_report']
        row['latest_report_path'] = f'projects/{project_id}/reports/report-{latest:03d}.md' if latest else None
        row['goal_path'] = f'projects/{project_id}/CURRENT_GOAL.md'
        row['deep_review_performed'] = False
        if project_id == focus:
            row['focus_sources'] = [_focus_excerpt(root, row['goal_path'], head)]
            if latest:
                row['focus_sources'].append(_focus_excerpt(root, row['latest_report_path'], head))
        rows.append(row)
    if git_store.get_head(root) != head:
        raise PreparationError('state_snapshot_changed')
    return {'schema_version': 1, 'kind': 'disposable_planning_context',
            'state_commit': head, 'observed_at': datetime.now(timezone.utc).isoformat(),
            'freshness': 'local_git_snapshot_not_remote_or_heartbeat_proof',
            'source_policy': 'read_original_evidence_before_decision',
            'features': list(SUPPORTED_FEATURES), 'protocol_version': 2,
            'focus_project': focus, 'projects': rows, 'execution_authorized': False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    prepare = sub.add_parser('prepare', help='preview a request; no write by default')
    prepare.add_argument('--state-root', type=Path, required=True)
    prepare.add_argument('--project', required=True)
    prepare.add_argument('--body-file', type=Path, required=True)
    prepare.add_argument('--producer', choices=['scheduled_chatgpt', 'finalizer'], required=True)
    prepare.add_argument('--request-id')
    prepare.add_argument('--write-staged', action='store_true')
    prepare.add_argument('--emit-request', action='store_true', help='emit exact validated envelope text, not a submission')
    context = sub.add_parser('context', help='current compact references without mutation')
    context.add_argument('--state-root', type=Path, required=True)
    context.add_argument('--focus')
    recovery = sub.add_parser('recovery', help='exact-run local evidence classification; never mutate or retry')
    recovery.add_argument('--state-root', type=Path, required=True)
    recovery.add_argument('--project', required=True)
    recovery.add_argument('--run-id', required=True)
    sub.add_parser('capabilities', help='this code revision, not a production claim')
    args = parser.parse_args(argv)
    try:
        if args.command == 'capabilities':
            result = {'schema_version': 1, 'features': list(SUPPORTED_FEATURES),
                      'engine_commit': git_store.get_head(Path(__file__).resolve().parents[1]),
                      'protocol_version': 2, 'production_deployment_verified': False,
                      'implementation_sha256': sha256(Path(__file__).read_bytes()).hexdigest()}
        elif args.command == 'context':
            result = compact_context(args.state_root, args.focus)
        elif args.command == 'recovery':
            root = state_roots.resolve_state_root(args.state_root, for_write=False)
            project_id, run_id = _id(args.project), _id(args.run_id)
            state_raw = git_store.read_blob(root, f'projects/{project_id}/state.json', max_bytes=256 * 1024)
            journal_raw = state_roots.read_regular_bytes(root/'worker'/'runtime'/project_id/'recovery'/f'{run_id}.json', max_bytes=256 * 1024)
            journal = _json(journal_raw)
            if journal.get('run_id') != run_id:
                raise PreparationError('run_identity_mismatch')
            result = {**recovery_advisory(_json(state_raw), journal), 'state_sha256': sha256(state_raw).hexdigest(), 'journal_sha256': sha256(journal_raw).hexdigest()}
        else:
            raw = state_roots.read_regular_bytes(args.body_file, max_bytes=MAX_COMMAND_BYTES)
            prepared, head = prepare_from_repository(args.state_root, args.project,
                raw.decode('utf-8'), producer=args.producer, request_id=args.request_id)
            result = {**prepared.summary(), 'state_commit': head}
            if args.emit_request:
                result['request_utf8'] = prepared.request_bytes.decode('utf-8')
            if args.write_staged:
                result = write_staged(args.state_root, prepared, expected_head=head,
                                      producer_attestation=args.producer)
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except (ValueError, OSError, git_store.WorkerError, protocol_core.ProtocolViolation,
            state_roots.StateRootError) as exc:
        # Only known fixed reasons may leave the tool; never raw paths or output.
        reason = str(exc) if isinstance(exc, (PreparationError, StagedPublicationError)) else 'source_or_transport_unavailable'
        if re.fullmatch(r'[a-z_]{1,96}', reason) is None:
            reason = 'validation_failed'
        print(json.dumps({'status': 'REJECTED', 'reason': reason, 'error_class': type(exc).__name__,
                          'execution_started': False, 'automatic_retry': False}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
