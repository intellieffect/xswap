"""Platform-neutral xswap: registry, policy, presentation, diagnostics framework.

Nothing in here may know about a particular AI coding platform. Core is the
bottom of the import graph (`core <- providers <- manager <- cli`); it never
imports `xswap.providers`, `xswap.manager` or `xswap.cli` at import time, and
what it needs from a platform it receives -- a `Provider`, a `QuotaShape`, a
callable -- from the caller. `tests/test_import_graph.py` enforces both that
direction and the absence of any platform's vocabulary in this package.
"""
