"""Хранилище сессий Claude Code: где оно лежит и как читать и писать его файлы."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path

# ~/.claude/projects/<закодированный-путь-проекта>/<session-id>.jsonl
DEFAULT_PROJECTS_DIR = Path.home() / ".claude" / "projects"


def encode_project_dir(path: str) -> str:
    """Так Claude Code называет папку проекта: все символы кроме букв и цифр → '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def iter_jsonl(path: Path, *, warn: bool = False) -> Iterator[dict]:
    """Записи JSONL-файла. Пустые и битые строки пропускаются (с warn=True — с предупреждением)."""
    with path.open(encoding="utf-8", errors="replace") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                if warn:
                    print(f"  ! {path.name}:{n}: битая строка JSON, пропускаю", file=sys.stderr)


def write_jsonl(path: Path, entries: Iterable[dict]) -> None:
    """Атомарная запись: сначала во временный файл, потом переименование."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n")
    tmp.replace(path)
