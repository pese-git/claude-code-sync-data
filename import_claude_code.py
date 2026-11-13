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
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_TARGET = Path.home() / ".claude" / "projects"


# ---------------------------------------------------------------- утилиты


def encode_project_dir(path: str) -> str:
    """Так Claude Code называет папку проекта: все символы кроме букв и цифр → '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


class Remapper:
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
            if path == old or path.startswith(old + "/") or path.startswith(old + "\\"):
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


class ImportItem:
    def __init__(
        self,
        src: Path,
        kind: str,
        session_id: str,
        cwd: str | None,
        entries: list[dict],
        subagent_of: str | None = None,
        agent_name: str | None = None,
        title: str | None = None,
    ):
        self.src, self.kind = src, kind
        self.session_id, self.cwd, self.entries = session_id, cwd, entries
        self.subagent_of, self.agent_name, self.title = subagent_of, agent_name, title


# ---------------------------------------------------------------- raw .jsonl


def load_raw(path: Path, index_by_file: dict[str, dict]) -> ImportItem | None:
    entries = read_jsonl(path)
    if not entries:
        return None
    sid = next((e["sessionId"] for e in entries if e.get("sessionId")), None)
    cwd = next((e["cwd"] for e in entries if e.get("cwd")), None)
    title = next((e.get("summary") for e in entries if e.get("type") == "summary"), None)

    # Субагент? Определяем по source_rel из index.json (…/<session>/subagents/<agent>.jsonl)
    meta = index_by_file.get(str(path.with_suffix("")))
    subagent_of = agent_name = None
    if meta and meta.get("source_rel"):
        parts = Path(meta["source_rel"]).parts
        if len(parts) >= 4 and parts[-2] == "subagents":
            subagent_of, agent_name = parts[-3], Path(parts[-1]).stem
    elif not meta:
        # Без index.json: в записях субагента есть agentId, а sessionId — родительская сессия
        agent_id = next((e["agentId"] for e in entries if e.get("agentId")), None)
        if agent_id and sid and all(e.get("isSidechain") for e in entries if "isSidechain" in e):
            subagent_of, agent_name = sid, f"agent-{agent_id}"
    if not sid:
        sid = path.stem
    return ImportItem(path, "raw", sid, cwd, entries, subagent_of, agent_name, title)


# ---------------------------------------------------------------- .json → записи Claude Code


def blocks_to_api(blocks: list[dict]) -> list[dict]:
    out = []
    for b in blocks:
        t = b.get("type")
        if t == "text":
            if b.get("text"):
                out.append({"type": "text", "text": b["text"]})
        elif t == "tool_use":
            out.append(
                {
                    "type": "tool_use",
                    "id": b.get("id") or f"toolu_{uuid.uuid4().hex[:24]}",
                    "name": b.get("name") or "unknown",
                    "input": b.get("input") or {},
                }
            )
        elif t == "tool_result":
            r = {"type": "tool_result", "tool_use_id": b.get("id"), "content": b.get("text") or ""}
            if b.get("is_error"):
                r["is_error"] = True
            out.append(r)
        elif t == "image":
            out.append({"type": "text", "text": "[изображение не сохранено при экспорте]"})
        elif t == "thinking":
            # Без криптографической подписи API отклонит такой блок при продолжении сессии
            continue
        elif "raw" in b and isinstance(b["raw"], dict):
            out.append(b["raw"])
    return out


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

    cwd = data.get("project_path")
    branch = data.get("git_branch")
    entries: list[dict] = []
    if data.get("title"):
        entries.append({"type": "summary", "summary": data["title"], "leafUuid": None})

    parent = None
    real_sid = subagent_of or sid
    for m in data["messages"]:
        content = blocks_to_api(m.get("content") or [])
        if not content:
            continue
        role = m.get("role") if m.get("role") in ("user", "assistant") else "user"
        u = str(uuid.uuid4())
        msg: dict[str, Any] = {"role": role, "content": content}
        if role == "assistant":
            msg.update(
                {
                    "model": m.get("model") or "imported",
                    "type": "message",
                    "id": f"msg_imported_{u[:12]}",
                    "stop_reason": None,
                    "usage": {},
                }
            )
        elif len(content) == 1 and content[0]["type"] == "text":
            msg["content"] = content[0]["text"]
        e = {
            "parentUuid": parent,
            "isSidechain": bool(m.get("is_sidechain")) or bool(subagent_of),
            "userType": "external",
            "cwd": cwd,
            "sessionId": real_sid,
            "version": "imported",
            "gitBranch": branch or "",
            "type": role,
            "message": msg,
            "uuid": u,
            "timestamp": m.get("timestamp")
            or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
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
    old = item.subagent_of or item.session_id
    new = id_map.setdefault(old, str(uuid.uuid4()))
    if item.subagent_of:
        item.subagent_of = new
    item.session_id = new
    uuid_map: dict[str, str] = {}
    for e in item.entries:
        if e.get("sessionId"):
            e["sessionId"] = new
        for k in ("uuid", "parentUuid", "leafUuid", "logicalParentUuid"):
            if e.get(k):
                e[k] = uuid_map.setdefault(e[k], str(uuid.uuid4()))


def target_path(item: ImportItem, target_root: Path) -> Path:
    if not item.cwd:
        raise ValueError(f"{item.src}: неизвестен путь проекта (нет cwd)")
    proj = target_root / encode_project_dir(item.cwd)
    if item.subagent_of:
        return proj / item.subagent_of / "subagents" / f"{item.agent_name}.jsonl"
    return proj / f"{item.session_id}.jsonl"


# ---------------------------------------------------------------- main


def collect(src: Path) -> list[ImportItem]:
    index_by_file: dict[str, dict] = {}
    idx = src / "index.json"
    if idx.is_file():
        try:
            for it in json.loads(idx.read_text(encoding="utf-8")):
                index_by_file[str(src / it["file"])] = it
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            pass

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


def main() -> int:
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
    opts = ap.parse_args()

    src = opts.source.expanduser()
    if not src.exists():
        print(f"Не найден источник {src}", file=sys.stderr)
        return 1
    target_root = opts.target.expanduser()
    remap = Remapper(opts.remap)

    items = collect(src)
    if not items:
        print("Не найдено ни одной сессии (.jsonl или .json из экспорта).", file=sys.stderr)
        return 1

    id_map: dict[str, str] = {}
    stats = {"ok": 0, "skip": 0, "nocwd": 0}
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
            if it.kind == "raw" and not remap.rules and not opts.new_ids:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(it.src, dst)  # байт в байт
            else:
                write_jsonl(dst, it.entries)
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
