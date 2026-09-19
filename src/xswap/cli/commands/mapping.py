"""`map` and `unmap`: the directory -> account mappings."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from xswap.manager import Manager


def add_map_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    mp = sub.add_parser("map", help="Map a directory to a default account, or list existing mappings")
    mp.add_argument("name", nargs="?")
    mp.add_argument("path", nargs="?", type=Path)
    return mp


def add_unmap_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    um = sub.add_parser("unmap", help="Remove a directory's account mapping")
    um.add_argument("path", nargs="?", type=Path)
    return um


def run_map(args: argparse.Namespace, manager: Manager) -> None:
    if args.name is None:
        mappings = manager.list_mappings()
        if not mappings:
            print("No directory mappings.")
        else:
            for path, mapped_name in sorted(mappings.items()):
                print(f"{path} → {mapped_name}")
    else:
        path, name = manager.map_dir(args.name, args.path)
        print(f"Mapped {path} to {name}.")


def run_unmap(args: argparse.Namespace, manager: Manager) -> None:
    path = manager.unmap_dir(args.path)
    print(f"Unmapped {path}.")
