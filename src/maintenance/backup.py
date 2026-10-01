import os
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from src.ports import DecisionStore

_BACKUP_NAME_RE = re.compile(r"^assistant-\d{4}-\d{2}-\d{2}\.db$")
_PARTIAL_SUFFIX = ".partial"


class BackupRotation:
    """One dated copy of the store per day, keeping only the most recent ones."""

    def __init__(
        self,
        store: DecisionStore,
        directory: str,
        keep: int,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if keep < 1:
            raise ValueError(f"keep must be at least 1, got {keep}")
        self._store = store
        self._directory = Path(directory)
        self._keep = keep
        self._clock = clock

    def run(self) -> Path:
        final = self._directory / f"assistant-{self._clock().date().isoformat()}.db"
        partial = final.with_name(final.name + _PARTIAL_SUFFIX)
        partial.unlink(missing_ok=True)
        try:
            self._store.backup(str(partial))
        except Exception:
            partial.unlink(missing_ok=True)
            raise
        # Replaced only once verified, so a failed run never destroys the previous good copy.
        os.replace(partial, final)
        self._rotate()
        return final

    def _rotate(self) -> None:
        # Only names this class writes are touched: the directory may hold other files.
        backups = sorted(
            path for path in self._directory.iterdir() if _BACKUP_NAME_RE.match(path.name)
        )
        for expired in backups[: -self._keep]:
            expired.unlink()
