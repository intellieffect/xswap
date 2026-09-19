# Type stub for the compatibility alias `xswap.usage` (INT-5612).
# The .py file replaces this module object with `xswap.providers.codex.usage` at runtime
# (sys.modules[__name__] = ...); pyright cannot follow that dynamic swap, so this
# stub tells it to type-check `from xswap.usage import X` as `from xswap.providers.codex.usage import X`.
from xswap.providers.codex.usage import *
