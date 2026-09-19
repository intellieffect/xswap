# Type stub for the compatibility alias `xswap.runs` (INT-5612).
# The .py file replaces this module object with `xswap.providers.codex.runs` at runtime
# (sys.modules[__name__] = ...); pyright cannot follow that dynamic swap, so this
# stub tells it to type-check `from xswap.runs import X` as `from xswap.providers.codex.runs import X`.
from xswap.providers.codex.runs import *
