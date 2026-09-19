# Type stub for the compatibility alias `xswap.identity` (INT-5612).
# The .py file replaces this module object with `xswap.providers.codex.identity` at runtime
# (sys.modules[__name__] = ...); pyright cannot follow that dynamic swap, so this
# stub tells it to type-check `from xswap.identity import X` as `from xswap.providers.codex.identity import X`.
from xswap.providers.codex.identity import *
