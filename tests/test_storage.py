from __future__ import annotations

from pathlib import Path

import pytest
from conftest import PROJECT_DIR, PROJECT_PATH

from claude_code_sync import storage


def test_encode_project_dir():
    assert storage.encode_project_dir(PROJECT_PATH) == PROJECT_DIR
    assert storage.encode_project_dir("C:\\Users\\me\\proj") == "C--Users-me-proj"


def test_iter_jsonl_skips_blank_and_broken_lines(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    path = tmp_path / "s.jsonl"
    path.write_text('{"a": 1}\n\n{битая\n  {"b": "я"}  \n', encoding="utf-8")

    assert list(storage.iter_jsonl(path)) == [{"a": 1}, {"b": "я"}]
    assert capsys.readouterr().err == ""

    list(storage.iter_jsonl(path, warn=True))
    assert "s.jsonl:3: битая строка JSON" in capsys.readouterr().err


def test_write_jsonl_round_trip(tmp_path: Path):
    path = tmp_path / "deep" / "dir" / "s.jsonl"
    entries = [{"text": "привет"}, {"n": 2}]
    storage.write_jsonl(path, entries)

    assert path.read_text(encoding="utf-8") == '{"text":"привет"}\n{"n":2}\n'
    assert list(storage.iter_jsonl(path)) == entries
    assert not list(path.parent.glob("*.tmp"))  # временный файл не остаётся
