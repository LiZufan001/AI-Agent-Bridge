"""Bounded, content-free run observations. Never serve raw CLI or prompt text."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from owner_console_control import run_directory

EVENTS = {
    "process_launch": "启动执行进程", "process_started": "执行进程已启动",
    "process_exit": "执行进程已退出", "final_marker_detected": "已捕获最终结果",
    "graceful_cleanup_start": "正在收尾", "forced_cleanup_start": "正在清理进程树",
    "report_finalize_start": "正在整理并发布报告", "owner_stop_accepted": "Worker 已接受停止请求",
    "log_threshold_exceeded": "输出量达到提示阈值，任务继续运行",
    "run_started": "已认领任务，准备执行", "run_completed": "结果已完成发布",
}


def process_alive(root: Path, project: str, run_id: str) -> bool | None:
    """Read-only OS evidence; birth time guards against recycled PIDs."""
    try:
        path = run_directory(root, project, run_id) / "codex-root.pid"
        with path.open("rb") as stream:
            raw = stream.read(32)
        if not raw.strip().isdigit() or os.name != "nt":
            return None
        from operator_maintenance import _windows_process_identity, _windows_process_is_active
        pid = int(raw)
        identity = _windows_process_identity(pid)
        if identity is None:
            return False
        born = datetime.fromisoformat(identity[0]).timestamp()
        if abs(path.stat().st_mtime - born) > 15:
            return False
        return _windows_process_is_active(pid, identity[1])
    except (OSError, ValueError):
        return None


def host_process_alive(pid: object) -> bool | None:
    if type(pid) is not int or pid <= 0:
        return None
    try:
        from operator_maintenance import _parent_identity_alive
        return _parent_identity_alive(pid, None)
    except OSError:
        return None


def record_event(directory: Path, event: str, fields: dict[str, Any]) -> None:
    if event not in EVENTS:
        return
    path = directory / "console-events.jsonl"
    try:
        if path.exists() and path.stat().st_size > 64 * 1024:
            return
        record = {"event": event, "at": datetime.now(timezone.utc).isoformat()}
        if type(fields.get("exit_code")) is int:
            record["exit_code"] = fields["exit_code"]
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record) + "\n")
    except OSError:
        pass  # Observation loss cannot change run outcome or recovery evidence.


def tail(path: Path, limit: int = 64 * 1024) -> tuple[str, dict[str, Any]]:
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size - limit))
            raw = stream.read(limit)
        if size > limit:
            raw = raw.partition(b"\n")[2]
        age = max(0, int(datetime.now(timezone.utc).timestamp() - path.stat().st_mtime))
        return raw.decode("utf-8", errors="replace"), {"available": True, "bytes": size, "age_seconds": age, "tail_limited": size > limit}
    except OSError:
        return "", {"available": False, "bytes": 0, "age_seconds": None}


def report_metadata(root: Path, project: str, number: int | None) -> dict[str, Any]:
    if type(number) is not int or not 0 < number < 1_000_000:
        return {}
    path = root / "projects" / project / "reports" / f"report-{number:03d}.md"
    try:
        with path.open("rb") as stream:
            text = stream.read(12 * 1024).decode("utf-8", errors="replace")
    except OSError:
        return {"available": False, "report_id": number}
    result: dict[str, Any] = {"available": True, "report_id": number}
    patterns = {
        "outcome": r"SUCCESS|FAILED|BLOCKED|NETWORK_INTERRUPTED",
        "run_id": r"run-\d{1,6}-[a-f0-9]{1,32}",
        "effective_cli_model": r"gpt-[A-Za-z0-9.-]{1,64}",
        "effective_cli_reasoning_effort": r"none|minimal|low|medium|high|xhigh|max|ultra",
        "completed_at": r"[0-9T:+.Z-]{10,40}",
        "termination_reason": r"owner_stop|execution_timeout|network_guard|final_grace_expired",
    }
    # Header metadata only. Report/diagnostic/final-response bodies are never read into the API.
    text = text.split("\n##", 1)[0]
    for key, pattern in patterns.items():
        match = re.search(r"(?m)^- " + key + r": `?(" + pattern + r")`?\s*$", text)
        if match:
            result[key] = match[1]
    return result


def command_summary(root: Path, project: str, number: int | None) -> dict[str, Any]:
    """Only a screened heading is display metadata; never return command bodies."""
    if type(number) is not int or not 0 < number < 1_000_000:
        return {}
    try:
        with (root / 'projects' / project / 'commands' / f'command-{number:03d}.md').open('rb') as stream:
            text = stream.read(8192).decode('utf-8', errors='replace')
    except OSError:
        return {}
    for line in text.splitlines():
        if line.startswith('# '):
            title = re.sub(r'^#\s*(?:Command|指令)\s*\d+\s*[—–:-]?\s*', '', line)
            sensitive = r'(?i)token|secret|password|authorization|cookie|openid|appsecret|bearer|api[_-]?key|https?://|[A-Z]:[\\/]|[\w.+-]+@[\w.-]+|[A-Za-z0-9_+/=-]{36,}'
            if re.search(sensitive, title) or len(title) > 160:
                return {'title': '任务标题已隐藏（可能包含敏感信息）'}
            return {'title': title.strip('# ').strip()[:120]}
    return {}


def run_view(root: Path, project: str, run_id: str) -> dict[str, Any]:
    directory = run_directory(root, project, run_id)
    if not directory.is_dir():
        return {"available": False, "events": [], "activity": "尚无本地运行证据"}
    events = []
    event_text, _ = tail(directory / "console-events.jsonl")
    for line in event_text.splitlines()[-64:]:
        try:
            value = json.loads(line)
            if value.get("event") in EVENTS and re.fullmatch(r"[0-9T:+.Z-]{10,40}", str(value.get("at", ""))):
                item = {"label": EVENTS[value["event"]], "at": value["at"]}
                if type(value.get("exit_code")) is int:
                    item["exit_code"] = value["exit_code"]
                events.append(item)
        except (ValueError, AttributeError, TypeError):
            continue
    stderr, stderr_info = tail(directory / "stderr.log")
    _, stdout_info = tail(directory / "stdout.log")
    activity = "等待新的执行输出"
    steps = []
    # Legacy CLI is plain text. Classify only exact structural lines; discard
    # commands, arguments, tool output, assistant prose and the echoed prompt.
    next_command = False
    for line in stderr.splitlines():
        if next_command:
            categories = [(r'(?i)\bgit\s+(status|diff|log|show)\b', '正在检查代码变更'),
                          (r'(?i)unittest|pytest|npm\s+(run\s+)?test', '正在运行测试'),
                          (r'(?i)\b(build|compileall|tsc)\b', '正在构建或检查代码'),
                          (r'(?i)apply_patch|Set-Content|write_text', '正在修改项目文件'),
                          (r'(?i)\brg\b|Get-Content|\bcat\b|read_text', '正在检索与阅读文件'),
                          (r'(?i)Start-Sleep|time\.sleep|\bsleep\s+\d', '正在等待工具任务完成')]
            for pattern, description in categories:
                if re.search(pattern, line):
                    activity = description
                    steps.append(description)
                    break
            next_command = False
        completed = re.search(r'\b(succeeded|exited \d+) in (\d+(?:\.\d+)?(?:ms|s)):', line)
        if completed:
            activity = ('工具执行完成' if completed[1] == 'succeeded' else '工具已退出') + ' · ' + completed[2]
            steps.append(activity)
        label = {"thinking": "正在分析任务", "exec": "正在执行工具命令", "codex": "正在整理回复", "tokens used": "本轮模型输出已结束"}.get(line.strip())
        if label:
            activity = label
            steps.append(label)
            next_command = line.strip() == 'exec'
    progress = {}
    try:
        from owner_console_control import read_object
        value = read_object(directory / "progress.json")
        for key in ("current_phase", "completed_phases"):
            v = value.get(key)
            if type(v) is int:
                progress[key] = v
        if type(value.get("final_acceptance_verified")) is bool:
            progress["final_acceptance_verified"] = value["final_acceptance_verified"]
    except (OSError, ValueError):
        pass
    return {"available": True, "run_id": run_id, "events": events[-24:],
            "activity": activity, "recent_steps": steps[-12:], "progress": progress,
            "stdout": stdout_info, "stderr": stderr_info,
            "stop_requested": (directory / "owner-stop.json").is_file(),
            "content_policy": "仅显示执行事件、阶段与输出活动；原始提示词、命令参数和日志正文不进入浏览器。"}
