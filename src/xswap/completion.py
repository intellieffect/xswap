"""Zsh/bash completion script generator for xswap.

`generate(parser, shell)` walks the argparse tree built by
`codex_swap.parser()` (stdlib argparse internals only, no third-party
dependencies) and renders a completion script for `shell` ("zsh" or
"bash"). The generated scripts call `xswap list --offline --json` at
*completion time* (not at generation time) to read live account names
from the local registry; `--offline` means that call never touches the
network. Any failure there (xswap not on PATH yet, empty registry, a
parse hiccup) is swallowed so completion never errors out — it just
offers no account-name candidates that keystroke.

Account-name convention
------------------------
argparse has no built-in way to say "this position expects an existing
account name" versus "an unrelated string or path". Rather than touch
`codex_swap.parser()` to add one (out of scope for a completion script
and it would risk changing real `--help` output), this module infers
it from the argparse `dest` alone: a positional or optional argument
whose `dest` is exactly "name" or "account" is treated as an existing-
account-name completion point, with one exception — on the `register`
and `add` subcommands the "name" argument introduces a brand-new label
that is not yet in the registry, so there is nothing to complete and
it is excluded. See `_is_account_hook` below. This is a plain `dest`
allow/deny-list, so it needs no changes to the CLI's own argument
definitions and cannot change `xswap`'s existing behavior.
"""

SUPPORTED_SHELLS = ("zsh", "bash")

# Subcommands whose "name" positional creates a brand-new account label
# rather than referring to one already in the registry (see module
# docstring "Account-name convention").
_NEW_NAME_SUBCOMMANDS = frozenset({"register", "add"})
_ACCOUNT_NAME_DESTS = frozenset({"name", "account"})


def generate(parser, shell):
    """Render a completion script for `shell` from an xswap argparse `parser`."""
    if shell not in SUPPORTED_SHELLS:
        raise ValueError(f"Unsupported shell {shell!r}; choose one of {', '.join(SUPPORTED_SHELLS)}.")
    sub_action = parser._subparsers._group_actions[0]
    groups = _canonical_groups(sub_action)
    if shell == "zsh":
        return _generate_zsh(sub_action, groups)
    return _generate_bash(groups)


def _canonical_groups(sub_action):
    """[(names, subparser), ...] with alias names grouped under their subparser.

    `sub_action.choices` maps every name *and* every alias to the same
    subparser object (e.g. both "use" and "switch" point at one
    parser); grouping by identity keeps aliases next to their
    canonical command instead of emitting a duplicate definition.
    """
    groups, index_by_id = [], {}
    for name, subparser in sub_action.choices.items():
        key = id(subparser)
        if key in index_by_id:
            groups[index_by_id[key]][0].append(name)
        else:
            index_by_id[key] = len(groups)
            groups.append(([name], subparser))
    return groups


def _is_account_hook(dest, subcommand_name):
    return dest in _ACCOUNT_NAME_DESTS and subcommand_name not in _NEW_NAME_SUBCOMMANDS


def _positionals_and_options(subparser):
    positionals, options = [], []
    for action in subparser._actions:
        (options if action.option_strings else positionals).append(action)
    return positionals, options


def _is_path_type(action):
    return getattr(action.type, "__name__", "") == "Path"


def _sq(text):
    """Single-quote `text` for safe embedding in a generated shell script."""
    return "'" + text.replace("'", "'\\''") + "'"


def _bracket_escape(text):
    """Escape characters that would break a zsh `_arguments` `[...]` span."""
    return text.replace("\\", "\\\\").replace("]", "\\]").replace("[", "\\[")


_ACCOUNT_NAMES_FN = "_xswap_account_names"

# Both shells extract account names the same way: `xswap list --offline
# --json` never touches the network (that is the whole point of
# --offline) and prints rows shaped like {"name": ..., "buckets": [],
# ...} in offline mode, so a plain "name" field grep is unambiguous —
# there are no nested objects with their own "name" key to collide
# with while --offline is in effect.
_ACCOUNT_NAMES_PIPELINE = (
    'xswap list --offline --json 2>/dev/null | '
    '''sed -n 's/.*"name": *"\\([^"]*\\)".*/\\1/p\''''
)


def _generate_zsh(sub_action, groups):
    choice_help = {a.dest: (a.help or "") for a in sub_action._choices_actions}
    lines = [
        "#compdef xswap",
        '# Install: xswap completion zsh > "${fpath[1]}/_xswap"  (new shells pick it up)',
        '# or add:  eval "$(xswap completion zsh)"  to your .zshrc',
        "",
        f"{_ACCOUNT_NAMES_FN}() {{",
        "  local -a names",
        f'  names=(${{(f)"$({_ACCOUNT_NAMES_PIPELINE})"}})',
        "  (( $#names )) && _describe -t xswap-accounts 'account' names",
        "}",
        "",
    ]

    fn_by_canon = {}
    for names, subparser in groups:
        canon = names[0]
        fn = "_xswap_args_" + canon.replace("-", "_")
        fn_by_canon[canon] = fn
        positionals, options = _positionals_and_options(subparser)
        specs = []
        for index, action in enumerate(positionals, start=1):
            if _is_account_hook(action.dest, canon):
                value_action = _ACCOUNT_NAMES_FN
            elif action.choices:
                value_action = "(" + " ".join(str(c) for c in action.choices) + ")"
            elif _is_path_type(action):
                value_action = "_files"
            else:
                value_action = ""
            specs.append(_sq(f"{index}:{action.dest or 'value'}:{value_action}"))
        for action in options:
            help_text = _bracket_escape(action.help or "")
            opt = action.option_strings[-1]
            if action.nargs == 0:
                specs.append(_sq(f"{opt}[{help_text}]"))
                continue
            if _is_account_hook(action.dest, canon):
                value_action = _ACCOUNT_NAMES_FN
            elif _is_path_type(action):
                value_action = "_files"
            else:
                value_action = ""
            specs.append(_sq(f"{opt}=[{help_text}]:{action.dest}:{value_action}"))
        lines.append(f"{fn}() {{")
        if specs:
            lines.append("  _arguments \\")
            for i, spec in enumerate(specs):
                sep = " \\" if i < len(specs) - 1 else ""
                lines.append(f"    {spec}{sep}")
        else:
            lines.append("  _arguments")
        lines.append("}")
        lines.append("")

    lines.append("_xswap() {")
    lines.append('  local curcontext="$curcontext" state line')
    lines.append("  local -a subcommands")
    lines.append("  subcommands=(")
    for names, _subparser in groups:
        canon = names[0]
        help_text = choice_help.get(canon, "")
        for name in names:
            lines.append(f"    {_sq(f'{name}:{help_text}')}")
    lines.append("  )")
    lines.append("  _arguments -C \\")
    lines.append("    '--version[show program version]' \\")
    lines.append("    '1: :->command' \\")
    lines.append("    '*:: :->args'")
    lines.append('  case "$state" in')
    lines.append("    command)")
    lines.append("      _describe -t xswap-commands 'xswap command' subcommands")
    lines.append("      ;;")
    lines.append("    args)")
    lines.append('      case "${words[1]}" in')
    for names, _subparser in groups:
        canon = names[0]
        pattern = "|".join(names)
        lines.append(f"        {pattern})")
        lines.append(f"          {fn_by_canon[canon]}")
        lines.append("          ;;")
    lines.append("      esac")
    lines.append("      ;;")
    lines.append("  esac")
    lines.append("}")
    lines.append("")
    lines.append("compdef _xswap xswap")
    return "\n".join(lines) + "\n"


def _generate_bash(groups):
    all_names = [name for names, _ in groups for name in names]
    lines = [
        "# xswap bash completion",
        '# Install: add  eval "$(xswap completion bash)"  to your .bashrc',
        "",
        f"{_ACCOUNT_NAMES_FN}() {{",
        f"  {_ACCOUNT_NAMES_PIPELINE}",
        "}",
        "",
        "_xswap() {",
        "  local cur prev cmd",
        '  cur="${COMP_WORDS[COMP_CWORD]}"',
        '  prev="${COMP_WORDS[COMP_CWORD-1]}"',
        '  cmd="${COMP_WORDS[1]}"',
        f'  local subcommands="{" ".join(all_names)}"',
        "  if [[ $COMP_CWORD -eq 1 ]]; then",
        '    COMPREPLY=( $(compgen -W "$subcommands" -- "$cur") )',
        "    return 0",
        "  fi",
        '  case "$cmd" in',
    ]
    for names, subparser in groups:
        canon = names[0]
        positionals, options = _positionals_and_options(subparser)
        long_opts = [action.option_strings[-1] for action in options]
        hook_option = next(
            (action.option_strings[-1] for action in options
             if action.nargs != 0 and _is_account_hook(action.dest, canon)),
            None,
        )
        first_positional = positionals[0] if positionals else None
        positional_hook = bool(first_positional and _is_account_hook(first_positional.dest, canon))
        positional_choices = first_positional.choices if (first_positional and first_positional.choices) else None

        pattern = "|".join(names)
        lines.append(f"    {pattern})")
        if hook_option:
            lines.append(f'      if [[ "$prev" == "{hook_option}" ]]; then')
            lines.append(f'        COMPREPLY=( $(compgen -W "$({_ACCOUNT_NAMES_FN})" -- "$cur") )')
            lines.append("        return 0")
            lines.append("      fi")
        if positional_hook:
            lines.append("      if [[ $COMP_CWORD -eq 2 ]]; then")
            lines.append(f'        COMPREPLY=( $(compgen -W "$({_ACCOUNT_NAMES_FN})" -- "$cur") )')
            lines.append("        return 0")
            lines.append("      fi")
        elif positional_choices:
            words = " ".join(str(choice) for choice in positional_choices)
            lines.append("      if [[ $COMP_CWORD -eq 2 ]]; then")
            lines.append(f'        COMPREPLY=( $(compgen -W "{words}" -- "$cur") )')
            lines.append("        return 0")
            lines.append("      fi")
        opts_words = " ".join(long_opts)
        lines.append(f'      COMPREPLY=( $(compgen -W "{opts_words}" -- "$cur") )')
        lines.append("      ;;")
    lines.append("    *)")
    lines.append("      COMPREPLY=()")
    lines.append("      ;;")
    lines.append("  esac")
    lines.append("}")
    lines.append("complete -F _xswap xswap")
    return "\n".join(lines) + "\n"
