"""PaperWhirl — figure-grounded paper review."""

import time
from contextlib import contextmanager


@contextmanager
def stage(name: str):
    """Print elapsed wall time for a named pipeline stage.

    Used by E1 (Stage 3) to instrument extraction without changing
    behavior. The print line is shaped to match the existing
    `[tag] ...` log convention so the backend monitor filter keeps
    catching it.
    """
    t0 = time.monotonic()
    try:
        yield
    finally:
        print(f"  [timing] {name}: {time.monotonic() - t0:.2f}s")
