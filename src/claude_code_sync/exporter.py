"""
Команда `claude-code-sync export`: выгрузка переписки Claude Code по всем проектам.

Читает сессии из ~/.claude/projects/<закодированный-путь>/<session-id>.jsonl и сохраняет
каждую в Markdown (для чтения), JSON (для обработки) и/или raw (копия .jsonl для импорта).

Примеры:
    claude-code-sync export
    claude-code-sync export -o ~/claude-export --format md json
    claude-code-sync export --project myapp --since 2026-10-01
    claude-code-sync export --tools --thinking          # с вызовами инструментов и размышлениями
    claude-code-sync export --single-file               # всё в один all_conversations.md
    claude-code-sync export --format md raw --sidechain # raw — для claude-code-sync import
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .storage import DEFAULT_PROJECTS_DIR, iter_jsonl

TOOL_RESULT_LIMIT = 4000  # символов результата инструмента в Markdown

# Расширение файла для каждого формата. Порядок задаёт, на какой файл ссылается INDEX.md.
FORMAT_EXT = {"md": ".md", "json": ".json", "raw": ".jsonl"}


# ---------------------------------------------------------------- модель данных


@dataclass
class Message:
    role: str  # user | assistant
    timestamp: str | None
    blocks: list[dict]  # нормализованные блоки: {"type": ..., ...}
    is_sidechain: bool = False
    model: str | None = None


@dataclass
class Session:
    session_id: str
    project_dir: str  # имя папки в ~/.claude/projects
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


@dataclass
class RenderOptions:
    """Что включать в Markdown."""

    tools: bool = False
    thinking: bool = False
    sidechain: bool = False
    keep_noise: bool = False  # --raw: не вырезать служебные сообщения
    max_tool_output: int = TOOL_RESULT_LIMIT


# ---------------------------------------------------------------- парсинг


def as_list(value: Any) -> list:
    return value if isinstance(value, list) else [value]


def normalize_block(b: Any) -> dict | None:
    """Один блок message.content → нормализованный блок (или None, если это не блок)."""
    if isinstance(b, str):
        return {"type": "text", "text": b}
    if not isinstance(b, dict):
        return None
    t = b.get("type")
    if t == "text":
        return {"type": "text", "text": b.get("text", "")}
    if t == "thinking":
        return {"type": "thinking", "text": b.get("thinking", "")}
    if t == "redacted_thinking":
        return {"type": "thinking", "text": "[скрыто]"}
    if t == "tool_use":
        return {
            "type": "tool_use",
            "id": b.get("id"),
            "name": b.get("name"),
            "input": b.get("input", {}),
        }
    if t == "tool_result":
        return {
            "type": "tool_result",
            "id": b.get("tool_use_id"),
            "is_error": bool(b.get("is_error")),
            "text": flatten_tool_result(b.get("content")),
        }
    if t == "image":
        return {"type": "image"}
    return {"type": t or "unknown", "raw": b}


def normalize_content(content: Any) -> list[dict]:
    """Приводит message.content (строка или список блоков) к списку блоков."""
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [nb for b in as_list(content) if (nb := normalize_block(b)) is not None]


def flatten_tool_result(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for c in as_list(content):
        if not isinstance(c, dict):
            parts.append(str(c))
        elif c.get("type") == "text":
            parts.append(c.get("text", ""))
        elif c.get("type") == "image":
            parts.append("[изображение]")
        else:
            parts.append(json.dumps(c, ensure_ascii=False))
    return "\n".join(parts)


def merge_assistant_chunks(messages: list[Message]) -> list[Message]:
    """Сообщения ассистента приходят кусками (по блоку на запись) — склеиваем подряд идущие."""
    merged: list[Message] = []
    for m in messages:
        prev = merged[-1] if merged else None
        if prev and m.role == prev.role == "assistant" and m.is_sidechain == prev.is_sidechain:
            prev.blocks.extend(m.blocks)
        else:
            merged.append(m)
    return merged


def parse_session(path: Path, project_dir: str) -> Session:
    s = Session(session_id=path.stem, project_dir=project_dir, project_path=None, file=path)
    seen_uuids: set[str] = set()

    for e in iter_jsonl(path, warn=True):
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
        if blocks:
            s.messages.append(
                Message(
                    role=msg.get("role") or etype,
                    timestamp=e.get("timestamp"),
                    blocks=blocks,
                    is_sidechain=bool(e.get("isSidechain")),
                    model=msg.get("model"),
                )
            )

    s.messages = merge_assistant_chunks(s.messages)
    return s


def iter_session_files(project: Path) -> Iterable[Path]:
    """Основные сессии + сессии субагентов (<session>/subagents/*.jsonl)."""
    yield from sorted(project.glob("*.jsonl"))
    yield from sorted(project.glob("*/subagents/*.jsonl"))


def is_subagent_file(path: Path) -> bool:
    return path.parent.name == "subagents"


# ---------------------------------------------------------------- фильтр шума

SYSTEM_NOISE = re.compile(
    r"^\s*(<command-name>|<local-command-stdout>|<command-message>|<local-command-caveat>|"
    r"Caveat: The messages below|\[Request interrupted)",
    re.S,
)
SYSTEM_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)


def is_noise(text: str) -> bool:
    return bool(SYSTEM_NOISE.match(text))


def strip_system_reminders(text: str) -> str:
    return SYSTEM_REMINDER.sub("", text).strip()


# ---------------------------------------------------------------- Markdown


def fmt_ts(ts: str | None) -> str:
    if not ts:
        return ""
    try:
        return parse_iso(ts).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ts


def fence(text: str, lang: str = "") -> str:
    ticks = "```"
    while ticks in text:
        ticks += "`"
    return f"{ticks}{lang}\n{text}\n{ticks}"


def details(summary: str, body: str) -> str:
    return f"<details><summary>{summary}</summary>\n\n{body}\n\n</details>"


def truncate(text: str, limit: int) -> str:
    if limit and len(text) > limit:
        return text[:limit] + f"\n… [обрезано, всего {len(text)} символов]"
    return text


def render_block(b: dict, opts: RenderOptions) -> str | None:
    """Markdown для одного блока или None, если блок не показываем."""
    t = b["type"]
    if t == "text":
        text = strip_system_reminders(b["text"])
        if not text or (not opts.keep_noise and is_noise(text)):
            return None
        return text
    if t == "thinking" and opts.thinking and b["text"].strip():
        return details("💭 Размышления", b["text"].strip())
    if t == "tool_use" and opts.tools:
        args = json.dumps(b["input"], ensure_ascii=False, indent=2)
        return f"🔧 **{b['name']}**\n\n" + fence(args, "json")
    if t == "tool_result" and opts.tools:
        label = "❌ Ошибка инструмента" if b["is_error"] else "📤 Результат"
        return details(label, fence(truncate(b["text"], opts.max_tool_output)))
    if t == "image":
        return "_[изображение]_"
    return None


def speaker(m: Message) -> str:
    if all(b["type"] == "tool_result" for b in m.blocks):
        who = "⚙️ Инструмент"
    elif m.role == "user":
        who = "👤 Пользователь"
    else:
        who = "🤖 Claude"
    return who + " (субагент)" if m.is_sidechain else who


def session_to_markdown(s: Session, opts: RenderOptions, heading_level: int = 1) -> str:
    h = "#" * heading_level
    out = [
        f"{h} {s.title or 'Сессия ' + s.session_id}",
        "",
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
        parts = [p for b in m.blocks if (p := render_block(b, opts)) is not None]
        if parts:
            out += [f"{h}# {speaker(m)}  ·  {fmt_ts(m.timestamp)}", "", "\n\n".join(parts), ""]
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
            {
                "role": m.role,
                "timestamp": m.timestamp,
                "model": m.model,
                "is_sidechain": m.is_sidechain,
                "content": m.blocks,
            }
            for m in s.messages
        ],
    }


# ---------------------------------------------------------------- отбор сессий


def parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def parse_date(s: str) -> datetime:
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def load_project_sessions(project: Path, with_subagents: bool) -> list[Session]:
    """Все непустые сессии проекта; сессии субагентов — только если with_subagents."""
    sessions = []
    for f in iter_session_files(project):
        try:
            s = parse_session(f, project.name)
        except OSError as e:
            print(f"  ! не удалось прочитать {f}: {e}", file=sys.stderr)
            continue
        if not s.messages:
            continue
        if is_subagent_file(f):
            s.session_id = f"{f.parent.parent.name}__{f.stem}"
            if not with_subagents:
                continue
            for m in s.messages:
                m.is_sidechain = False  # в отдельном файле показываем как обычные
        sessions.append(s)
    return sessions


def project_matches(project: Path, sessions: list[Session], patterns: Sequence[str]) -> bool:
    if not patterns:
        return True
    label = (sessions[0].project_path if sessions else None) or project.name
    names = (label.lower(), project.name.lower())
    return any(p.lower() in n for p in patterns for n in names)


def in_range(s: Session, since: datetime | None, until: datetime | None) -> bool:
    if not s.started:
        return True
    st = parse_date(s.started.replace("Z", "+00:00"))
    return (not since or st >= since) and (not until or st < until)


# ---------------------------------------------------------------- запись


def safe_name(name: str, maxlen: int = 80) -> str:
    name = re.sub(r"[^\w\-.]+", "_", name, flags=re.U).strip("_.")
    return name[:maxlen] or "untitled"


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def session_basename(s: Session) -> str:
    """Имя файла без расширения: <дата>_<заголовок>_<id>."""
    date = (s.started or "")[:10] or "nodate"
    sid = safe_name(s.session_id) if "__" in s.session_id else s.session_id[:8]
    return f"{date}_{safe_name(s.title, 50)}_{sid}" if s.title else f"{date}_{sid}"


def export_session(s: Session, base: Path, formats: Sequence[str], opts: RenderOptions) -> None:
    if "md" in formats:
        base.with_name(base.name + ".md").write_text(session_to_markdown(s, opts), encoding="utf-8")
    if "json" in formats:
        write_json(base.with_name(base.name + ".json"), session_to_dict(s))
    if "raw" in formats:
        shutil.copy2(s.file, base.with_name(base.name + ".jsonl"))  # байт в байт


def write_index(out_root: Path, index: list[dict], formats: Sequence[str]) -> None:
    ext = next(e for f, e in FORMAT_EXT.items() if f in formats)
    lines = [
        "# Экспорт Claude Code",
        "",
        f"Экспортировано: {datetime.now():%Y-%m-%d %H:%M}, сессий: {len(index)}",
        "",
    ]
    current = None
    for it in index:
        if it["project"] != current:
            current = it["project"]
            lines += ["", f"## `{current}`", ""]
        link = (it["file"] + ext).replace(" ", "%20")
        lines.append(
            f"- {fmt_ts(it['started'])} — [{it['title'] or it['session_id'][:8]}]"
            f"({link}) ({it['messages']} сообщ.)"
        )
    (out_root / "INDEX.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    write_json(out_root / "index.json", index)


# ---------------------------------------------------------------- команда


def add_arguments(ap: argparse.ArgumentParser) -> None:
    ap.add_argument(
        "-s",
        "--source",
        type=Path,
        default=DEFAULT_PROJECTS_DIR,
        help=f"папка с проектами (по умолчанию {DEFAULT_PROJECTS_DIR})",
    )
    ap.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("claude_code_export"),
        help="куда сохранить (по умолчанию ./claude_code_export)",
    )
    ap.add_argument(
        "--format",
        nargs="+",
        choices=list(FORMAT_EXT),
        default=["md"],
        help="форматы вывода: md, json, raw (можно несколько; raw — копия .jsonl для импорта)",
    )
    ap.add_argument(
        "--project",
        action="append",
        default=[],
        help="экспортировать только проекты, путь которых содержит подстроку (можно несколько)",
    )
    ap.add_argument(
        "--since", type=parse_date, help="только сессии, начатые после даты (YYYY-MM-DD)"
    )
    ap.add_argument("--until", type=parse_date, help="только сессии, начатые до даты (YYYY-MM-DD)")
    ap.add_argument(
        "--tools", action="store_true", help="включить вызовы инструментов и их результаты"
    )
    ap.add_argument("--thinking", action="store_true", help="включить блоки размышлений")
    ap.add_argument("--sidechain", action="store_true", help="включить сообщения субагентов")
    ap.add_argument(
        "--raw",
        dest="keep_noise",
        action="store_true",
        help="не фильтровать служебные сообщения (/команды и т.п.)",
    )
    ap.add_argument(
        "--max-tool-output",
        type=int,
        default=TOOL_RESULT_LIMIT,
        help="макс. длина результата инструмента в md (0 = без ограничения)",
    )
    ap.add_argument(
        "--single-file",
        action="store_true",
        help="дополнительно собрать всё в один all_conversations.md",
    )


def run(args: argparse.Namespace) -> int:
    opts = RenderOptions(
        tools=args.tools,
        thinking=args.thinking,
        sidechain=args.sidechain,
        keep_noise=args.keep_noise,
        max_tool_output=args.max_tool_output,
    )

    src: Path = args.source.expanduser()
    if not src.is_dir():
        print(f"Не найдена папка {src}. Укажите путь через --source.", file=sys.stderr)
        return 1

    out_root: Path = args.output.expanduser()
    out_root.mkdir(parents=True, exist_ok=True)

    index: list[dict] = []
    all_md: list[str] = []

    for project in sorted(p for p in src.iterdir() if p.is_dir()):
        sessions = load_project_sessions(project, with_subagents=args.sidechain)
        if not project_matches(project, sessions, args.project):
            continue
        sessions = sorted(
            (s for s in sessions if in_range(s, args.since, args.until)),
            key=lambda s: s.started or "",
        )
        if not sessions:
            continue

        proj_label = sessions[0].project_path or project.name
        proj_out = out_root / safe_name(project.name.lstrip("-"))
        proj_out.mkdir(parents=True, exist_ok=True)
        print(f"📁 {proj_label}  —  {len(sessions)} сесс.")

        if args.single_file:
            all_md.append(f"# Проект: {proj_label}\n")

        for s in sessions:
            base = proj_out / session_basename(s)
            export_session(s, base, args.format, opts)
            if args.single_file:
                all_md.append(session_to_markdown(s, opts, heading_level=2))

            index.append(
                {
                    "project": proj_label,
                    "session_id": s.session_id,
                    "title": s.title,
                    "started": s.started,
                    "ended": s.ended,
                    "messages": len(s.messages),
                    # через / и на Windows: это ещё и ссылка в INDEX.md
                    "file": base.relative_to(out_root).as_posix(),
                    # исходное место: по нему импорт узнаёт субагентов (<session>/subagents/…)
                    "source_rel": s.file.relative_to(src).as_posix(),
                }
            )

    write_index(out_root, index, args.format)
    if args.single_file:
        (out_root / "all_conversations.md").write_text("\n\n---\n\n".join(all_md), encoding="utf-8")

    print(f"\n✅ Готово: {len(index)} сессий → {out_root.resolve()}")
    return 0
