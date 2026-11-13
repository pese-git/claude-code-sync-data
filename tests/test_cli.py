from __future__ import annotations

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


def test_python_dash_m(history: History, tmp_path: Path):
    """python -m claude_code_sync работает как установленная команда."""
    out = tmp_path / "out"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "claude_code_sync",
            "export",
            "-s",
            str(history.root),
            "-o",
            str(out),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (out / "INDEX.md").is_file()
