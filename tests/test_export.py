from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from conftest import (
    AGENT_ID,
    OTHER_SESSION_ID,
    PROJECT_DIR,
    PROJECT_PATH,
    SESSION_ID,
    History,
    Runner,
)

from claude_code_sync import exporter as ex

# ---------------------------------------------------------------- нормализация блоков


@pytest.mark.parametrize(
    ("block", "expected"),
    [
        ("просто строка", {"type": "text", "text": "просто строка"}),
        ({"type": "text", "text": "hi"}, {"type": "text", "text": "hi"}),
        ({"type": "thinking", "thinking": "хм"}, {"type": "thinking", "text": "хм"}),
        ({"type": "redacted_thinking", "data": "x"}, {"type": "thinking", "text": "[скрыто]"}),
        (
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"cmd": "ls"}},
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"cmd": "ls"}},
        ),
        (
            {"type": "tool_result", "tool_use_id": "t1", "is_error": 1, "content": "boom"},
            {"type": "tool_result", "id": "t1", "is_error": True, "text": "boom"},
        ),
        ({"type": "image", "source": {}}, {"type": "image"}),
        ({"type": "weird", "k": 1}, {"type": "weird", "raw": {"type": "weird", "k": 1}}),
        ({"k": 1}, {"type": "unknown", "raw": {"k": 1}}),
        (42, None),
    ],
)
def test_normalize_block(block, expected):
    assert ex.normalize_block(block) == expected


def test_normalize_content_accepts_string_list_and_none():
    assert ex.normalize_content(None) == []
    assert ex.normalize_content("hi") == [{"type": "text", "text": "hi"}]
    assert ex.normalize_content(["a", 1, {"type": "image"}]) == [
        {"type": "text", "text": "a"},
        {"type": "image"},
    ]


def test_flatten_tool_result():
    content = [{"type": "text", "text": "a"}, {"type": "image"}, {"type": "x", "v": 1}, 7]
    assert ex.flatten_tool_result(content) == 'a\n[изображение]\n{"type": "x", "v": 1}\n7'
    assert ex.flatten_tool_result(None) == ""
    assert ex.flatten_tool_result("plain") == "plain"


def test_merge_assistant_chunks_joins_only_consecutive_same_sidechain():
    def msg(role: str, text: str, side: bool = False) -> ex.Message:
        return ex.Message(role, None, [{"type": "text", "text": text}], is_sidechain=side)

    merged = ex.merge_assistant_chunks(
        [
            msg("assistant", "1"),
            msg("assistant", "2"),
            msg("assistant", "3", side=True),
            msg("user", "4"),
            msg("assistant", "5"),
        ]
    )
    assert [[b["text"] for b in m.blocks] for m in merged] == [["1", "2"], ["3"], ["4"], ["5"]]


# ---------------------------------------------------------------- разбор сессии


def test_parse_session(history: History, capsys: pytest.CaptureFixture[str]):
    s = ex.parse_session(history.main_session, PROJECT_DIR)

    assert s.session_id == SESSION_ID
    assert s.title == "Починка логина"
    assert s.project_path == PROJECT_PATH
    assert s.git_branch == "main"
    assert s.started == "2026-11-02T10:00:00Z"
    assert s.ended == "2026-11-02T10:00:05Z"
    # дубль u1 отброшен, system пропущен, куски ассистента a1+a2 склеены
    assert [m.role for m in s.messages] == ["user", "user", "assistant", "user", "assistant"]
    assert [b["type"] for b in s.messages[2].blocks] == ["thinking", "text", "tool_use"]
    assert "битая строка JSON" in capsys.readouterr().err


def test_custom_title_overrides_summary(tmp_path: Path):
    path = tmp_path / "s.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(e)
            for e in [
                {"type": "summary", "summary": "авто"},
                {"type": "summary", "summary": "второй summary игнорируется"},
                {"type": "custom-title", "customTitle": "своё"},
            ]
        ),
        encoding="utf-8",
    )
    assert ex.parse_session(path, "p").title == "своё"


# ---------------------------------------------------------------- Markdown


def test_noise_and_system_reminders():
    assert ex.is_noise("  <command-name>/clear</command-name>")
    assert ex.is_noise("[Request interrupted by user]")
    assert not ex.is_noise("обычный текст")
    assert ex.strip_system_reminders("a<system-reminder>x\ny</system-reminder> b") == "a b"


def test_fence_grows_past_backticks_inside():
    assert ex.fence("x") == "```\nx\n```"
    assert ex.fence("a ``` b", "py") == "````py\na ``` b\n````"


def test_truncate():
    assert ex.truncate("abcdef", 0) == "abcdef"
    assert ex.truncate("abcdef", 10) == "abcdef"
    assert ex.truncate("abcdef", 3) == "abc\n… [обрезано, всего 6 символов]"


def test_render_block_respects_options():
    off, on = ex.RenderOptions(), ex.RenderOptions(tools=True, thinking=True, keep_noise=True)
    noise = {"type": "text", "text": "<command-name>/x</command-name>"}
    thinking = {"type": "thinking", "text": "мысль"}
    tool = {"type": "tool_use", "name": "Bash", "input": {"c": "ls"}}
    result = {"type": "tool_result", "is_error": True, "text": "x" * 10}

    assert ex.render_block(noise, off) is None
    assert ex.render_block(noise, on) == noise["text"]
    assert ex.render_block(thinking, off) is None
    assert "мысль" in (ex.render_block(thinking, on) or "")
    assert ex.render_block(tool, off) is None
    assert '"c": "ls"' in (ex.render_block(tool, on) or "")
    rendered = ex.render_block(result, ex.RenderOptions(tools=True, max_tool_output=4)) or ""
    assert "Ошибка инструмента" in rendered and "обрезано, всего 10" in rendered
    assert ex.render_block({"type": "image"}, off) == "_[изображение]_"


def test_speaker():
    def m(role, types, side=False):
        return ex.Message(role, None, [{"type": t} for t in types], is_sidechain=side)

    assert ex.speaker(m("user", ["text"])) == "👤 Пользователь"
    assert ex.speaker(m("user", ["tool_result"])) == "⚙️ Инструмент"
    assert ex.speaker(m("assistant", ["text"], side=True)) == "🤖 Claude (субагент)"


# ---------------------------------------------------------------- имена и фильтры


def test_safe_name_and_basename():
    assert ex.safe_name("Fix: login / bug?") == "Fix_login_bug"
    assert ex.safe_name("???") == "untitled"
    assert len(ex.safe_name("x" * 200)) == 80

    s = ex.Session(SESSION_ID, "p", None, Path("f"), title="Починка логина")
    s.messages.append(ex.Message("user", "2026-11-02T10:00:00Z", []))
    assert ex.session_basename(s) == "2026-11-02_Починка_логина_11111111"

    sub = ex.Session(f"{SESSION_ID}__agent-{AGENT_ID}", "p", None, Path("f"))
    assert ex.session_basename(sub) == f"nodate_{SESSION_ID}__agent-{AGENT_ID}"


def test_in_range():
    s = ex.Session("id", "p", None, Path("f"))
    assert ex.in_range(s, None, None)  # без даты — всегда проходит
    s.messages.append(ex.Message("user", "2026-11-02T10:00:00Z", []))
    since = datetime(2026, 11, 1, tzinfo=timezone.utc)
    until = datetime(2026, 11, 2, 10, tzinfo=timezone.utc)
    assert ex.in_range(s, since, None)
    assert not ex.in_range(s, None, until)  # until — строго раньше
    assert not ex.in_range(s, datetime(2026, 12, 1, tzinfo=timezone.utc), None)


def test_project_matches_by_path_or_dir_name():
    s = ex.Session("id", "p", "/home/user/My App", Path("f"))
    proj = Path("-home-user-My-App")
    assert ex.project_matches(proj, [s], [])
    assert ex.project_matches(proj, [s], ["my app"])
    assert ex.project_matches(proj, [s], ["MY-APP"])
    assert not ex.project_matches(proj, [s], ["other"])


# ---------------------------------------------------------------- CLI


def read_index(out: Path) -> list[dict]:
    return json.loads((out / "index.json").read_text(encoding="utf-8"))


def test_export_default_markdown(history: History, run_export: Runner, tmp_path: Path):
    out = tmp_path / "out"
    code, stdout, _ = run_export("-s", history.root, "-o", out)

    assert code == 0
    assert "Готово: 3 сессий" in stdout  # субагент без --sidechain и пустая сессия не попали
    proj = out / "home-user-my-app"
    md = (proj / "2026-11-02_Починка_логина_11111111.md").read_text(encoding="utf-8")
    assert md.startswith("# Починка логина")
    assert f"`{PROJECT_PATH}`" in md
    assert "Почини логин" in md and "скрыть" not in md
    assert "/clear" not in md  # служебное сообщение отфильтровано
    assert "toolu_1" not in md and "надо посмотреть" not in md  # без --tools/--thinking
    assert not list(proj.glob("*.json*"))

    index = read_index(out)
    assert [i["session_id"] for i in index] == [OTHER_SESSION_ID, SESSION_ID, "33333333"]
    assert index[1]["source_rel"] == f"{PROJECT_DIR}/{SESSION_ID}.jsonl"
    assert "(home-user-my-app/2026-11-02_Починка_логина_11111111.md)" in (
        out / "INDEX.md"
    ).read_text(encoding="utf-8")


def test_export_all_formats_with_subagents(history: History, run_export: Runner, tmp_path: Path):
    out = tmp_path / "out"
    code, _, _ = run_export(
        "-s",
        history.root,
        "-o",
        out,
        "--format",
        "md",
        "json",
        "raw",
        "--sidechain",
        "--single-file",
    )
    assert code == 0
    proj = out / "home-user-my-app"

    raw = proj / "2026-11-02_Починка_логина_11111111.jsonl"
    assert raw.read_bytes() == history.main_session.read_bytes()
    sub_base = f"2026-11-02_{SESSION_ID}__agent-{AGENT_ID}"
    assert (proj / f"{sub_base}.jsonl").read_bytes() == history.subagent.read_bytes()

    data = json.loads(
        (proj / "2026-11-02_Починка_логина_11111111.json").read_text(encoding="utf-8")
    )
    assert data["session_id"] == SESSION_ID
    assert data["messages"][2]["content"][2] == {
        "type": "tool_use",
        "id": "toolu_1",
        "name": "Read",
        "input": {"path": "a.py"},
    }

    sub_index = next(i for i in read_index(out) if "__" in i["session_id"])
    assert sub_index["source_rel"] == f"{PROJECT_DIR}/{SESSION_ID}/subagents/agent-{AGENT_ID}.jsonl"
    assert "# Проект: /home/user/my app" in (out / "all_conversations.md").read_text(
        encoding="utf-8"
    )


def test_index_links_to_first_available_format(
    history: History, run_export: Runner, tmp_path: Path
):
    out = tmp_path / "out"
    run_export("-s", history.root, "-o", out, "--format", "raw")
    assert ".jsonl)" in (out / "INDEX.md").read_text(encoding="utf-8")


def test_export_filters(history: History, run_export: Runner, tmp_path: Path):
    out = tmp_path / "by_date"
    run_export("-s", history.root, "-o", out, "--since", "2026-01-01", "--until", "2026-11-03")
    assert [i["session_id"] for i in read_index(out)] == [SESSION_ID]

    out = tmp_path / "by_project"
    run_export("-s", history.root, "-o", out, "--project", "OTHER")
    assert [i["session_id"] for i in read_index(out)] == ["33333333"]


def test_export_tools_and_thinking(history: History, run_export: Runner, tmp_path: Path):
    out = tmp_path / "out"
    run_export("-s", history.root, "-o", out, "--tools", "--thinking", "--raw")
    md = (out / "home-user-my-app" / "2026-11-02_Починка_логина_11111111.md").read_text(
        encoding="utf-8"
    )
    assert "🔧 **Read**" in md and "print('hi')" in md
    assert "надо посмотреть код" in md
    assert "/clear" in md  # --raw оставляет служебные сообщения


def test_export_missing_source(run_export: Runner, tmp_path: Path):
    code, _, err = run_export("-s", tmp_path / "nope", "-o", tmp_path / "out")
    assert code == 1
    assert "Не найдена папка" in err


def test_export_rejects_unknown_format(run_export: Runner, tmp_path: Path):
    code, _, err = run_export("-s", tmp_path, "--format", "pdf")
    assert code == 2
    assert "invalid choice" in err
