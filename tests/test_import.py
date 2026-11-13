from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import AGENT_ID, PROJECT_DIR, PROJECT_PATH, SESSION_ID, History, Runner

import import_claude_code as im

# ---------------------------------------------------------------- утилиты


def test_encode_project_dir():
    assert im.encode_project_dir(PROJECT_PATH) == PROJECT_DIR
    assert im.encode_project_dir("C:\\Users\\me\\proj") == "C--Users-me-proj"


def test_remapper_matches_whole_path_components():
    remap = im.Remapper(["/Users/me/=/home/me", "C:\\old=D:\\new"])
    assert remap("/Users/me") == "/home/me"
    assert remap("/Users/me/work/app") == "/home/me/work/app"
    assert remap("/Users/meow/app") == "/Users/meow/app"  # не префикс компонента
    assert remap("C:\\old\\x") == "D:\\new\\x"
    assert remap(None) is None


def test_remapper_first_rule_wins():
    remap = im.Remapper(["/a/b=/x", "/a=/y"])
    assert remap("/a/b/c") == "/x/c"
    assert remap("/a/c") == "/y/c"


def test_remapper_rejects_bad_rule():
    with pytest.raises(SystemExit, match="OLD=NEW"):
        im.Remapper(["no-equals-sign"])


# ---------------------------------------------------------------- распознавание субагентов


def test_detect_subagent_by_index_meta():
    meta = {"source_rel": f"{PROJECT_DIR}/{SESSION_ID}/subagents/agent-{AGENT_ID}.jsonl"}
    assert im.detect_subagent([], SESSION_ID, meta) == (SESSION_ID, f"agent-{AGENT_ID}")
    assert im.detect_subagent(
        [], SESSION_ID, {"source_rel": f"{PROJECT_DIR}/{SESSION_ID}.jsonl"}
    ) == (None, None)


def test_detect_subagent_without_index_uses_agent_id():
    entries = [{"agentId": AGENT_ID, "isSidechain": True}, {"type": "summary"}]
    assert im.detect_subagent(entries, SESSION_ID, None) == (SESSION_ID, f"agent-{AGENT_ID}")
    # обычная сессия, где есть и не-сайдчейн записи, субагентом не считается
    mixed = [{"agentId": AGENT_ID, "isSidechain": True}, {"isSidechain": False}]
    assert im.detect_subagent(mixed, SESSION_ID, None) == (None, None)
    # index.json есть, но о файле ничего не знает про субагента — agentId не используется
    assert im.detect_subagent(entries, SESSION_ID, {"file": "x"}) == (None, None)


# ---------------------------------------------------------------- json → записи API


def test_block_to_api():
    assert im.block_to_api({"type": "text", "text": ""}) is None
    assert im.block_to_api({"type": "thinking", "text": "x"}) is None  # без подписи не восстановить
    assert im.block_to_api({"type": "image"}) == {
        "type": "text",
        "text": "[изображение не сохранено при экспорте]",
    }
    assert im.block_to_api({"type": "tool_result", "id": "t1", "text": "e", "is_error": True}) == {
        "type": "tool_result",
        "tool_use_id": "t1",
        "content": "e",
        "is_error": True,
    }
    tool = im.block_to_api({"type": "tool_use"}) or {}
    assert tool["id"].startswith("toolu_") and tool["name"] == "unknown" and tool["input"] == {}
    assert im.block_to_api({"type": "weird", "raw": {"type": "weird", "v": 1}}) == {
        "type": "weird",
        "v": 1,
    }


def test_build_message():
    text = [{"type": "text", "text": "hi"}]
    assert im.build_message("user", text, None, "u") == {"role": "user", "content": "hi"}
    msg = im.build_message("assistant", text, None, "0123456789abcdef")
    assert msg["model"] == "imported" and msg["id"] == "msg_imported_0123456789ab"
    assert msg["content"] == text


# ---------------------------------------------------------------- CLI: полный цикл


def read_entries(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def project_files(target: Path) -> dict[str, bytes]:
    return {
        p.relative_to(target).as_posix(): p.read_bytes() for p in sorted(target.rglob("*.jsonl"))
    }


@pytest.fixture
def raw_export(history: History, run_export: Runner, tmp_path: Path) -> Path:
    out = tmp_path / "export"
    code, _, _ = run_export(
        "-s", history.root, "-o", out, "--format", "md", "json", "raw", "--sidechain"
    )
    assert code == 0
    return out


def test_raw_round_trip_is_byte_exact(
    history: History, raw_export: Path, run_import: Runner, tmp_path: Path
):
    target = tmp_path / "target"
    code, stdout, _ = run_import(raw_export, "-t", target)

    assert code == 0
    assert "Импортировано: 4" in stdout
    restored = project_files(target)
    original = project_files(history.root)
    original.pop(f"{PROJECT_DIR}/empty.jsonl")  # пустые сессии не экспортируются
    assert restored == original


def test_raw_round_trip_without_index(
    history: History, raw_export: Path, run_import: Runner, tmp_path: Path
):
    (raw_export / "index.json").unlink()
    target = tmp_path / "target"
    run_import(raw_export, "-t", target)
    sub = target / PROJECT_DIR / SESSION_ID / "subagents" / f"agent-{AGENT_ID}.jsonl"
    assert sub.read_bytes() == history.subagent.read_bytes()
    assert (
        target / PROJECT_DIR / f"{SESSION_ID}.jsonl"
    ).read_bytes() == history.main_session.read_bytes()


def test_import_from_json_builds_resumable_chain(
    history: History, run_export: Runner, run_import: Runner, tmp_path: Path
):
    out = tmp_path / "export"
    run_export(
        "-s", history.root, "-o", out, "--format", "json", "--sidechain", "--project", "my app"
    )
    target = tmp_path / "target"
    code, _, _ = run_import(out, "-t", target)
    assert code == 0

    summary, *messages = read_entries(target / PROJECT_DIR / f"{SESSION_ID}.jsonl")
    assert summary == {
        "type": "summary",
        "summary": "Починка логина",
        "leafUuid": messages[-1]["uuid"],
    }
    assert messages[0]["parentUuid"] is None
    assert all(m["parentUuid"] == prev["uuid"] for prev, m in zip(messages, messages[1:]))
    assert {m["sessionId"] for m in messages} == {SESSION_ID}
    assert {m["cwd"] for m in messages} == {PROJECT_PATH}
    # размышления выброшены, вызов инструмента сохранён
    assistant = messages[2]["message"]["content"]
    assert [b["type"] for b in assistant] == ["text", "tool_use"]

    sub = target / PROJECT_DIR / SESSION_ID / "subagents" / f"agent-{AGENT_ID}.jsonl"
    sub_entries = read_entries(sub)
    assert all(
        e["isSidechain"] and e["agentId"] == AGENT_ID and e["sessionId"] == SESSION_ID
        for e in sub_entries
    )


def test_existing_sessions_are_skipped_unless_overwrite(
    raw_export: Path, run_import: Runner, tmp_path: Path
):
    target = tmp_path / "target"
    run_import(raw_export, "-t", target)
    main = target / PROJECT_DIR / f"{SESSION_ID}.jsonl"
    main.write_text("изменено", encoding="utf-8")

    _, stdout, _ = run_import(raw_export, "-t", target)
    assert "Импортировано: 0, пропущено (уже есть): 4" in stdout
    assert main.read_text(encoding="utf-8") == "изменено"

    run_import(raw_export, "-t", target, "--overwrite")
    assert main.read_text(encoding="utf-8") != "изменено"


def test_dry_run_writes_nothing(raw_export: Path, run_import: Runner, tmp_path: Path):
    target = tmp_path / "target"
    code, stdout, _ = run_import(raw_export, "-t", target, "--dry-run")
    assert code == 0
    assert "Будет импортировано: 4" in stdout
    assert not target.exists()


def test_remap_moves_project_and_rewrites_cwd(raw_export: Path, run_import: Runner, tmp_path: Path):
    target = tmp_path / "target"
    run_import(raw_export, "-t", target, "--remap", "/home/user=/Users/me")
    session = target / "-Users-me-my-app" / f"{SESSION_ID}.jsonl"
    cwds = {e.get("cwd") for e in read_entries(session)}
    assert cwds == {None, "/Users/me/my app"}  # у summary/system cwd нет
    assert (
        target / "-Users-me-my-app" / SESSION_ID / "subagents" / f"agent-{AGENT_ID}.jsonl"
    ).exists()


def test_new_ids_keep_subagent_with_its_session(
    raw_export: Path, run_import: Runner, tmp_path: Path
):
    target = tmp_path / "target"
    run_import(raw_export, "-t", target, "--new-ids", "--project", "my app")
    proj = target / PROJECT_DIR
    sessions = sorted(p.stem for p in proj.glob("*.jsonl"))
    assert len(sessions) == 2 and SESSION_ID not in sessions

    subs = list(proj.glob("*/subagents/*.jsonl"))
    assert len(subs) == 1
    new_sid = subs[0].parent.parent.name
    assert new_sid in sessions
    main = read_entries(proj / f"{new_sid}.jsonl")
    assert {e["sessionId"] for e in main if "sessionId" in e} == {new_sid}
    assert not {e.get("uuid") for e in main} & {"u1", "a1", "a2"}  # uuid тоже новые


def test_project_filter_and_missing_cwd(raw_export: Path, run_import: Runner, tmp_path: Path):
    (raw_export / "nocwd.jsonl").write_text(
        json.dumps({"type": "user", "sessionId": "x"}) + "\n", encoding="utf-8"
    )
    target = tmp_path / "target"
    _, stdout, err = run_import(raw_export, "-t", target, "--project", "other")
    assert "Импортировано: 1" in stdout and "без пути проекта: 1" in stdout
    assert "nocwd.jsonl: неизвестен путь проекта" in err
    assert [p.parent.name for p in target.rglob("*.jsonl")] == ["-srv-other"]


def test_import_single_file(raw_export: Path, run_import: Runner, tmp_path: Path):
    target = tmp_path / "target"
    src = raw_export / "srv-other" / "2026-11-03_33333333.json"
    code, stdout, _ = run_import(src, "-t", target)
    assert code == 0 and "Импортировано: 1" in stdout
    assert (target / "-srv-other" / "33333333.jsonl").exists()


def test_import_errors(run_import: Runner, tmp_path: Path):
    code, _, err = run_import(tmp_path / "nope")
    assert code == 1 and "Не найден источник" in err

    empty = tmp_path / "empty"
    empty.mkdir()
    code, _, err = run_import(empty)
    assert code == 1 and "Не найдено ни одной сессии" in err

    code, _, err = run_import(empty, "--remap", "bad")
    assert code == 1 and "OLD=NEW" in err
