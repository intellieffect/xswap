# Type stub for the compatibility alias `xswap.credentials` (INT-5612).
# The .py file replaces this module object with `xswap.providers.codex.credentials` at runtime
# (sys.modules[__name__] = ...); pyright cannot follow that dynamic swap, so this
# stub tells it to type-check `from xswap.credentials import X` as `from xswap.providers.codex.credentials import X`.
from xswap.providers.codex.credentials import *
