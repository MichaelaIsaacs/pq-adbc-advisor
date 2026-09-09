"""pytest bootstrap for the src/ layout.

This file exists so `python -m pytest tests/` works out of the box against
the source tree without needing `pip install -e .`. Also resets v0.3.8
security state between tests so hard-opt-out doesn't leak across cases.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_v038_hard_opt_out():
    """v0.3.8 introduced a session-scoped hard opt-out flag that persists
    across test cases within a single process. Reset it before and after
    every test so tests don't affect each other."""
    try:
        from pq_adbc_advisor import state as _state
        _state._reset_hard_opt_out_for_tests()
    except Exception:
        pass
    yield
    try:
        _state._reset_hard_opt_out_for_tests()
    except Exception:
        pass
