# Exit codes (INT-5611)

Numbers below are a contract with wrappers, launchd jobs, and scripts that check
`$?`; they are documented, not renumbered, and defined once as `xswap.exit_codes.ExitCode`.

## `xswap auto-tick`

| Code | `ExitCode` member | Meaning |
|---|---|---|
| `0` | `OK` | Switched (or, with `--dry-run`, would have switched). |
| `1` | `ERROR` | An error prevented the check from completing (uncaught exception at the CLI layer). |
| `2` | `NO_ACTION` | The selected account is still above the weekly reserve, or its quota is unknown, so nothing changed. |
| `3` | `BLOCKED` | The tick could not act: no account selected, automatic switching disabled, or no pool account is currently above the reserve. |

`--json`'s `exitCode` field always equals the process's actual exit status for that run.

## `xswap list --warn PCT`

| Code | Meaning |
|---|---|
| `0` | No account's codex window is below `PCT` remaining. |
| `3` | At least one warning was printed to stderr (one line per account/window below `PCT`). |

## Other commands

Every other xswap subcommand follows the plain convention: `0` on success, `1` on any
raised `SwapError`/unexpected exception (caught at the CLI entry point in
`src/xswap/cli/__init__.py`), with the error message printed to stderr. Nothing else
currently returns `2` or `3`.

Outside this contract: argparse itself exits with `2` on an unknown subcommand or bad usage (before `main()` runs), and a `KeyboardInterrupt` exits with `130`.
