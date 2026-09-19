# Type stub for the compatibility alias `xswap.doctor` (INT-5612).
# The .py file replaces this module object with `xswap.providers.codex.doctor` at runtime
# (sys.modules[__name__] = ...); pyright cannot follow that dynamic swap, so this
# stub tells it to type-check `from xswap.doctor import X` as `from xswap.providers.codex.doctor import X`.
from xswap.providers.codex.doctor import *
