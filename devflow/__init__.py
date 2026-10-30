"""devflow: Claude Code plans and reviews, opencode codes, Telegram steers."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("devflow-bot")
except PackageNotFoundError:  # running from a checkout that was never installed
    __version__ = "0+unknown"
