"""RoboGuard package exports.

The main class is loaded lazily so lightweight adapters can run before the
LLM/Spot dependencies are imported.
"""

from typing import Any

__all__ = ["RoboGuard"]


def __getattr__(name: str) -> Any:
    if name == "RoboGuard":
        from roboguard.roboguard import RoboGuard

        return RoboGuard
    raise AttributeError(name)

__all__ = ["RoboGuard"]
