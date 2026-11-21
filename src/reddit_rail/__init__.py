"""reddit-rail: Reddit research for your agent. No login, no API key.

Public surface:
    reddit_rail.main       -> the CLI entry point (console script `reddit`)
    reddit_rail.__version__
"""

__version__ = "2.0.0"

from .rail import main  # noqa: E402

__all__ = ["main", "__version__"]
