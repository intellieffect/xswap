"""One module per group of subcommands.

Each module contributes two things and nothing else: `add_*_parser(sub)`, which
registers a subparser, and `run_*(args, manager)`, which is the branch body
`xswap.manager.main` used to hold, moved across unchanged. `xswap.cli` decides
the registration order (argparse prints the subcommands in it) and the dispatch.
"""
