"""Общие фикстуры: синтетическая история Claude Code и запуск CLI в процессе."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

import pytest

from claude_code_sync import cli

PROJECT_PATH = "/home/user/my app"
PROJECT_DIR = "-home-user-my-app"  # так Claude Code кодирует PROJECT_PATH
SESSION_ID = "11111111-aaaa-bbbb-cccc-000000000001"
OTHER_SESSION_ID = "22222222-aaaa-bbbb-cccc-000000000002"
AGENT_ID = "a1b2c3"


def write_jsonl(path: Path, entries: Iterable[dict], extra_lines: Iterable[str] = ()) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
        for line in extra_lines:
            f.write(line + "\n")
    return path


def entry(etype: str, uid: str, content, ts: str, **extra) -> dict:
    """Запись user/assistant в формате Claude Code."""
    message = {"role": etype, "content": content}
    if etype == "assistant":
        message["model"] = "claude-test"
    return {
        "type": etype,
        "uuid": uid,
        "parentUuid": None,
        "sessionId": SESSION_ID,
        "cwd": PROJECT_PATH,
        "gitBranch": "main",
        "timestamp": ts,
        "message": message,
        **extra,
    }


def main_session_entries() -> list[dict]:
    return [
        {"type": "summary", "summary": "Починка логина", "leafUuid": "a2"},
        entry(
            "user",
            "u1",
            "<system-reminder>скрыть</system-reminder>Почини логин",
            "2026-11-02T10:00:00Z",
        ),
        entry("user", "u1", "дубль той же записи", "2026-11-02T10:00:00Z"),
        entry("user", "u2", "<command-name>/clear</command-name>", "2026-11-02T10:00:01Z"),
        entry(
            "assistant",
            "a1",
            [{"type": "thinking", "thinking": "надо посмотреть код"}],
            "2026-11-02T10:00:02Z",
        ),
        entry(
            "assistant",
            "a2",
            [
                {"type": "text", "text": "Смотрю файл"},
                {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"path": "a.py"}},
            ],
            "2026-11-02T10:00:03Z",
        ),
        entry(
            "user",
            "u3",
            [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "print('hi')"}],
            "2026-11-02T10:00:04Z",
        ),
        entry("assistant", "a3", [{"type": "text", "text": "Готово"}], "2026-11-02T10:00:05Z"),
        {"type": "system", "uuid": "s1", "content": "служебная запись"},
    ]


def subagent_entries() -> list[dict]:
    return [
        entry(
            "user",
            "su1",
            "задача субагенту",
            "2026-11-02T10:01:00Z",
            isSidechain=True,
            agentId=AGENT_ID,
        ),
        entry(
            "assistant",
            "sa1",
            [{"type": "text", "text": "сделано"}],
            "2026-11-02T10:01:01Z",
            isSidechain=True,
            agentId=AGENT_ID,
        ),
    ]


@dataclass
class History:
    """Синтетическая папка ~/.claude/projects."""

    root: Path
    main_session: Path
    subagent: Path
    old_session: Path


@pytest.fixture
def history(tmp_path: Path) -> History:
    root = tmp_path / "projects"
    proj = root / PROJECT_DIR
    main = write_jsonl(
        proj / f"{SESSION_ID}.jsonl", main_session_entries(), extra_lines=["{битая строка"]
    )
    sub = write_jsonl(
        proj / SESSION_ID / "subagents" / f"agent-{AGENT_ID}.jsonl", subagent_entries()
    )
    old = write_jsonl(
        proj / f"{OTHER_SESSION_ID}.jsonl",
        [
            {
                **entry("user", "o1", "старый вопрос", "2025-01-15T09:00:00Z"),
                "sessionId": OTHER_SESSION_ID,
            }
        ],
    )
    # Пустая сессия (без сообщений) не должна попадать в экспорт
    write_jsonl(proj / "empty.jsonl", [{"type": "summary", "summary": "пусто"}])
    # Второй проект — для фильтра --project
    write_jsonl(
        root / "-srv-other" / "33333333.jsonl",
        [
            {
                **entry("user", "x1", "другой проект", "2026-11-03T08:00:00Z"),
                "cwd": "/srv/other",
                "sessionId": "33333333",
            }
        ],
    )
    return History(root, main, sub, old)


Runner = Callable[..., tuple[int, str, str]]


def run_cli(capsys: pytest.CaptureFixture[str], *args: str | Path) -> tuple[int, str, str]:
    """claude-code-sync <args> в том же процессе → (код возврата, stdout, stderr)."""
    try:
        code = cli.main([str(a) for a in args])
    except SystemExit as e:  # argparse и Remapper завершают через SystemExit
        code = e.code if isinstance(e.code, int) else 1
        if isinstance(e.code, str):
            print(e.code, file=sys.stderr)
    out, err = capsys.readouterr()
    return code, out, err


@pytest.fixture
def run_export(capsys: pytest.CaptureFixture[str]) -> Runner:
    return lambda *args: run_cli(capsys, "export", *args)


@pytest.fixture
def run_import(capsys: pytest.CaptureFixture[str]) -> Runner:
    return lambda *args: run_cli(capsys, "import", *args)
