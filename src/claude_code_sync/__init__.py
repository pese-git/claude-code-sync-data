"""Экспорт, импорт и перенос переписки Claude Code."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("claude-code-sync-data")
except PackageNotFoundError:  # запуск из исходников без установки
    __version__ = "0+unknown"
