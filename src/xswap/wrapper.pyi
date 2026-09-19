# Type stub for the compatibility alias `xswap.wrapper` (INT-5612).
# The .py file replaces this module object with `xswap.providers.codex.wrapper` at runtime
# (sys.modules[__name__] = ...); pyright cannot follow that dynamic swap, so this
# stub tells it to type-check `from xswap.wrapper import X` as `from xswap.providers.codex.wrapper import X`.
from xswap.providers.codex.wrapper import *
