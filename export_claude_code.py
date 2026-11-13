#!/usr/bin/env python3
"""
Экспорт переписки Claude Code для всех проектов.

Claude Code хранит сессии в ~/.claude/projects/<закодированный-путь>/<session-id>.jsonl
Скрипт проходит по всем проектам и сохраняет каждую сессию в Markdown и/или JSON.

Примеры:
    python export_claude_code.py
    python export_claude_code.py -o ~/claude-export --format md json
    python export_claude_code.py --project myapp --since 2026-10-01
    python export_claude_code.py --tools --thinking          # с вызовами инструментов и размышлениями
    python export_claude_code.py --single-file               # всё в один all_conversations.md
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

DEFAULT_SOURCE = Path.home() / ".claude" / "projects"
TOOL_RESULT_LIMIT = 4000  # символов результата инструмента в Markdown


# ---------------------------------------------------------------- модель данных

@dataclass
class Message:
    role: str                 # user | assistant
    timestamp: str | None
    blocks: list[dict]        # нормализованные блоки: {"type": ..., ...}
    is_sidechain: bool = False
    model: str | None = None


@dataclass
class Session:
    session_id: str
    project_dir: str          # имя папки в ~/.claude/projects
    project_path: str | None  # реальный путь проекта (cwd)
    file: Path
    title: str | None = None
    git_branch: str | None = None
    messages: list[Message] = field(default_factory=list)

    @property
    def started(self) -> str | None:
        return next((m.timestamp for m in self.messages if m.timestamp), None)

    @property
    def ended(self) -> str | None:
        return next((m.timestamp for m in reversed(self.messages) if m.timestamp), None)


# ---------------------------------------------------------------- парсинг

def read_jsonl(path: Path) -> Iterable[dict]:
    with path.open(encoding="utf-8", errors="replace") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                print(f"  ! {path.name}:{n}: битая строка JSON, пропускаю", file=sys.stderr)


def normalize_content(content: Any) -> list[dict]:
    """Приводит message.content (строка или список блоков) к списку блоков."""
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    blocks = []
    for b in content if isinstance(content, list) else [content]:
        if isinstance(b, str):
            blocks.append({"type": "text", "text": b})
            continue
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            blocks.append({"type": "text", "text": b.get("text", "")})
        elif t == "thinking":
            blocks.append({"type": "thinking", "text": b.get("thinking", "")})
        elif t == "redacted_thinking":
            blocks.append({"type": "thinking", "text": "[скрыто]"})
        elif t == "tool_use":
            blocks.append({"type": "tool_use", "id": b.get("id"),
                           "name": b.get("name"), "input": b.get("input", {})})
        elif t == "tool_result":
            blocks.append({"type": "tool_result", "id": b.get("tool_use_id"),
                           "is_error": bool(b.get("is_error")),
                           "text": flatten_tool_result(b.get("content"))})
        elif t == "image":
            blocks.append({"type": "image"})
        else:
            blocks.append({"type": t or "unknown", "raw": b})
    return blocks


def flatten_tool_result(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for c in content if isinstance(content, list) else [content]:
        if isinstance(c, dict):
            if c.get("type") == "text":
                parts.append(c.get("text", ""))
            elif c.get("type") == "image":
                parts.append("[изображение]")
            else:
                parts.append(json.dumps(c, ensure_ascii=False))
        else:
            parts.append(str(c))
    return "\n".join(parts)


def parse_session(path: Path, project_dir: str) -> Session:
    s = Session(session_id=path.stem, project_dir=project_dir, project_path=None, file=path)
    seen_uuids: set[str] = set()

    for e in read_jsonl(path):
        etype = e.get("type")

        if etype == "summary" and e.get("summary") and not s.title:
            s.title = e["summary"]
            continue
        if etype in ("custom-title", "ai-title") and (e.get("customTitle") or e.get("title")):
            s.title = e.get("customTitle") or e.get("title")
            continue
        if etype not in ("user", "assistant"):
            continue

        # дубли (Claude Code иногда пишет одну запись несколько раз)
        uid = e.get("uuid")
        if uid:
            if uid in seen_uuids:
                continue
            seen_uuids.add(uid)

        s.project_path = s.project_path or e.get("cwd")
        s.git_branch = s.git_branch or e.get("gitBranch")
        if e.get("sessionId"):
            s.session_id = e["sessionId"]

        msg = e.get("message") or {}
        blocks = normalize_content(msg.get("content"))
        if not blocks:
            continue

        s.messages.append(Message(
            role=msg.get("role") or etype,
            timestamp=e.get("timestamp"),
            blocks=blocks,
            is_sidechain=bool(e.get("isSidechain")),
            model=msg.get("model"),
        ))

    # Сообщения ассистента приходят кусками (по блоку на запись) — склеиваем подряд идущие
    merged: list[Message] = []
    for m in s.messages:
        if merged and m.role == "assistant" and merged[-1].role == "assistant" \
                and merged[-1].is_sidechain == m.is_sidechain:
            merged[-1].blocks.extend(m.blocks)
        else:
            merged.append(m)
    s.messages = merged
    return s


def iter_session_files(project: Path) -> Iterable[Path]:
    """Основные сессии + сессии субагентов (<session>/subagents/*.jsonl)."""
    yield from sorted(project.glob("*.jsonl"))
    yield from sorted(project.glob("*/subagents/*.jsonl"))


# ---------------------------------------------------------------- фильтр шума

SYSTEM_NOISE = re.compile(
    r"^\s*(<command-name>|<local-command-stdout>|<command-message>|<local-command-caveat>|"
    r"Caveat: The messages below|\[Request interrupted)", re.S)


def is_noise(text: str) -> bool:
    return bool(SYSTEM_NOISE.match(text))


def strip_system_reminders(text: str) -> str:
    return re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.S).strip()


# ---------------------------------------------------------------- Markdown

def fmt_ts(ts: str | None) -> str:
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ts


def fence(text: str, lang: str = "") -> str:
    ticks = "```"
    while ticks in text:
        ticks += "`"
    return f"{ticks}{lang}\n{text}\n{ticks}"


def truncate(text: str, limit: int) -> str:
    if limit and len(text) > limit:
        return text[:limit] + f"\n… [обрезано, всего {len(text)} символов]"
    return text


def session_to_markdown(s: Session, opts: argparse.Namespace, heading_level: int = 1) -> str:
    h = "#" * heading_level
    out = [f"{h} {s.title or 'Сессия ' + s.session_id}", ""]
    out += [
        f"- **Проект:** `{s.project_path or s.project_dir}`",
        f"- **Сессия:** `{s.session_id}`",
        f"- **Начало:** {fmt_ts(s.started)}",
        f"- **Конец:** {fmt_ts(s.ended)}",
    ]
    if s.git_branch:
        out.append(f"- **Ветка:** `{s.git_branch}`")
    out += ["", "---", ""]

    for m in s.messages:
        if m.is_sidechain and not opts.sidechain:
            continue
        parts: list[str] = []
        for b in m.blocks:
            t = b["type"]
            if t == "text":
                text = strip_system_reminders(b["text"])
                if not text or (not opts.raw and is_noise(text)):
                    continue
                parts.append(text)
            elif t == "thinking" and opts.thinking and b["text"].strip():
                parts.append("<details><summary>💭 Размышления</summary>\n\n"
                             + b["text"].strip() + "\n\n</details>")
            elif t == "tool_use" and opts.tools:
                parts.append(f"🔧 **{b['name']}**\n\n"
                             + fence(json.dumps(b["input"], ensure_ascii=False, indent=2), "json"))
            elif t == "tool_result" and opts.tools:
                label = "❌ Ошибка инструмента" if b["is_error"] else "📤 Результат"
                parts.append(f"<details><summary>{label}</summary>\n\n"
                             + fence(truncate(b["text"], opts.max_tool_output))
                             + "\n\n</details>")
            elif t == "image":
                parts.append("_[изображение]_")
        if not parts:
            continue
        only_results = all(b["type"] == "tool_result" for b in m.blocks)
        who = "⚙️ Инструмент" if only_results else ("👤 Пользователь" if m.role == "user" else "🤖 Claude")
        if m.is_sidechain:
            who += " (субагент)"
        out.append(f"{h}# {who}  ·  {fmt_ts(m.timestamp)}")
        out.append("")
        out.append("\n\n".join(parts))
        out.append("")
    return "\n".join(out)


def session_to_dict(s: Session) -> dict:
    return {
        "session_id": s.session_id,
        "title": s.title,
        "project_dir": s.project_dir,
        "project_path": s.project_path,
        "git_branch": s.git_branch,
        "started": s.started,
        "ended": s.ended,
        "source_file": str(s.file),
        "messages": [
            {"role": m.role, "timestamp": m.timestamp, "model": m.model,
             "is_sidechain": m.is_sidechain, "content": m.blocks}
            for m in s.messages
        ],
    }


# ---------------------------------------------------------------- main

def safe_name(name: str, maxlen: int = 80) -> str:
    name = re.sub(r"[^\w\-.]+", "_", name, flags=re.U).strip("_.")
    return name[:maxlen] or "untitled"


def parse_date(s: str) -> datetime:
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def main() -> int:
    ap = argparse.ArgumentParser(description="Экспорт переписки Claude Code по всем проектам")
    ap.add_argument("-s", "--source", type=Path, default=DEFAULT_SOURCE,
                    help=f"папка с проектами (по умолчанию {DEFAULT_SOURCE})")
    ap.add_argument("-o", "--output", type=Path, default=Path("claude_code_export"),
                    help="куда сохранить (по умолчанию ./claude_code_export)")
    ap.add_argument("--format", nargs="+", choices=["md", "json"], default=["md"],
                    help="форматы вывода: md, json (можно оба)")
    ap.add_argument("--project", action="append", default=[],
                    help="экспортировать только проекты, путь которых содержит подстроку (можно несколько)")
    ap.add_argument("--since", type=parse_date, help="только сессии, начатые после даты (YYYY-MM-DD)")
    ap.add_argument("--until", type=parse_date, help="только сессии, начатые до даты (YYYY-MM-DD)")
    ap.add_argument("--tools", action="store_true", help="включить вызовы инструментов и их результаты")
    ap.add_argument("--thinking", action="store_true", help="включить блоки размышлений")
    ap.add_argument("--sidechain", action="store_true", help="включить сообщения субагентов")
    ap.add_argument("--raw", action="store_true", help="не фильтровать служебные сообщения (/команды и т.п.)")
    ap.add_argument("--max-tool-output", type=int, default=TOOL_RESULT_LIMIT,
                    help="макс. длина результата инструмента в md (0 = без ограничения)")
    ap.add_argument("--single-file", action="store_true",
                    help="дополнительно собрать всё в один all_conversations.md")
    opts = ap.parse_args()

    src: Path = opts.source.expanduser()
    if not src.is_dir():
        print(f"Не найдена папка {src}. Укажите путь через --source.", file=sys.stderr)
        return 1

    out_root: Path = opts.output.expanduser()
    out_root.mkdir(parents=True, exist_ok=True)

    index: list[dict] = []
    all_md: list[str] = []
    total = 0

    for project in sorted(p for p in src.iterdir() if p.is_dir()):
        files = list(iter_session_files(project))
        if not files:
            continue

        sessions = []
        for f in files:
            try:
                s = parse_session(f, project.name)
            except OSError as e:
                print(f"  ! не удалось прочитать {f}: {e}", file=sys.stderr)
                continue
            if not s.messages:
                continue
            if f.parent.name == "subagents":
                s.session_id = f"{f.parent.parent.name}__{f.stem}"
                if not opts.sidechain:
                    continue
                for m in s.messages:
                    m.is_sidechain = False  # в отдельном файле показываем как обычные
            sessions.append(s)

        if opts.project:
            label = (sessions[0].project_path if sessions else None) or project.name
            if not any(p.lower() in label.lower() or p.lower() in project.name.lower()
                       for p in opts.project):
                continue

        def in_range(s: Session) -> bool:
            if not s.started:
                return True
            st = parse_date(s.started.replace("Z", "+00:00"))
            return (not opts.since or st >= opts.since) and (not opts.until or st < opts.until)

        sessions = sorted(filter(in_range, sessions), key=lambda s: s.started or "")
        if not sessions:
            continue

        proj_label = sessions[0].project_path or project.name
        proj_out = out_root / safe_name(project.name.lstrip("-"))
        proj_out.mkdir(parents=True, exist_ok=True)
        print(f"📁 {proj_label}  —  {len(sessions)} сесс.")

        if opts.single_file:
            all_md.append(f"# Проект: {proj_label}\n")

        for s in sessions:
            date = (s.started or "")[:10] or "nodate"
            sid = safe_name(s.session_id) if "__" in s.session_id else s.session_id[:8]
            base = f"{date}_{safe_name(s.title or '', 50)}_{sid}" if s.title else f"{date}_{sid}"

            if "md" in opts.format:
                (proj_out / f"{base}.md").write_text(session_to_markdown(s, opts), encoding="utf-8")
            if "json" in opts.format:
                (proj_out / f"{base}.json").write_text(
                    json.dumps(session_to_dict(s), ensure_ascii=False, indent=2), encoding="utf-8")
            if opts.single_file:
                all_md.append(session_to_markdown(s, opts, heading_level=2))

            index.append({
                "project": proj_label, "session_id": s.session_id, "title": s.title,
                "started": s.started, "ended": s.ended, "messages": len(s.messages),
                "file": str((proj_out / base).relative_to(out_root)),
            })
            total += 1

    # Оглавление
    lines = ["# Экспорт Claude Code", "",
             f"Экспортировано: {datetime.now():%Y-%m-%d %H:%M}, сессий: {total}", ""]
    current = None
    for it in index:
        if it["project"] != current:
            current = it["project"]
            lines += ["", f"## `{current}`", ""]
        link = it["file"] + (".md" if "md" in opts.format else ".json")
        lines.append(f"- {fmt_ts(it['started'])} — [{it['title'] or it['session_id'][:8]}]"
                     f"({link.replace(' ', '%20')}) ({it['messages']} сообщ.)")
    (out_root / "INDEX.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out_root / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")

    if opts.single_file:
        (out_root / "all_conversations.md").write_text("\n\n---\n\n".join(all_md), encoding="utf-8")

    print(f"\n✅ Готово: {total} сессий → {out_root.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
