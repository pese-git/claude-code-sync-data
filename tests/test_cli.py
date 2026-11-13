from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import History, run_cli

from claude_code_sync import __version__, cli


def test_help_lists_commands(capsys: pytest.CaptureFixture[str]):
    code, out, _ = run_cli(capsys, "--help")
    assert code == 0
    assert "export" in out and "import" in out


def test_version(capsys: pytest.CaptureFixture[str]):
    code, out, _ = run_cli(capsys, "--version")
    assert code == 0
    assert out.strip() == f"claude-code-sync {__version__}"


def test_command_is_required(capsys: pytest.CaptureFixture[str]):
    code, _, err = run_cli(capsys, "")
    assert code == 2 and "invalid choice" in err
    code, _, err = run_cli(capsys)
    assert code == 2 and "required" in err


@pytest.mark.parametrize(
    ("legacy", "command"), [(cli.export_main, "export"), (cli.import_main, "import")]
)
def test_legacy_entry_points_delegate_to_subcommands(
    legacy, command: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setattr(sys, "argv", [f"claude-code-{command}", "--help"])
    with pytest.raises(SystemExit) as exc:
        legacy()
    assert exc.value.code == 0
    assert f"usage: claude-code-sync {command}" in capsys.readouterr().out


def run_subprocess(
    *args: str | Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    """python -m claude_code_sync в отдельном процессе; вывод идёт в pipe, как при `> log.txt`."""
    return subprocess.run(
        [sys.executable, "-m", "claude_code_sync", *map(str, args)],
        capture_output=True,
        encoding="utf-8",
        errors="replace",  # чтобы упавший процесс показал stderr, а не сломал тест
        env=env,
        check=False,
    )


def test_python_dash_m(history: History, tmp_path: Path):
    """python -m claude_code_sync работает как установленная команда, вывод в pipe — UTF-8.

    На Windows без configure_output() этот вызов падал: pipe получал cp1252, а экспорт печатает 📁.
    """
    out = tmp_path / "out"
    result = run_subprocess("export", "-s", history.root, "-o", out)
    assert result.returncode == 0, result.stderr
    assert "📁 /home/user/my app" in result.stdout
    assert "Готово: 3 сессий" in result.stdout
    assert "битая строка JSON" in result.stderr
    assert (out / "INDEX.md").is_file()


def test_explicit_non_utf8_output_encoding_does_not_crash(history: History, tmp_path: Path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHONUTF8")}
    env["PYTHONIOENCODING"] = "cp1252"
    result = run_subprocess("export", "-s", history.root, "-o", tmp_path / "out", env=env)
    assert result.returncode == 0, result.stderr
    # 📁 нет в cp1252 — заменён escape-последовательностью, а не уронил процесс
    assert "\\U0001f4c1" in result.stdout


def fake_stream(encoding: str) -> io.TextIOWrapper:
    return io.TextIOWrapper(io.BytesIO(), encoding=encoding)


def test_configure_output_switches_locale_encoding_to_utf8(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    raw = io.BytesIO()
    out = io.TextIOWrapper(raw, encoding="cp1252", newline="\n")  # без \r\n на Windows
    err, ok = fake_stream("cp1252"), fake_stream("UTF-8")
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    cli.configure_output()
    assert out.encoding == err.encoding == "utf-8"

    print("📁 Готово", file=out)
    out.flush()
    assert raw.getvalue().decode("utf-8") == "📁 Готово\n"

    monkeypatch.setattr(sys, "stdout", ok)
    cli.configure_output()  # уже UTF-8 — не трогаем
    assert ok.encoding == "UTF-8"


def test_configure_output_respects_explicit_encoding(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PYTHONIOENCODING", "cp1252")
    out = fake_stream("cp1252")
    monkeypatch.setattr(sys, "stdout", out)
    cli.configure_output()
    assert (out.encoding, out.errors) == ("cp1252", "backslashreplace")


def test_configure_output_ignores_replaced_streams(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "stdout", None)  # pythonw: потоков нет
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    cli.configure_output()  # не падает
