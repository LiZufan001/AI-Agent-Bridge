import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import git_store
import bridge_worker as bw
import protocol_core
from supervisor_publication_gateway import (
    HISTORY_DIRECTORY_NAME,
    MAX_SCAN_ENTRIES,
    MAX_REQUESTS_PER_POLL,
    SupervisorPublicationGateway,
)


class SupervisorPublicationGatewayIntegrationTests(unittest.TestCase):
    """Exercise the staged boundary against a disposable bare Git remote."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="bridge-supervisor-gateway-")
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote.git"
        self.worker = self.root / "worker"
        self.competitor = self.root / "competitor"
        self._git(self.root, "init", "--bare", str(self.remote))
        self._git(self.root, "clone", str(self.remote), str(self.worker))
        self._identity(self.worker, "Gateway Worker", "gateway-worker@example.test")
        self._init_project(self.worker, "p")
        self._git(self.worker, "add", ".")
        self._git(self.worker, "commit", "-m", "initial disposable bridge state")
        self._git(self.worker, "branch", "-M", "main")
        self._git(self.worker, "push", "-u", "origin", "main")
        self._git(self.root, "clone", "--branch", "main", str(self.remote), str(self.competitor))
        self._identity(
            self.competitor,
            "Gateway Competitor",
            "gateway-competitor@example.test",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _git(self, cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        if check and result.returncode != 0:
            self.fail(
                f"git {' '.join(args)} failed ({result.returncode})\n"
                f"stdout={result.stdout}\nstderr={result.stderr}"
            )
        return result

    def _identity(self, repository: Path, name: str, email: str) -> None:
        self._git(repository, "config", "user.name", name)
        self._git(repository, "config", "user.email", email)

    def _init_project(
        self,
        repository: Path,
        project_id: str,
        *,
        generation: int = 10,
        latest_command: int = 5,
        latest_report: int = 5,
        status: str = "REPORT_READY",
        active_run: object | None = None,
    ) -> None:
        project = repository / "projects" / project_id
        (project / "commands").mkdir(parents=True, exist_ok=True)
        (project / "reports").mkdir(parents=True, exist_ok=True)
        (project / "commands" / ".keep").write_text("\n", encoding="utf-8")
        (project / "reports" / ".keep").write_text("\n", encoding="utf-8")
        (project / "MISSION.md").write_text(
            f"disposable mission for {project_id}\n", encoding="utf-8"
        )
        state = {
            "protocol_version": 2,
            "project_id": project_id,
            "status": status,
            "generation": generation,
            "latest_command": latest_command,
            "latest_report": latest_report,
            # Keep the real smoke boundary meaningful: a fresh report still
            # requires Supervisor review before this staged publication.
            "last_reviewed_report": max(0, latest_report - 1),
            "active_run": active_run,
            "finalized": False,
            "human_required": False,
        }
        (project / "state.json").write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def _state(self, repository: Path | None = None, project_id: str = "p") -> dict[str, object]:
        checkout = repository or self.worker
        return json.loads(
            (checkout / "projects" / project_id / "state.json").read_text(
                encoding="utf-8"
            )
        )

    def _commit_push(self, repository: Path, relative: str, message: str) -> None:
        self._commit_push_paths(repository, [relative], message)

    def _commit_push_paths(
        self, repository: Path, relatives: list[str], message: str
    ) -> None:
        self._git(repository, "add", "--", *relatives)
        self._git(repository, "commit", "-m", message)
        self._git(repository, "push", "origin", "HEAD:main")

    def _command(
        self,
        *,
        command_id: int = 6,
        based_on_report: int = 5,
        expected_generation: int = 11,
        source: str = "scheduled_chatgpt",
        kind: str = "EXECUTE",
        body: str = "Perform one bounded disposable operation.",
    ) -> bytes:
        metadata = {
            "command_id": command_id,
            "source": source,
            "based_on_report": based_on_report,
            "expected_generation": expected_generation,
            "kind": kind,
        }
        header = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
        return (
            f"<!-- bridge-command: {header} -->\n"
            f"# Command {command_id:03d}\n\n{body.rstrip()}\n"
        ).encode("utf-8")

    def _stage_request(
        self,
        request_id: str,
        *,
        project_id: str = "p",
        command_id: int = 6,
        command: bytes | None = None,
        command_sha256: str | None = None,
        source: str = "scheduled_chatgpt",
        kind: str = "EXECUTE",
        based_on_report: int = 5,
        expected_generation: int = 11,
        extra: dict[str, object] | None = None,
    ) -> None:
        command = command or self._command(
            command_id=command_id,
            based_on_report=based_on_report,
            expected_generation=expected_generation,
            source=source,
            kind=kind,
        )
        request_path = (
            self.worker
            / "worker"
            / "staged-publications"
            / "requests"
            / f"request-{request_id}.json"
        )
        request_path.parent.mkdir(parents=True, exist_ok=True)
        request_path.write_bytes(
            self._request_bytes(
                request_id,
                project_id=project_id,
                command_id=command_id,
                command=command,
                command_sha256=command_sha256,
                source=source,
                kind=kind,
                based_on_report=based_on_report,
                expected_generation=expected_generation,
                extra=extra,
            )
        )
        self._commit_push(
            self.worker,
            str(request_path.relative_to(self.worker)).replace("\\", "/"),
            f"stage disposable request {request_id}",
        )

    def _request_bytes(
        self,
        request_id: str,
        *,
        project_id: str = "p",
        command_id: int = 6,
        command: bytes | None = None,
        command_sha256: str | None = None,
        source: str = "scheduled_chatgpt",
        kind: str = "EXECUTE",
        based_on_report: int = 5,
        expected_generation: int = 11,
        extra: dict[str, object] | None = None,
    ) -> bytes:
        command = command or self._command(
            command_id=command_id,
            based_on_report=based_on_report,
            expected_generation=expected_generation,
            source=source,
            kind=kind,
        )
        envelope: dict[str, object] = {
            "schema_version": 1,
            "request_id": request_id,
            "project_id": project_id,
            "command_id": command_id,
            "source": source,
            "kind": kind,
            "based_on_report": based_on_report,
            "expected_generation": expected_generation,
            "command_sha256": command_sha256
            or hashlib.sha256(command).hexdigest(),
            "command_content": command.decode("utf-8"),
            "created_at": "2001-01-15T00:00:00+00:00",
        }
        if extra:
            envelope.update(extra)
        return (json.dumps(envelope, ensure_ascii=False, indent=2) + "\n").encode(
            "utf-8"
        )

    def _stage_requests(
        self,
        request_ids: list[str],
        *,
        project_id: str = "p",
        command_id: int = 6,
        command: bytes | None = None,
        source: str = "scheduled_chatgpt",
        kind: str = "EXECUTE",
        based_on_report: int = 5,
        expected_generation: int = 11,
    ) -> None:
        relative_paths: list[str] = []
        for request_id in request_ids:
            request_path = (
                self.worker
                / "worker"
                / "staged-publications"
                / "requests"
                / f"request-{request_id}.json"
            )
            request_path.parent.mkdir(parents=True, exist_ok=True)
            request_path.write_bytes(
                self._request_bytes(
                    request_id,
                    project_id=project_id,
                    command_id=command_id,
                    command=command,
                    source=source,
                    kind=kind,
                    based_on_report=based_on_report,
                    expected_generation=expected_generation,
                )
            )
            relative_paths.append(str(request_path.relative_to(self.worker)).replace("\\", "/"))
        self._commit_push_paths(
            self.worker,
            relative_paths,
            f"stage {len(request_ids)} disposable requests",
        )

    def _simulate_claim_and_report_completion(self, command_id: int) -> None:
        state_path = self.worker / "projects" / "p" / "state.json"
        state = self._state()
        claim_generation = int(state["generation"]) + 1
        state.update(
            {
                "status": "CODEX_RUNNING",
                "generation": claim_generation,
                "active_run": {
                    "run_id": f"run-{command_id:03d}-disposable",
                    "command_id": command_id,
                    "claimed_generation": claim_generation,
                },
                "worker_pid": 1234,
            }
        )
        state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        self._commit_push(
            self.worker,
            "projects/p/state.json",
            f"claim disposable command {command_id:03d}",
        )

        state = self._state()
        state.update(
            {
                "status": "REPORT_READY",
                "generation": int(state["generation"]) + 1,
                "latest_report": command_id,
                "active_run": None,
                "worker_pid": None,
            }
        )
        report_path = (
            self.worker / "projects" / "p" / "reports" / f"report-{command_id:03d}.md"
        )
        report_path.write_text(
            f"# Report {command_id:03d} — p\n\n- outcome: SUCCESS\n",
            encoding="utf-8",
        )
        state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        self._commit_push_paths(
            self.worker,
            [
                "projects/p/state.json",
                f"projects/p/reports/report-{command_id:03d}.md",
            ],
            f"complete disposable command {command_id:03d}",
        )

    def _gateway(self) -> SupervisorPublicationGateway:
        return SupervisorPublicationGateway(self.worker)

    def _head(self, repository: Path | None = None) -> str:
        return self._git(repository or self.worker, "rev-parse", "HEAD").stdout.strip()

    def _blob(self, relative: str) -> bytes:
        return git_store.read_blob(self.worker, relative)

    @staticmethod
    def _result(results: tuple[object, ...], request_id: str):
        return next(item for item in results if getattr(item, "request_id", None) == request_id)

    def test_success_publishes_exact_command_and_state_in_one_commit(self) -> None:
        # Exercise the Windows CRLF boundary: the committed canonical blob
        # must remain byte-for-byte identical to the staged request.
        command = self._command().replace(b"\n", b"\r\n")
        self._stage_request("success-1", command=command)

        results = self._gateway().poll()

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].outcome, "published")
        self.assertEqual(results[0].reason, "published")
        state = self._state()
        self.assertEqual(state["status"], "COMMAND_READY")
        self.assertEqual(state["generation"], 11)
        self.assertEqual(state["latest_command"], 6)
        self.assertEqual(state["latest_report"], 5)
        self.assertEqual(state["last_reviewed_report"], 5)
        self.assertIsNone(state["active_run"])
        self.assertEqual(self._blob("projects/p/commands/command-006.md"), command)
        self.assertFalse(
            (
                self.worker
                / "worker"
                / "staged-publications"
                / "requests"
                / "request-success-1.json"
            ).exists()
        )
        self.assertTrue(
            (
                self.worker
                / "worker"
                / "staged-publications"
                / HISTORY_DIRECTORY_NAME
                / "published"
                / "request-success-1.json"
            ).is_file()
        )
        worker_meta = bw.command_metadata(command.decode("utf-8"))
        bw.validate_command(state=state, command_id=6, meta=worker_meta)
        canonical_commit = self._git(
            self.worker,
            "log",
            "-n",
            "1",
            "--format=%H",
            "--",
            "projects/p/commands/command-006.md",
            "projects/p/state.json",
        ).stdout.strip()
        changed = set(
            self._git(
                self.worker,
                "diff-tree",
                "--no-commit-id",
                "--name-only",
                "-r",
                canonical_commit,
            ).stdout.splitlines()
        )
        self.assertEqual(
            changed,
            {"projects/p/commands/command-006.md", "projects/p/state.json"},
        )

    def test_duplicate_request_is_idempotent_without_a_second_commit(self) -> None:
        self._stage_request("duplicate-1")
        first = self._gateway().poll()[0]
        self._stage_request("duplicate-1")

        second = self._gateway().poll()[0]

        self.assertEqual(first.outcome, "published")
        self.assertEqual(second.outcome, "already_applied")
        self.assertEqual(second.reason, "already_applied")
        self.assertEqual(self._state()["generation"], 11)
        self.assertEqual(self._state()["latest_command"], 6)
        self.assertEqual(self._state()["last_reviewed_report"], 5)

    def test_second_staged_command_after_normal_claim_and_report_is_published(self) -> None:
        self._stage_request("sequence-6", command_id=6)
        first = self._gateway().poll()[0]
        self.assertEqual(first.outcome, "published")
        self.assertEqual(self._state()["last_reviewed_report"], 5)

        self._simulate_claim_and_report_completion(6)
        self.assertEqual(self._state()["latest_report"], 6)
        self.assertEqual(self._state()["last_reviewed_report"], 5)

        self._stage_request(
            "sequence-7",
            command_id=7,
            based_on_report=6,
            expected_generation=14,
        )
        result = self._result(self._gateway().poll(), "sequence-7")

        self.assertEqual(result.outcome, "published")
        state = self._state()
        self.assertEqual(state["status"], "COMMAND_READY")
        self.assertEqual(state["generation"], 14)
        self.assertEqual(state["latest_command"], 7)
        self.assertEqual(state["latest_report"], 6)
        self.assertEqual(state["last_reviewed_report"], 6)
        self.assertFalse(
            (
                self.worker
                / "worker"
                / "staged-publications"
                / "requests"
                / "request-sequence-6.json"
            ).exists()
        )

    def test_terminal_request_accumulation_is_compacted_without_starvation(self) -> None:
        stale_ids = [f"old-{index:03d}" for index in range(MAX_SCAN_ENTRIES + 8)]
        self._stage_requests(
            stale_ids,
            command_id=6,
            based_on_report=4,
            expected_generation=11,
        )
        self._stage_request("zz-new-work", command_id=6)

        observed: list[object] = []
        published = None
        for _ in range(8):
            batch = self._gateway().poll()
            self.assertLessEqual(len(batch), MAX_REQUESTS_PER_POLL)
            observed.extend(batch)
            published = next(
                (item for item in batch if getattr(item, "request_id", None) == "zz-new-work"),
                None,
            )
            if published is not None:
                break

        self.assertIsNotNone(published)
        self.assertEqual(getattr(published, "outcome"), "published")
        self.assertGreaterEqual(len(observed), MAX_REQUESTS_PER_POLL)
        self.assertEqual(self._state()["latest_command"], 6)
        self.assertEqual(self._state()["last_reviewed_report"], 5)
        self.assertFalse(
            (
                self.worker
                / "worker"
                / "staged-publications"
                / "requests"
                / "request-zz-new-work.json"
            ).exists()
        )

    def test_interrupted_recovery_precondition_accepts_reviewed_staged_report(self) -> None:
        self._stage_request("recovery-sequence-1", command_id=6)
        self.assertEqual(self._gateway().poll()[0].outcome, "published")
        published = self._state()
        self.assertEqual(published["last_reviewed_report"], 5)

        run_id = "run-006-disposable-recovery"
        pending_path = "worker/runtime/p/pending-report-006.md"
        report_digest = hashlib.sha256(b"pending recovery evidence").hexdigest()
        recovery_state = {
            **published,
            "status": "RECOVERY_REQUIRED",
            "generation": 13,
            "active_run": None,
            "worker_pid": None,
            "human_required": False,
            "finalized": False,
        }
        journal = {
            "project_id": "p",
            "command_id": 6,
            "run_id": run_id,
            "claim_generation": 12,
            "journal_status": "reconciled",
            "remote_publish_pending": False,
            "interruption_kind": "network_guard",
            "interruption_reason_safe": "network guard interruption",
            "pending_report_path": pending_path,
            "report_path": "projects/p/reports/report-006.md",
            "external_side_effects_unknown": True,
        }
        report_identity = {
            "project_id": "p",
            "command_id": 6,
            "run_id": run_id,
            "claim_generation": 12,
            "source": "scheduled_chatgpt",
            "kind": "EXECUTE",
            "based_on_report": 5,
            "outcome": "BLOCKED",
            "interrupted": True,
            "no_final_success_marker": True,
            "external_side_effects_unknown": True,
            "pending_report_sha256": report_digest,
            "interruption_classification": "network_guard",
        }
        decision = protocol_core.validate_recovery_resolution(
            state=recovery_state,
            journal=journal,
            report_identity=report_identity,
            project_id="p",
            command_id=6,
            run_id=run_id,
            claim_generation=12,
            expected_generation=13,
            source="scheduled_chatgpt",
            kind="EXECUTE",
            based_on_report=5,
            pending_report_path=pending_path,
            expected_report_sha256=report_digest,
            actual_report_sha256=report_digest,
            canonical_report_exists=False,
            canonical_report_matches=False,
        )
        self.assertEqual(decision, "ALLOW")

        broken_state = {**recovery_state, "last_reviewed_report": 4}
        with self.assertRaises(protocol_core.ProtocolConflict):
            protocol_core.validate_recovery_resolution(
                state=broken_state,
                journal=journal,
                report_identity=report_identity,
                project_id="p",
                command_id=6,
                run_id=run_id,
                claim_generation=12,
                expected_generation=13,
                source="scheduled_chatgpt",
                kind="EXECUTE",
                based_on_report=5,
                pending_report_path=pending_path,
                expected_report_sha256=report_digest,
                actual_report_sha256=report_digest,
                canonical_report_exists=False,
                canonical_report_matches=False,
            )

    def test_stale_generation_and_wrong_report_are_safe_noops(self) -> None:
        before = self._state()
        self._stage_request(
            "stale-generation",
            command=self._command(expected_generation=12),
            expected_generation=12,
        )
        generation_result = self._result(
            self._gateway().poll(), "stale-generation"
        )
        self.assertEqual(generation_result.outcome, "stale")
        self.assertEqual(generation_result.reason, "wrong_generation")
        self.assertEqual(self._state(), before)

        self._stage_request(
            "stale-report",
            command=self._command(based_on_report=4),
            based_on_report=4,
        )
        report_result = self._result(self._gateway().poll(), "stale-report")
        self.assertEqual(report_result.outcome, "stale")
        self.assertEqual(report_result.reason, "wrong_based_on_report")
        self.assertEqual(self._state(), before)

    def test_malformed_metadata_is_rejected_before_any_canonical_write(self) -> None:
        command = b"# Command 006\n\nmissing protocol metadata\n"
        self._stage_request("bad-metadata", command=command)

        result = self._gateway().poll()[0]

        self.assertEqual(result.outcome, "invalid")
        self.assertEqual(result.reason, "malformed_command_metadata")
        self.assertEqual(self._state()["latest_command"], 5)

    def test_malformed_filenames_archive_by_content_hash_without_collision(self) -> None:
        self._stage_request("bad+filename-one")
        self._stage_request("bad+filename-two")

        results = self._gateway().poll()

        self.assertEqual([item.outcome for item in results], ["invalid", "invalid"])
        request_dir = self.worker / "worker" / "staged-publications" / "requests"
        self.assertEqual(list(request_dir.glob("request-*.json")), [])
        history_dir = (
            self.worker
            / "worker"
            / "staged-publications"
            / HISTORY_DIRECTORY_NAME
            / "invalid"
        )
        self.assertEqual(len(list(history_dir.glob("raw-*.json"))), 2)

    def test_envelope_source_kind_must_match_command_metadata(self) -> None:
        self._stage_request(
            "source-kind-mismatch",
            command=self._command(),
            source="finalizer",
            kind="FINALIZE",
        )

        result = self._gateway().poll()[0]

        self.assertEqual(result.outcome, "invalid")
        self.assertEqual(result.reason, "metadata_source_mismatch")
        self.assertEqual(self._state()["latest_command"], 5)
        self.assertFalse(
            (self.worker / "projects" / "p" / "commands" / "command-006.md").exists()
        )

    def test_bad_phased_parser_is_rejected_after_metadata_preflight(self) -> None:
        body = (
            '<!-- bridge-phased-task: {"schema_version":1,"phases":["A"]} -->\n'
            "# Command 006\n"
        )
        self._stage_request("bad-phased", command=self._command(body=body))

        result = self._gateway().poll()[0]

        self.assertEqual(result.outcome, "invalid")
        self.assertEqual(result.reason, "preflight_failed")
        self.assertEqual(self._state()["status"], "REPORT_READY")
        self.assertFalse(
            (self.worker / "projects" / "p" / "commands" / "command-006.md").exists()
        )

    def test_project_mismatch_and_hash_mismatch_are_rejected(self) -> None:
        state_path = self.worker / "projects" / "p" / "state.json"
        state = self._state()
        state["project_id"] = "other-project"
        state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        self._commit_push(self.worker, "projects/p/state.json", "mismatched disposable state")
        self._stage_request("project-mismatch")
        self._stage_request("hash-mismatch", command_sha256="0" * 64)
        results = self._gateway().poll()
        mismatch = self._result(results, "project-mismatch")
        self.assertEqual(mismatch.outcome, "invalid")
        self.assertEqual(mismatch.reason, "project_mismatch")
        hash_result = self._result(results, "hash-mismatch")
        self.assertEqual(hash_result.outcome, "invalid")
        self.assertEqual(hash_result.reason, "hash_mismatch")
        self.assertEqual(self._state()["latest_command"], 5)

    def test_active_project_is_not_published(self) -> None:
        state_path = self.worker / "projects" / "p" / "state.json"
        state = self._state()
        state.update(
            {
                "status": "CODEX_RUNNING",
                "active_run": {
                    "run_id": "run-005-example",
                    "command_id": 5,
                    "claimed_generation": 11,
                },
            }
        )
        state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        self._commit_push(self.worker, "projects/p/state.json", "active disposable state")
        self._stage_request("active-project")

        result = self._gateway().poll()[0]

        self.assertEqual(result.outcome, "active_project")
        self.assertEqual(result.reason, "active_project")
        self.assertFalse(
            (self.worker / "projects" / "p" / "commands" / "command-006.md").exists()
        )

    def test_human_required_is_not_promoted_or_reviewed(self) -> None:
        state_path = self.worker / "projects" / "p" / "state.json"
        state = self._state()
        state.update({"status": "HUMAN_REQUIRED", "human_required": True})
        state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        self._commit_push(self.worker, "projects/p/state.json", "human-required disposable state")
        self._stage_request("human-required")

        result = self._gateway().poll()[0]

        self.assertEqual(result.outcome, "stale")
        self.assertEqual(result.reason, "not_report_ready")
        final_state = self._state()
        self.assertEqual(final_state["status"], "HUMAN_REQUIRED")
        self.assertEqual(final_state["last_reviewed_report"], 4)
        self.assertEqual(final_state["latest_command"], 5)

    def test_finalizer_pair_enters_finalizing_without_claiming(self) -> None:
        command = self._command(source="finalizer", kind="FINALIZE")
        self._stage_request(
            "finalizer-1",
            command=command,
            source="finalizer",
            kind="FINALIZE",
        )

        result = self._gateway().poll()[0]

        self.assertEqual(result.outcome, "published")
        state = self._state()
        self.assertEqual(state["status"], "FINALIZING")
        self.assertIsNone(state["active_run"])
        self.assertEqual(state["last_reviewed_report"], 5)
        self.assertEqual(self._blob("projects/p/commands/command-006.md"), command)

    def test_cas_race_does_not_overwrite_remote_canonical_state(self) -> None:
        self._stage_request("cas-race-1")
        raced = False
        original_git = git_store.git

        def race_on_first_push(repository: Path, *args: str, **kwargs: object):
            nonlocal raced
            if args and args[0] == "push" and not raced:
                raced = True
                self._git(self.competitor, "fetch", "origin")
                self._git(self.competitor, "reset", "--hard", "origin/main")
                competitor_state_path = self.competitor / "projects" / "p" / "state.json"
                competitor_state = self._state(self.competitor)
                competitor_state["generation"] = 11
                competitor_state_path.write_text(
                    json.dumps(competitor_state, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                self._commit_push(
                    self.competitor,
                    "projects/p/state.json",
                    "competitor wins disposable CAS race",
                )
            return original_git(repository, *args, **kwargs)

        with patch.object(git_store, "git", side_effect=race_on_first_push):
            result = self._gateway().poll()[0]

        self.assertTrue(raced)
        self.assertEqual(result.outcome, "cas_race")
        self.assertEqual(result.reason, "cas_race")
        self.assertEqual(self._state()["generation"], 11)
        self.assertEqual(self._state()["latest_command"], 5)
        self.assertFalse(
            (self.worker / "projects" / "p" / "commands" / "command-006.md").exists()
        )
        self.assertFalse(
            (
                self.worker
                / "worker"
                / "staged-publications"
                / "requests"
                / "request-cas-race-1.json"
            ).is_file()
        )
        self.assertTrue(
            (
                self.worker
                / "worker"
                / "staged-publications"
                / HISTORY_DIRECTORY_NAME
                / "cas_race"
                / "request-cas-race-1.json"
            ).is_file()
        )

    def test_portfolio_isolation_publishes_unrelated_valid_project(self) -> None:
        self._init_project(self.worker, "q")
        self._git(self.worker, "add", "--", "projects/q")
        self._git(self.worker, "commit", "-m", "add second disposable project")
        self._git(self.worker, "push", "origin", "HEAD:main")

        malformed = b"# Command 006\n\nmalformed p command\n"
        self._stage_request("portfolio-a-bad", command=malformed)
        self._stage_request("portfolio-b-good", project_id="q")

        results = self._gateway().poll()

        bad = self._result(results, "portfolio-a-bad")
        good = self._result(results, "portfolio-b-good")
        self.assertEqual(bad.outcome, "invalid")
        self.assertEqual(bad.reason, "malformed_command_metadata")
        self.assertEqual(good.outcome, "published")
        self.assertEqual(self._state(project_id="p")["latest_command"], 5)
        self.assertEqual(self._state(project_id="q")["latest_command"], 6)
        self.assertEqual(self._state(project_id="q")["status"], "COMMAND_READY")
        self.assertEqual(
            self._blob("projects/q/commands/command-006.md"), self._command()
        )

    def test_old_requests_in_one_project_do_not_starve_another_project(self) -> None:
        self._init_project(self.worker, "q")
        self._git(self.worker, "add", "--", "projects/q")
        self._git(self.worker, "commit", "-m", "add second isolation project")
        self._git(self.worker, "push", "origin", "HEAD:main")

        old_ids = [f"old-p-{index:03d}" for index in range(MAX_REQUESTS_PER_POLL + 8)]
        self._stage_requests(
            old_ids,
            project_id="p",
            command_id=6,
            based_on_report=4,
            expected_generation=11,
        )
        self._stage_request("zz-q-work", project_id="q")

        published = None
        for _ in range(4):
            batch = self._gateway().poll()
            published = next(
                (item for item in batch if getattr(item, "request_id", None) == "zz-q-work"),
                None,
            )
            if published is not None:
                break

        self.assertIsNotNone(published)
        self.assertEqual(getattr(published, "outcome"), "published")
        self.assertEqual(self._state(project_id="p")["latest_command"], 5)
        self.assertEqual(self._state(project_id="q")["latest_command"], 6)




# Existing lifecycle cases assume an explicit Owner AUTO decision.
def setUpModule():
    from testing_execution_control import install_auto_fixture
    install_auto_fixture()

if __name__ == "__main__":
    unittest.main()
