#!/usr/bin/env python3
"""
Импорт переписки в Claude Code (парный скрипт к export_claude_code.py).

Кладёт сессии в ~/.claude/projects/<закодированный-путь>/<session-id>.jsonl,
после чего они доступны через `claude --resume` / `claude -c` в соответствующем проекте.

Источники (ищутся рекурсивно в указанной папке):
  *.jsonl — экспорт с --format raw (без потерь, рекомендуется);
  *.json  — экспорт с --format json (восстанавливается; блоки размышлений и картинки теряются).
  Если для сессии есть оба файла, берётся .jsonl.

Примеры:
    python import_claude_code.py claude_code_export --dry-run
    python import_claude_code.py claude_code_export
    python import_claude_code.py export --remap /Users/old=/home/new     # перенос на другую машину
    python import_claude_code.py export --project myapp --new-ids        # копии под новыми id
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import shutil
import sys
import uuid
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_TARGET = Path.home() / ".claude" / "projects"
UUID_FIELDS = ("uuid", "parentUuid", "leafUuid", "logicalParentUuid")


# ---------------------------------------------------------------- утилиты


def encode_project_dir(path: str) -> str:
    """Так Claude Code называет папку проекта: все символы кроме букв и цифр → '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def first(entries: Iterable[dict], key: str) -> Any:
    """Первое непустое значение поля среди записей."""
    return next((e[key] for e in entries if e.get(key)), None)


class Remapper:
    """Замена префиксов путей по правилам OLD=NEW (первое совпадение)."""

    def __init__(self, rules: list[str]):
        self.rules: list[tuple[str, str]] = []
        for r in rules:
            if "=" not in r:
                raise SystemExit(f"--remap: ожидается OLD=NEW, получено {r!r}")
            old, new = r.split("=", 1)
            self.rules.append((old.rstrip("/\\") or old, new.rstrip("/\\") or new))

    def __call__(self, path: str | None) -> str | None:
        if not path:
            return path
        for old, new in self.rules:
            if path == old or path.startswith((old + "/", old + "\\")):
                return new + path[len(old) :]
        return path


def read_jsonl(path: Path) -> list[dict]:
    out = []
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            with contextlib.suppress(json.JSONDecodeError):
                out.append(json.loads(line))
    return out


def write_jsonl(path: Path, entries: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n")
    tmp.replace(path)


# ---------------------------------------------------------------- модель импортируемой сессии


@dataclass
class ImportItem:
    src: Path
    kind: str  # raw | json
    session_id: str
    cwd: str | None
    entries: list[dict]
    subagent_of: str | None = None  # id родительской сессии, если это субагент
    agent_name: str | None = None  # agent-<id>
    title: str | None = None


# ---------------------------------------------------------------- raw .jsonl


def detect_subagent(
    entries: list[dict], session_id: str | None, meta: dict | None
) -> tuple[str | None, str | None]:
    """(родительская сессия, имя агента) или (None, None) для обычной сессии."""
    if meta:
        # source_rel из index.json: …/<session>/subagents/<agent>.jsonl
        parts = Path(meta["source_rel"]).parts if meta.get("source_rel") else ()
        if len(parts) >= 4 and parts[-2] == "subagents":
            return parts[-3], Path(parts[-1]).stem
        return None, None
    # Без index.json: в записях субагента есть agentId, а sessionId — родительская сессия
    agent_id = first(entries, "agentId")
    if agent_id and session_id and all(e.get("isSidechain") for e in entries if "isSidechain" in e):
        return session_id, f"agent-{agent_id}"
    return None, None


def load_raw(path: Path, index_by_file: dict[str, dict]) -> ImportItem | None:
    entries = read_jsonl(path)
    if not entries:
        return None
    sid = first(entries, "sessionId")
    title = next((e.get("summary") for e in entries if e.get("type") == "summary"), None)
    subagent_of, agent_name = detect_subagent(
        entries, sid, index_by_file.get(str(path.with_suffix("")))
    )
    return ImportItem(
        path,
        "raw",
        sid or path.stem,
        first(entries, "cwd"),
        entries,
        subagent_of,
        agent_name,
        title,
    )


# ---------------------------------------------------------------- .json → записи Claude Code


def block_to_api(b: dict) -> dict | None:
    """Нормализованный блок экспорта → блок API (или None, если блок не восстанавливается)."""
    t = b.get("type")
    if t == "text":
        return {"type": "text", "text": b["text"]} if b.get("text") else None
    if t == "tool_use":
        return {
            "type": "tool_use",
            "id": b.get("id") or f"toolu_{uuid.uuid4().hex[:24]}",
            "name": b.get("name") or "unknown",
            "input": b.get("input") or {},
        }
    if t == "tool_result":
        r = {"type": "tool_result", "tool_use_id": b.get("id"), "content": b.get("text") or ""}
        if b.get("is_error"):
            r["is_error"] = True
        return r
    if t == "image":
        return {"type": "text", "text": "[изображение не сохранено при экспорте]"}
    if t == "thinking":
        # Без криптографической подписи API отклонит такой блок при продолжении сессии
        return None
    raw = b.get("raw")
    return raw if isinstance(raw, dict) else None


def blocks_to_api(blocks: list[dict]) -> list[dict]:
    return [ab for b in blocks if (ab := block_to_api(b)) is not None]


def build_message(role: str, content: list[dict], model: str | None, msg_uuid: str) -> dict:
    msg: dict[str, Any] = {"role": role, "content": content}
    if role == "assistant":
        msg.update(
            {
                "model": model or "imported",
                "type": "message",
                "id": f"msg_imported_{msg_uuid[:12]}",
                "stop_reason": None,
                "usage": {},
            }
        )
    elif len(content) == 1 and content[0]["type"] == "text":
        msg["content"] = content[0]["text"]
    return msg


def load_json(path: Path) -> ImportItem | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or "messages" not in data:
        return None

    sid = data.get("session_id") or path.stem
    subagent_of = agent_name = None
    if "__" in sid:  # так экспорт помечает субагентов: <session>__<agent>
        subagent_of, agent_name = sid.split("__", 1)
    real_sid = subagent_of or sid
    cwd = data.get("project_path")

    entries: list[dict] = []
    if data.get("title"):
        entries.append({"type": "summary", "summary": data["title"], "leafUuid": None})

    parent = None
    for m in data["messages"]:
        content = blocks_to_api(m.get("content") or [])
        if not content:
            continue
        role = m.get("role") if m.get("role") in ("user", "assistant") else "user"
        u = str(uuid.uuid4())
        e = {
            "parentUuid": parent,
            "isSidechain": bool(m.get("is_sidechain")) or bool(subagent_of),
            "userType": "external",
            "cwd": cwd,
            "sessionId": real_sid,
            "version": "imported",
            "gitBranch": data.get("git_branch") or "",
            "type": role,
            "message": build_message(role, content, m.get("model"), u),
            "uuid": u,
            "timestamp": m.get("timestamp") or now_iso(),
        }
        if agent_name:
            e["agentId"] = agent_name.removeprefix("agent-")
        entries.append(e)
        parent = u

    if entries and entries[0].get("type") == "summary":
        entries[0]["leafUuid"] = parent
    return ImportItem(
        path, "json", real_sid, cwd, entries, subagent_of, agent_name, data.get("title")
    )


# ---------------------------------------------------------------- преобразования


def apply_remap(item: ImportItem, remap: Remapper) -> None:
    if not remap.rules:
        return
    item.cwd = remap(item.cwd)
    for e in item.entries:
        if e.get("cwd"):
            e["cwd"] = remap(e["cwd"])


def apply_new_id(item: ImportItem, id_map: dict[str, str]) -> None:
    """Новый id сессии (общий для сессии и её субагентов) и новые uuid записей."""
    new = id_map.setdefault(item.subagent_of or item.session_id, str(uuid.uuid4()))
    if item.subagent_of:
        item.subagent_of = new
    item.session_id = new
    uuid_map: dict[str, str] = {}
    for e in item.entries:
        if e.get("sessionId"):
            e["sessionId"] = new
        for k in UUID_FIELDS:
            if e.get(k):
                e[k] = uuid_map.setdefault(e[k], str(uuid.uuid4()))


def target_path(item: ImportItem, target_root: Path) -> Path:
    if not item.cwd:
        raise ValueError(f"{item.src}: неизвестен путь проекта (нет cwd)")
    proj = target_root / encode_project_dir(item.cwd)
    if item.subagent_of:
        return proj / item.subagent_of / "subagents" / f"{item.agent_name}.jsonl"
    return proj / f"{item.session_id}.jsonl"


# ---------------------------------------------------------------- поиск источников


def load_index(src: Path) -> dict[str, dict]:
    """index.json экспорта: путь файла без расширения → запись."""
    idx = src / "index.json"
    if not idx.is_file():
        return {}
    by_file: dict[str, dict] = {}
    try:
        for it in json.loads(idx.read_text(encoding="utf-8")):
            by_file[str(src / it["file"])] = it
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        pass
    return by_file


def collect(src: Path) -> list[ImportItem]:
    index_by_file = load_index(src)
    files = [src] if src.is_file() else sorted(src.rglob("*.jsonl")) + sorted(src.rglob("*.json"))
    raw_stems = {f.with_suffix("") for f in files if f.suffix == ".jsonl"}
    items = []
    for f in files:
        if f.name == "index.json":
            continue
        if f.suffix == ".jsonl":
            it = load_raw(f, index_by_file)
        elif f.with_suffix("") in raw_stems:
            continue  # есть lossless-версия
        else:
            it = load_json(f)
        if it:
            items.append(it)
    return items


# ---------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Импорт переписки в Claude Code")
    ap.add_argument("source", type=Path, help="папка экспорта (или отдельный .jsonl/.json файл)")
    ap.add_argument(
        "-t",
        "--target",
        type=Path,
        default=DEFAULT_TARGET,
        help=f"куда импортировать (по умолчанию {DEFAULT_TARGET})",
    )
    ap.add_argument(
        "--remap",
        action="append",
        default=[],
        metavar="OLD=NEW",
        help="заменить префикс пути проекта, напр. /Users/me=/home/me (можно несколько)",
    )
    ap.add_argument(
        "--project",
        action="append",
        default=[],
        help="импортировать только проекты, путь которых содержит подстроку",
    )
    ap.add_argument(
        "--new-ids",
        action="store_true",
        help="выдать сессиям новые id (импорт копий рядом с существующими)",
    )
    ap.add_argument("--overwrite", action="store_true", help="перезаписывать существующие сессии")
    ap.add_argument("--dry-run", action="store_true", help="только показать, что будет сделано")
    return ap


def write_item(item: ImportItem, dst: Path, rebuild: bool) -> None:
    if item.kind == "raw" and not rebuild:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item.src, dst)  # байт в байт
    else:
        write_jsonl(dst, item.entries)


def main() -> int:
    opts = build_parser().parse_args()

    src = opts.source.expanduser()
    if not src.exists():
        print(f"Не найден источник {src}", file=sys.stderr)
        return 1
    target_root = opts.target.expanduser()
    remap = Remapper(opts.remap)
    rebuild = bool(remap.rules) or opts.new_ids  # raw-файл нельзя просто скопировать

    items = collect(src)
    if not items:
        print("Не найдено ни одной сессии (.jsonl или .json из экспорта).", file=sys.stderr)
        return 1

    id_map: dict[str, str] = {}
    stats: Counter[str] = Counter()
    # Сначала основные сессии, потом субагенты — чтобы id_map уже знал новые id родителей
    for it in sorted(items, key=lambda i: (i.subagent_of is not None, str(i.src))):
        apply_remap(it, remap)
        if not it.cwd:
            print(
                f"  ! {it.src.name}: неизвестен путь проекта (нет cwd), пропускаю", file=sys.stderr
            )
            stats["nocwd"] += 1
            continue
        if opts.project and not any(p.lower() in it.cwd.lower() for p in opts.project):
            continue
        if opts.new_ids:
            apply_new_id(it, id_map)

        dst = target_path(it, target_root)
        label = f"{it.cwd}  ←  {it.src.name}" + (f"  [{it.title}]" if it.title else "")
        if dst.exists() and not opts.overwrite:
            print(f"  = уже есть, пропуск: {label}")
            stats["skip"] += 1
            continue

        print(f"  {'(dry) ' if opts.dry_run else ''}+ {label}\n      → {dst}")
        if not opts.dry_run:
            write_item(it, dst, rebuild)
        stats["ok"] += 1

    verb = "Будет импортировано" if opts.dry_run else "Импортировано"
    print(
        f"\n✅ {verb}: {stats['ok']}, пропущено (уже есть): {stats['skip']}, "
        f"без пути проекта: {stats['nocwd']}"
    )
    if stats["ok"] and not opts.dry_run:
        print("Откройте проект и запустите `claude --resume`, чтобы выбрать сессию.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
