"""Bounded background enrichment for non-critical display labels."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time

_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="display-labels")
_lock = threading.Lock()
_names = {}
_pending = set()
_attempted = {}


def symbol_names(context, symbols):
    symbols = tuple(sorted(set(s for s in symbols if s)))
    with _lock:
        cached = {s: _names.get(s, "") for s in symbols}
        missing = tuple(s for s in symbols if s not in _names and time.monotonic() - _attempted.get(s, 0) > 60)
        if missing and not _pending:
            _pending.update(missing)
            for symbol in missing:
                _attempted[symbol] = time.monotonic()
            def refresh():
                try:
                    rows = context.hub.get_instruments(list(missing))
                    with _lock:
                        for row in rows:
                            _names[row.symbol] = getattr(row, "name", "") or ""
                finally:
                    with _lock:
                        _pending.difference_update(missing)
            _pool.submit(refresh)
    return cached
