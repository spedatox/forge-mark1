"""FileStateCache — read-before-write grounding (study §3).

The agent holds no live model of the Cell filesystem. It tracks only files it has
touched: content + the mtime it saw. Edits are gated on 'you read this, and it
hasn't changed since' — the single highest-value pattern for keeping edits
grounded in reality without a filesystem watcher. Kept small (an LRU) so a long
session can't grow it without bound.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass


@dataclass
class FileState:
    content: str
    mtime: str          # opaque freshness token from the Cell (stat mtime, as text)
    shown_fully: bool = False
    """Whether the model was shown this file's entire text, as opposed to a
    window of it. Freshness only cares that the file was read; the
    unchanged-re-read shortcut additionally needs to know the model still *has*
    the content, which is false after a ranged read."""


class FileStateCache:
    def __init__(self, max_entries: int = 100) -> None:
        self._cache: "OrderedDict[str, FileState]" = OrderedDict()
        self._max = max_entries

    @staticmethod
    def _norm(path: str) -> str:
        return path.replace("\\", "/").rstrip("/")

    def record(self, path: str, content: str, mtime: str, shown_fully: bool = True) -> None:
        key = self._norm(path)
        self._cache[key] = FileState(content, mtime, shown_fully)
        self._cache.move_to_end(key)
        while len(self._cache) > self._max:
            self._cache.popitem(last=False)

    def clear(self) -> None:
        """Forget every file. Called after compaction: the model's memory of file
        contents is now a summary's, not a transcript's, so read-before-write
        must make it look again rather than trust a read it can no longer see."""
        self._cache.clear()

    def get(self, path: str) -> FileState | None:
        key = self._norm(path)
        st = self._cache.get(key)
        if st is not None:
            self._cache.move_to_end(key)
        return st

    def freshness_error(self, path: str, current_mtime: str | None) -> str | None:
        """Return an explanatory error if `path` may not be edited yet, else None.
        current_mtime is the Cell's current mtime for the file (None if absent)."""
        st = self.get(path)
        if st is None:
            return (f"File {path!r} has not been read yet. Read it first before "
                    f"writing to it (read-before-write).")
        if current_mtime is not None and current_mtime != st.mtime:
            return (f"File {path!r} has been modified since you last read it. "
                    f"Read it again before editing to avoid clobbering changes.")
        return None
