"""Keep the real Codex release outside xswap's own state directory (INT-5186, item 2).

Codex's standalone installer (openai/codex scripts/install/install.sh) derives every
path from CODEX_HOME:

    BIN_DIR="${CODEX_INSTALL_DIR:-$HOME/.local/bin}"
    CODEX_HOME_DIR="${CODEX_HOME:-$HOME/.codex}"
    STANDALONE_ROOT="$CODEX_HOME_DIR/packages/standalone"
    RELEASES_DIR="$STANDALONE_ROOT/releases"        # releases/<version>-<triple>/
    CURRENT_LINK="$STANDALONE_ROOT/current"          # -> absolute release directory
    update_visible_command: "$BIN_DIR/codex" -> "$CURRENT_LINK/bin/codex"

A bridged session runs Codex with CODEX_HOME set to xswap's runtime home, so ctrl+u
or `codex upgrade` inside it installs under auto/cli-codex/packages, and 0.7.8's
reconnect faithfully recorded that path as realCodex (Mini, 2026-09-13: both 0.153.4
and 0.154.0 lived only there and ~/.codex/packages did not exist). Purging or
resetting auto/ would take plain `codex` down with it.

Prevention: every home xswap owns and hands to Codex (auto/cli-codex, auto/codex,
profiles/<name>/codex) gets `packages` as a symlink to the reference home's
`packages`, so the installer writes releases where it would without xswap, and a
recorded path through that link is canonicalised to the reference home.
Repair: `xswap relocate-codex` moves releases already inside such a home to the
reference home, re-points `current`, replaces the directory with the link, rewrites
auto.json, and re-points a restored `codex` link. Nothing here deletes a release,
overwrites an existing one, edits shell configuration, or touches a Codex binary.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import uuid

RUNTIME_HOME_NAMES = ('cli-codex', 'codex')  # auto CLI runtime, auto desktop runtime
STANDALONE_ENTRIES = {'current', 'install.lock', 'releases'}  # what install.sh leaves after a completed install


def _resolve(path):
    try:
        return Path(path).resolve()
    except (OSError, RuntimeError):
        return Path(path)


def _accounts(manager, accounts):
    if accounts is not None:
        return accounts
    from codex_swap import SwapError
    try:
        return manager.read()['accounts']
    except SwapError:
        return {}


def codex_homes_inside_root(manager, accounts=None):
    """Homes xswap owns and passes to Codex as CODEX_HOME: the auto runtime homes and
    every managed profile home. Registered external homes are the user's own and are
    never listed here."""
    homes = [manager.root / 'auto' / name for name in RUNTIME_HOME_NAMES]
    for value in _accounts(manager, accounts).values():
        if not isinstance(value, dict) or not value.get('managed') or not isinstance(value.get('home'), str):
            continue
        home = Path(value['home'])
        if home.is_absolute() and home.is_relative_to(manager.root) and home not in homes:
            homes.append(home)
    return homes


def reference_home(manager, accounts=None):
    """Where the real Codex release belongs: a registered home outside xswap's root
    whose packages/standalone already holds realCodex, else the source home
    (CODEX_HOME or ~/.codex). None when neither is a directory outside the root."""
    from xswap_cli import read_settings
    from xswap_live import LiveError
    accounts = _accounts(manager, accounts)
    try:
        real = (read_settings(manager).get('wrapper') or {}).get('realCodex')
    except LiveError:
        real = None
    resolved_real = _resolve(real) if isinstance(real, str) and real else None
    candidates = [Path(value['home']) for value in accounts.values()
                  if isinstance(value, dict) and isinstance(value.get('home'), str)]
    for home in candidates:
        resolved = _resolve(home)
        if not resolved.is_dir() or resolved.is_relative_to(manager.root):
            continue
        if resolved_real is not None and resolved_real.is_relative_to(resolved / 'packages' / 'standalone'):
            return resolved
    source = _resolve(manager.source)
    if source.is_dir() and not source.is_relative_to(manager.root):
        return source
    return None


def inside_root(manager, path):
    """True when a recorded executable path lies inside xswap's state directory,
    literally or once symlinks are resolved. A literal path under the root breaks
    when auto/ is purged even if it currently resolves elsewhere; a resolved one
    breaks either way."""
    if not isinstance(path, str) or not path:
        return False
    literal = Path(path).expanduser()
    if not literal.is_absolute():
        return False
    return literal.is_relative_to(manager.root) or _resolve(literal).is_relative_to(manager.root)


def canonical_codex_path(manager, path, accounts=None):
    """Rewrite a path that passes through an owned home's `packages` link so that it
    no longer traverses xswap's root: <home>/packages/X -> <link target>/X.

    The `current` indirection is kept on purpose (this is not a full resolve): a later
    Codex update only re-points `current`, and the recorded path stays valid. Any
    other path is returned unchanged, including one into a real packages directory;
    doctor reports that and `xswap relocate-codex` repairs it.
    """
    from xswap_cli import link_target_path
    literal = Path(path)
    for home in codex_homes_inside_root(manager, accounts):
        packages = home / 'packages'
        if not literal.is_relative_to(packages):
            continue
        try:
            if not packages.is_symlink():
                return str(literal)
            target = Path(link_target_path(packages, os.readlink(packages)))
        except OSError:
            return str(literal)
        return str(target / literal.relative_to(packages))
    return str(literal)


def link_packages(manager, home, accounts=None):
    """Make <home>/packages a link to the reference home's `packages` when the entry
    does not exist yet. Returns a short label and never raises: a launch must not
    fail over this. An existing real directory is left for `xswap relocate-codex`
    (doctor's `packages link` row names it); a registered external home is never
    touched."""
    home = Path(home)
    if not home.is_relative_to(manager.root):
        return 'outside-root'
    link = home / 'packages'
    try:
        if link.is_symlink():
            # install.sh runs `mkdir -p "$STANDALONE_ROOT"`, which fails outright on a
            # broken link. Never report that as a working prevention: doctor names it.
            return 'linked' if link.exists() else 'dangling'
        if link.exists():
            return 'directory'
        reference = reference_home(manager, accounts)
        if reference is None:
            return 'no-reference-home'
        target = reference / 'packages'
        target.mkdir(mode=0o755, exist_ok=True)
        if target.is_symlink() or target.lstat().st_uid != os.getuid():
            return 'unsafe-reference'
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        return 'failed'
    return 'linked'


def _current_name(standalone):
    from xswap_cli import link_target_path
    current = standalone / 'current'
    if not current.is_symlink():
        return None
    return Path(link_target_path(current, os.readlink(current))).name


def _standalone_releases(standalone):
    """Release directories under <standalone>/releases; refuse any layout other than
    what a completed install.sh run leaves behind, so nothing unexpected is moved."""
    from codex_swap import SwapError
    if standalone.is_symlink() or not standalone.is_dir():
        raise SwapError(f'{standalone} is not a directory; move it aside, then retry. Nothing was changed.')
    if (standalone / 'install.lock.d').exists():
        raise SwapError(f'{standalone / "install.lock.d"} exists: a Codex install may be in progress. Retry later. Nothing was changed.')
    unknown = sorted(entry.name for entry in standalone.iterdir() if entry.name not in STANDALONE_ENTRIES)
    if unknown:
        raise SwapError(f'{standalone} holds entries the installer would not leave ({", ".join(unknown)}); move them aside, then retry. Nothing was changed.')
    releases = standalone / 'releases'
    if not releases.is_dir() or releases.is_symlink():
        return []
    found = []
    for entry in sorted(releases.iterdir()):
        if entry.name.startswith('.') or entry.is_symlink() or not entry.is_dir():
            raise SwapError(f'{entry} is not a release directory (an interrupted install leaves .staging.* behind); move it aside, then retry. Nothing was changed.')
        found.append(entry)
    return found


def _rewrite(manager, value, reference, accounts):
    literal = Path(value)
    for home in codex_homes_inside_root(manager, accounts):
        packages = home / 'packages'
        if not literal.is_relative_to(packages):
            continue
        if packages.is_symlink():
            return canonical_codex_path(manager, value, accounts)
        if reference is None:
            return value
        return str(reference / 'packages' / literal.relative_to(packages))
    return value


def _record_rewrites(manager, record, reference, accounts):
    """{key: new value} for the paths one wrapper record spells through xswap's root."""
    rewrites = {}
    for key in ('realCodex', 'originalTarget'):
        value = record.get(key)
        if isinstance(value, str) and value:
            rewritten = _rewrite(manager, value, reference, accounts)
            if rewritten != value:
                rewrites[key] = rewritten
    return rewrites


def plan_relocation(manager):
    """Everything relocate() would do, computed without changing anything. Raises
    SwapError for any state it refuses to touch."""
    from codex_swap import SwapError
    from xswap_cli import read_settings
    accounts = manager.read()['accounts']
    settings = read_settings(manager)
    wrapper = settings.get('wrapper') or {}
    real = wrapper.get('realCodex') if isinstance(wrapper.get('realCodex'), str) else None
    reference = reference_home(manager, accounts)
    homes = []
    for home in codex_homes_inside_root(manager, accounts):
        packages = home / 'packages'
        if packages.is_symlink() or not packages.is_dir():
            continue
        others = sorted(entry.name for entry in packages.iterdir() if entry.name != 'standalone')
        if others:
            raise SwapError(f'{packages} holds entries other than standalone ({", ".join(others)}); move them aside, then retry. Nothing was changed.')
        standalone = packages / 'standalone'
        releases = _standalone_releases(standalone) if standalone.exists() or standalone.is_symlink() else []
        homes.append({'home': home, 'packages': packages, 'releases': releases,
                      'current': _current_name(standalone) if standalone.is_dir() else None,
                      'holdsReal': bool(real) and Path(real).is_relative_to(packages)})
    rewrites = _record_rewrites(manager, wrapper, reference, accounts)
    # 0.8.0 can hold several wrapped entries (`wrappers`), and `auto-disable` restores every
    # one of them. A secondary record still spelling a release through xswap's own root would
    # put that path back on PATH with no record left and nothing to repair it, so every
    # record is rewritten here, not only the entry plain `codex` currently runs through.
    wrapper_rewrites = {}
    for record in settings.get('wrappers') or []:
        if not isinstance(record, dict) or not isinstance(record.get('path'), str) or not record['path']:
            continue
        changed = _record_rewrites(manager, record, reference, accounts)
        if changed:
            wrapper_rewrites[record['path']] = changed
    plan = {'reference': reference, 'homes': homes, 'rewrites': rewrites,
            'wrapperRewrites': wrapper_rewrites, 'moves': [], 'current': None, 'wrapper': wrapper}
    if not homes:
        return plan
    if reference is None:
        raise SwapError(f'no reference Codex home outside {manager.root}: CODEX_HOME ({manager.source}) must be an existing directory outside the xswap state directory. Nothing was changed.')
    ref_releases = reference / 'packages' / 'standalone' / 'releases'
    claimed = {}
    for entry in homes:
        for release in entry['releases']:
            destination = ref_releases / release.name
            if destination.exists() or destination.is_symlink():
                raise SwapError(f'refusing to move {release}: {destination} already exists. Remove or rename one copy, then retry. Nothing was changed.')
            # The check above asks the filesystem, which cannot see the plan being built. Two
            # owned homes updated to the same version (ctrl+u in one, `xswap run NAME` in the
            # other) claim the same destination, and the second os.rename then fails with
            # ENOTEMPTY once the first release has already moved -- a half-migration the user
            # can only finish by hand. Refuse here, while nothing has been changed.
            if destination in claimed:
                raise SwapError(f'refusing to move {release}: {claimed[destination]} is the same release and would '
                                f'move to the same {destination}. Remove or rename one copy, then retry. Nothing was changed.')
            claimed[destination] = release
            plan['moves'].append((release, destination))
    moved = {release.name for release, _ in plan['moves']}

    def usable(name):
        return name is not None and (name in moved or (ref_releases / name).is_dir())

    # install.sh spells the recorded realCodex through `<standalone>/current`, and _rewrite
    # re-spells it through the reference home, so a `current` this plan cannot place there --
    # a real directory holding a copy (a restore that dereferenced links: rsync -L, an
    # unzipped backup), a missing link, one pointing at a release that is not here -- left
    # the record on a reference path nothing creates. relocate then exited 0 and `xswap
    # doctor` reported `real codex ... is missing; plain codex cannot start`, one command
    # after the one doctor had asked for. Refuse while nothing has been changed.
    for entry in homes:
        current = entry['packages'] / 'standalone' / 'current'
        if entry['holdsReal'] and Path(real).is_relative_to(current) and not usable(entry['current']):
            raise SwapError(f'{current} does not point at a release that can be moved to {reference / "packages"}, '
                            f'but auto.json records the real Codex through it ({real}); point it at a release under '
                            f'{current.parent / "releases"} (ln -sfn releases/<name> current), then retry. '
                            'Nothing was changed.')

    # The release the recorded realCodex runs from decides `current`: that is the one
    # plain `codex` is about to execute. Otherwise a reference `current` that already
    # exists is the user's own choice and is left alone.
    holder = next((entry for entry in homes if entry['holdsReal'] and usable(entry['current'])), None)
    if holder is not None:
        plan['current'] = holder['current']
    elif not (ref_releases.parent / 'current').is_symlink():
        plan['current'] = next((entry['current'] for entry in homes if usable(entry['current'])), None)
    return plan


def _is_skeleton(aside):
    """True when only what install.sh itself leaves remains: `current`, `install.lock`,
    and an empty releases/ -- the only leftovers relocate() produces."""
    if [entry.name for entry in aside.iterdir()] not in ([], ['standalone']):
        return False
    standalone = aside / 'standalone'
    if not standalone.exists():
        return True
    for entry in standalone.iterdir():
        if entry.name == 'current' and entry.is_symlink():
            continue
        if entry.name == 'install.lock' and entry.is_file() and not entry.is_symlink():
            continue
        if entry.name == 'releases' and entry.is_dir() and not entry.is_symlink() and not any(entry.iterdir()):
            continue
        return False
    return True


def relocate(manager, dry=False):
    """Move Codex releases out of xswap-owned homes into the reference home and link
    those homes to it. Prints one line per change (or per planned change with
    `dry`) and returns the plan."""
    from codex_swap import SwapError, atomic_json
    from xswap_cli import read_settings, status_data, swap_symlink
    kept, running, lines = [], 0, []
    with manager.locked():
        plan = plan_relocation(manager)
        if not plan['homes'] and not plan['rewrites'] and not plan['wrapperRewrites']:
            real = plan['wrapper'].get('realCodex') or 'none recorded'
            print(f'Nothing to relocate: the real Codex ({real}) is outside {manager.root} and no xswap-owned home holds a packages directory.')
            return plan
        reference = plan['reference']
        ref_standalone = reference / 'packages' / 'standalone' if reference is not None else None
        verb = 'move' if dry else 'moved'
        if reference is not None:
            lines.append(f'Reference Codex home: {reference}')
        for entry in plan['homes']:
            names = ', '.join(release.name for release in entry['releases']) or 'none'
            lines.append(f"{entry['packages']}: {verb} {len(entry['releases'])} release(s) to {ref_standalone / 'releases'}: {names}")
            lines.append(f"{entry['packages']} -> {reference / 'packages'}")
        if plan['current']:
            lines.append(f"{ref_standalone / 'current'} -> {ref_standalone / 'releases' / plan['current']}")
        for key, value in plan['rewrites'].items():
            lines.append(f'auto.json {key} -> {value}')
        for record_path, values in plan['wrapperRewrites'].items():
            for key, value in values.items():
                lines.append(f'auto.json wrappers {record_path} {key} -> {value}')
        if dry:
            print('\n'.join(lines + ['Dry run: nothing changed.']))
            return plan
        running = sum(1 for session in status_data(manager, cleanup=False)['sessions'] if session.get('running'))
        if plan['homes']:
            (ref_standalone / 'releases').mkdir(parents=True, exist_ok=True)
        for source, destination in plan['moves']:
            try:
                os.rename(source, destination)
            except OSError as exc:
                raise SwapError(f'cannot move {source} to {destination} ({exc.strerror or exc}); releases already moved stay under {ref_standalone / "releases"} and nothing was deleted.') from None
        if plan['current']:
            swap_symlink(ref_standalone / 'current', str(ref_standalone / 'releases' / plan['current']))
        for entry in plan['homes']:
            aside = entry['home'] / ('.packages-relocated-' + uuid.uuid4().hex)
            os.rename(entry['packages'], aside)
            swap_symlink(entry['packages'], str(reference / 'packages'))
            if _is_skeleton(aside):
                shutil.rmtree(aside)
            else:
                kept.append(aside)
        if plan['rewrites'] or plan['wrapperRewrites']:
            settings = read_settings(manager)
            wrapper = settings.get('wrapper') or {}
            updates = [(wrapper, plan['rewrites'])]
            for record in settings.get('wrappers') or []:
                if isinstance(record, dict) and plan['wrapperRewrites'].get(record.get('path')):
                    updates.append((record, plan['wrapperRewrites'][record['path']]))
            repoints = []
            for record, values in updates:
                if not values:
                    continue
                old_original = record.get('originalTarget')
                record.update(values)
                new_original = values.get('originalTarget')
                path = Path(record.get('path') or '')
                # auto-disable restores originalTarget literally, so a disconnected entry
                # still points into the moved-out directory until it is re-pointed here.
                # A relative recorded path (0.7.8's `shutil.which` result) would resolve against
                # the working directory and re-point a `codex` inside an unrelated repository;
                # the record's own fields are still rewritten, the link is left to a human.
                if (new_original and record.get('path') and os.path.isabs(record['path'])
                        and path.is_symlink() and os.readlink(path) == old_original):
                    repoints.append((path, new_original))
            settings['wrapper'] = wrapper
            atomic_json(manager.root / 'auto.json', settings)
            for path, new_original in repoints:
                swap_symlink(path, new_original)
                lines.append(f'{path} -> {new_original}')
    for aside in kept:
        lines.append(f"Kept {aside}: it held more than the installer's lock and links; review and remove it by hand.")
    if running:
        lines.append(f'{running} running bridged session(s) keep their loaded binary; new launches use the relocated release.')
    print('\n'.join(lines))
    return plan
