"""The `xswap doctor` framework: statuses, the result shape, and how it prints.

What is actually checked belongs to a provider -- only it knows what a healthy
install of its platform looks like -- and arrives here through
`Provider.doctor_checks`. This module owns the three statuses, the one-line
result shape every check produces, and the two renderers (`format_report` and
`print_report`), so a second platform's checks print and exit exactly like the
first one's without being edited.

Results travel as plain dicts (`{"name", "status", "detail"}`) because that is
what `usage-cache`-era code, the JSON payload builder and the suite all read;
`xswap.core.types.Check` is the same thing as a frozen dataclass, and
`Check.as_dict()` converts.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

from xswap.core.types import CheckPayload

if TYPE_CHECKING:
    from xswap.manager import Manager

OK, WARN, FAIL = "OK", "WARN", "FAIL"


def check(name: str, status: str, detail: str = "") -> CheckPayload:
    """One result row. The unit every provider check returns."""
    return {"name": name, "status": status, "detail": detail}


def collect(manager: Manager) -> list[CheckPayload]:
    """Every check MANAGER's provider offers, as dict rows, in the provider's order."""
    return [cast(CheckPayload, result) if isinstance(result, dict) else result.as_dict()
            for result in manager.provider.doctor_checks(manager)]


def format_report(results: list[CheckPayload]) -> str:
    name_width = max((len(r["name"]) for r in results), default=0)
    lines = [f"{r['status']:<5} {r['name']:<{name_width}}  {r['detail']}".rstrip() for r in results]
    return "\n".join(lines)


def print_report(results: list[CheckPayload], json_output: bool = False) -> int:
    if json_output:
        from xswap.core.json_output import doctor_payload
        print(json.dumps(doctor_payload(results), ensure_ascii=False, indent=2))
    else:
        print(format_report(results))
    return 1 if any(r["status"] == FAIL for r in results) else 0
