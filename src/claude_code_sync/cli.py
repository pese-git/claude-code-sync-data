"""Командная строка: claude-code-sync <команда> [опции]."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from . import __version__, exporter, importer

# Подкоманда → (модуль с add_arguments/run, краткое описание)
COMMANDS = {
    "export": (exporter, "выгрузить переписку в Markdown, JSON и/или raw"),
    "import": (importer, "загрузить экспорт обратно в Claude Code"),
}


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="claude-code-sync",
        description="Экспорт, импорт и перенос переписки Claude Code между машинами.",
    )
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="command", required=True, metavar="команда")
    for name, (module, summary) in COMMANDS.items():
        p = sub.add_parser(name, help=summary, description=summary.capitalize() + ".")
        module.add_arguments(p)
        p.set_defaults(run=module.run)
    return ap


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.run(args)


# Прежние команды claude-code-export / claude-code-import — оставлены для совместимости


def export_main() -> int:
    return main(["export", *sys.argv[1:]])


def import_main() -> int:
    return main(["import", *sys.argv[1:]])
