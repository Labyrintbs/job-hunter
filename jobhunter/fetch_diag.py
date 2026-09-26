"""Fetch-phase drop/degradation tracking -- makes otherwise-silent skips
(non-France location miss, pagination cap hit, malformed record, a per-item
error that doesn't stop the whole fetch) visible after the fact, without
persisting every dropped job. Ambient: track() is a no-op unless a
run_tracking() block is active, so it's safe to call from anywhere with no
signature changes to any fetch function."""
from __future__ import annotations

from collections import defaultdict

_SAMPLE_CAP = 3
_current: "_Tracker | None" = None


class _Tracker:
    def __init__(self) -> None:
        self.counts: dict[tuple[str, str, str], int] = defaultdict(int)
        self.samples: dict[tuple[str, str, str], list[str]] = defaultdict(list)

    def flush(self, conn) -> None:
        from . import db
        rows = [(s, c, r, n, self.samples[(s, c, r)])
                for (s, c, r), n in self.counts.items()]
        if rows:
            db.record_fetch_drops(conn, rows)


def track(source: str, reason: str, detail: str = "", company: str = "") -> None:
    if _current is None:
        return
    key = (source, company, reason)
    _current.counts[key] += 1
    if len(_current.samples[key]) < _SAMPLE_CAP:
        _current.samples[key].append(detail)


class run_tracking:
    """`with fetch_diag.run_tracking() as t: ...; t.flush(conn)`"""

    def __enter__(self) -> _Tracker:
        global _current
        self._prev = _current
        _current = _Tracker()
        return _current

    def __exit__(self, *exc) -> None:
        global _current
        _current = self._prev
