"""The `codex` command itself: the symlink xswap wraps, and its drift repair.

`auto-enable --wrap-codex` points the user-owned `codex` entry at the
`xswap-codex` console script and records what it pointed at before; everything
here reads, classifies or restores that arrangement. Nothing in this module
launches Codex or talks to the bridge -- `codex_cli` sits above it and does
that -- so the direction is `settings` <- `wrapper` <- `runs` <- `codex_cli`.

Patchability: `swap_symlink` and `entry_drift` are called through `_late()`
below rather than by their local names, because the suite patches them as
`xswap.providers.codex.codex_cli.<name>`, which is where they used to live. See `_late`.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from xswap.core.fsutil import atomic_json
from xswap.core.paths import ROOT_VARIABLE, default_root, settings_path
from xswap.core.settings import read_settings
from xswap.providers.codex.live import AccountPool, LiveError, validate_threshold

if TYPE_CHECKING:
    from xswap.manager import Manager

Drift = dict[str, Any]


def _late(name: str) -> Callable[..., Any]:
    """Resolve NAME through `xswap.providers.codex.codex_cli`, which re-exports it, at call time.

    These functions used to be defined in `codex_cli`, and the test suite still
    patches them there (`patch('xswap.providers.codex.codex_cli.swap_symlink')`,
    `patch('xswap.providers.codex.codex_cli.entry_drift')`). Calling the local definition would
    make those patches replace a name nothing reads, so the internal call sites
    below go through the original module instead. The import is function-local:
    `codex_cli` imports this module at module level, and reaching back at import
    time would be a cycle.
    """
    from xswap import codex_cli
    return getattr(codex_cli, name)


def link_target_path(link: Path, target: str) -> str:
    """Absolute path of a symlink's target, resolving a relative target against the link's directory."""
    return str(link.parent / target) if not Path(target).is_absolute() else target


def swap_symlink(path: Path, target: str) -> None:
    """Atomically point `path` at `target` without a window where the entry is missing."""
    temporary = path.with_name('.xswap-codex-' + uuid.uuid4().hex)
    try:
        temporary.symlink_to(target)
        Path(temporary).replace(path)
    finally:
        temporary.unlink(missing_ok=True)


# The console script every xswap install provides; `wrapper.proxy` names one of them.
PROXY_NAME = 'xswap-codex'


def recorded_proxies(settings: dict[str, Any]) -> list[str]:
    """The resolved path of every xswap-codex auto.json names, primary record first."""
    values = []
    for record in [settings.get('wrapper'), *(settings.get('wrappers') or [])]:
        proxy = record.get('proxy') if isinstance(record, dict) else None
        if isinstance(proxy, str) and proxy:
            with contextlib.suppress(OSError):
                values.append(os.path.realpath(proxy))
    return values


def wrapped_target(path: str | Path, settings: dict[str, Any]) -> bool:
    """True when `path` reaches an xswap-codex rather than a Codex release.

    A machine can hold more than one xswap-codex (a pipx install beside a uv one, a
    project venv on PATH), and only the recorded proxy was ever compared against. A
    second one ahead on PATH was therefore treated as a Codex release: `--wrap-codex`
    recorded it as `realCodex` and the shadow repair promoted it, after which plain
    `codex` exec'd xswap-codex, which exec'd itself -- `codex --version` never returned
    -- while doctor named an xswap-codex as the real Codex. Both a recorded proxy and
    the install name count: the second install is by definition not in the record yet.
    """
    try:
        real = os.path.realpath(path)
    except OSError:
        return False
    return real in recorded_proxies(settings) or Path(real).name == PROXY_NAME


def recorded_real_codex(settings: dict[str, Any]) -> str | None:
    """The real Codex a wrapper record names, or None when that value cannot be used.

    None when nothing is recorded and when the value is not absolute. 0.7.8 stored
    `link_target_path(...)`, which is relative exactly when the entry it wrapped was
    (RELATIVE_RECORD_REASON), and nothing migrates either. Such a value names a different
    file in every working directory: inside a repository that happens to hold it, the
    xswap-codex entry point exec'd that file with a pool account's CODEX_HOME and doctor
    reported it as the real Codex, while from every other directory the same record read
    as "missing" and the recovery it printed (reinstall Codex) could not help. Whether the
    file exists is the caller's test -- each has its own words for a recorded value that
    is gone. `settings` is auto.json, or {'wrapper': record} for a `wrappers` entry.
    """
    real = (settings.get('wrapper') or {}).get('realCodex')
    return real if isinstance(real, str) and Path(real).is_absolute() else None


# The `codex` a project installed for itself. npm writes `node_modules/.bin/codex` for a
# dependency on @openai/codex, and such a directory reaches PATH absolutely (direnv, a
# Makefile, an IDE task). The shadow repair asked only for a user-owned symlink at an
# existing executable, so one read-only-looking xswap command inside such a repository
# rewrote that link to xswap-codex and made it the primary record; afterwards every
# `xswap run`/`login`/`app`, usage read and pool build exec'd the project's own file with a
# pool account's CODEX_HOME (2026-09-13). xswap adopts no entry from a dependency tree on
# its own -- the user can still wrap one by hand, which is what `--wrap-codex` is.
DEPENDENCY_REASON = 'project-dependency'


# The one reason wrapper_drift contributes instead of entry_drift: the recorded entry's own path is
# not absolute. 0.7.8 stored `str(shutil.which('codex'))` verbatim, so a shell whose PATH held a
# relative element recorded `bin/codex`, and 0.8.0 has no migration for it. Resolving that against
# the working directory re-points -- and `auto-disable` later "restores" -- a `codex` inside
# whatever repository xswap happens to run in, while the entry it really wrapped keeps running
# xswap-codex with no record left. So it is a skip everywhere and only a human can repair it.
RELATIVE_RECORD_REASON = 'relative-record'
DRIFT_REASONS = ('ok', 'replaced', 'not-wrapped', 'missing', 'not-a-symlink', 'not-user-owned',
    'dangling-target', 'not-executable', 'auto-disabled', 'unreadable', 'proxy-missing',
    DEPENDENCY_REASON, RELATIVE_RECORD_REASON)
# The one reason the PATH walk contributes instead of entry_drift: the `codex` a relative PATH
# element resolved to. Deliberately outside DRIFT_REASONS -- entry_drift never returns it (the
# condition belongs to the PATH element, not to the entry's link state) and its fix is the
# user's PATH, not the reconnect command every other fix ends with. It is keyed into the same
# two dicts because describe_drift looks a reason up, so auto-status and the use/switch notice
# can report a relative entry with the same words, in the shape they print every other skip in.
RELATIVE_ENTRY_REASON = 'relative-path-entry'
# XSWAP_BYPASS=1 is the documented way to hand one command to the real Codex with the caller's
# own home (`XSWAP_BYPASS=1 codex exec resume ...` reaches the sessions saved before 0.8.0).
# Exported instead of prefixed -- which is what a documented prefix leads to -- it turns the
# wrapper off for every `codex` in that shell, and no surface said so: doctor's wrapper row,
# auto-status and the use/switch notice all reported a connected wrapper while plain `codex`
# ran with the caller's CODEX_HOME, account and API key (2026-09-13). Like the relative entry,
# it is a condition of the environment rather than of the entry's link state, so it stays out
# of DRIFT_REASONS (entry_drift never returns it) and only shares the two description dicts.
BYPASS_VARIABLE = 'XSWAP_BYPASS'
BYPASS_REASON = 'bypass-variable'


def bypass_set(env: dict[str, str] | None = None) -> bool:
    """True when this environment hands every `codex` straight to the real Codex."""
    return (os.environ if env is None else env).get(BYPASS_VARIABLE) == '1'


# A global install writes its own `node_modules`: npm and pnpm put it under `<prefix>/lib`,
# volta and the version managers under their own store. Those are this machine's Codex, and
# adopting one is the 2026-09-10 repair working -- only a tree a project installed for itself
# must be left alone.
GLOBAL_MODULE_PARENTS = ('lib', 'pnpm', '.volta', '.nvm', '.fnm', '.asdf', '.bun')


def _project_tree(path: str | Path) -> bool:
    """True when `path` runs out of a dependency tree a project installed for itself."""
    parts = Path(path).parts
    for index, part in enumerate(parts):
        if part == 'node_modules' and not set(parts[:index]) & set(GLOBAL_MODULE_PARENTS):
            return True
    return False


def dependency_entry(path: str | Path, real: str | Path | None = None) -> bool:
    """True when the entry, or the executable behind it, sits in a project's dependency tree."""
    return _project_tree(path) or bool(real and _project_tree(real))


DRIFT_CAUSES = {
    'replaced': 'a Codex update replaced {path} and xswap could not reconnect it',
    'missing': '{path} does not exist',
    'not-a-symlink': '{path} is not a symlink, so xswap leaves it alone',
    'not-user-owned': '{path} is not owned by this user, so xswap leaves it alone',
    'dangling-target': '{path} -> {target} does not exist, so xswap leaves it alone',
    'not-executable': '{path} -> {target} is not an executable file, so xswap leaves it alone',
    'auto-disabled': 'automatic switching is disabled, so xswap leaves {path} alone (-> {target})',
    'unreadable': '{path} could not be inspected ({error}), so xswap leaves it alone',
    'proxy-missing': 'the xswap-codex xswap recorded ({proxy}) no longer exists, so pointing {path} at it would '
        'leave plain codex on a link to nothing',
    DEPENDENCY_REASON: '{path} -> {real} belongs to a project (it is inside a node_modules tree), not to this '
        "machine's Codex install, so xswap does not wrap it on its own",
    RELATIVE_ENTRY_REASON: '{path} is found through a relative PATH entry, which the shell resolves against the '
        'working directory, so it names a different file in every directory and xswap never re-points it',
    RELATIVE_RECORD_REASON: 'the recorded codex entry {path} is not an absolute path, so it names a different file '
        'in every working directory: xswap cannot tell which entry it wrapped and leaves every codex alone',
    BYPASS_REASON: 'XSWAP_BYPASS=1 is set in this environment, so the xswap-codex entry point hands every command '
        "straight to the real Codex with the caller's own CODEX_HOME, account and API keys",
}
DRIFT_FIXES = {
    'replaced': 'reconnect it by hand: {reconnect}',
    'missing': 'reinstall Codex or recreate the link, then run: {reconnect}',
    'not-a-symlink': 'replace it with a user-owned symlink to the Codex executable, then run: {reconnect}',
    'not-user-owned': 'make it a user-owned symlink (or use xswap directly), then run: {reconnect}',
    'dangling-target': 'reinstall Codex or point the link at a Codex executable, then run: {reconnect}',
    'not-executable': 'point the link at a Codex executable, then run: {reconnect}',
    'auto-disabled': 'enable automatic switching and connect it: {reconnect}',
    'unreadable': 'check its permissions, then run: {reconnect}',
    'proxy-missing': 'reinstall xswap, which is what installs the xswap-codex command, then run: {reconnect}',
    DEPENDENCY_REASON: 'take that directory off PATH ahead of the codex xswap wrapped; if it really is the Codex '
        'you want plain codex to run, wrap it by hand from this shell: {reconnect}',
    RELATIVE_ENTRY_REASON: 'make that PATH entry absolute; until then what plain codex runs depends on the '
        'directory you run it from',
    RELATIVE_RECORD_REASON: 'point the codex entry that still runs xswap-codex back at the real Codex by hand, then '
        'from a shell whose PATH holds no relative entry run: {reconnect}',
    BYPASS_REASON: 'unset XSWAP_BYPASS in this shell, and set it for the one command that needs its own home '
        'instead of exporting it',
}


def entry_drift(path: str | Path, proxy: str | None, enabled: bool = True, adopt: bool = False) -> Drift:
    """Classify one `codex` entry against the recorded xswap-codex proxy.

    Codex's standalone updater (ctrl+u in the TUI, `codex upgrade`, the install
    script) re-points the user-owned `codex` symlink at its new release and so
    silently disconnects plain `codex` from xswap. Returns a dict with the same
    keys every time:

      action  'reconnect' (a user-owned symlink now points at another existing
              executable and automatic switching is enabled) or 'skip'
      reason  one of DRIFT_REASONS; 'ok' means the entry runs xswap-codex and
              'replaced' is the only reason paired with 'reconnect'
      path    the entry that was judged
      target  the entry's current link text (may be relative), or None
      real    that target as an absolute path, or None
      proxy   the recorded xswap-codex path, or None
      error   short OS error text for 'unreadable', else None

    `adopt` says xswap is judging an entry it has no record of and would wrap on its
    own (the shadow repair); such an entry also has to come from somewhere xswap can
    attribute to a Codex install, which a project's dependency tree is not.

    Pure: lstat/readlink/access only, never a write. Every condition that used to
    be a silent None is named, so doctor and use/switch can explain a gap nothing
    will ever close on its own. A link-state reason wins over `auto-disabled`:
    those need the same manual repair whether or not switching is on.
    """
    path = Path(path)
    result = {'action': 'skip', 'reason': 'replaced', 'path': str(path),
              'target': None, 'real': None, 'proxy': proxy, 'error': None}
    try:
        # Before the entry: nothing about it can be repaired while the xswap-codex the record
        # names is gone (xswap re-installed to another prefix, a venv or worktree deleted).
        # Reconnecting pointed the entry at the missing file and reported success, so plain
        # `codex` stopped resolving at all while `wrapper_drift` still said `ok` and doctor
        # blamed the PATH ("... is not on this shell's PATH") of the directory that held it.
        if proxy and not Path(proxy).is_file():
            return {**result, 'reason': 'proxy-missing'}
        # lexists, not exists: a dangling symlink still occupies the entry and
        # takes a different repair than a missing one.
        if not os.path.lexists(path):
            return {**result, 'reason': 'missing'}
        if not path.is_symlink():
            return {**result, 'reason': 'not-a-symlink'}
        target = os.readlink(path)
        result.update(target=target, real=link_target_path(path, target))
        if target == proxy:
            return {**result, 'reason': 'ok'}
        # Ownership is checked after that: an entry already running xswap-codex is
        # connected whoever owns it, which is the rule doctor has always applied.
        if path.lstat().st_uid != os.getuid():
            return {**result, 'reason': 'not-user-owned'}
        real = Path(result['real'])
        if not real.is_file():
            return {**result, 'reason': 'dangling-target'}
        if not os.access(real, os.X_OK):
            return {**result, 'reason': 'not-executable'}
        if real.resolve() == Path(proxy).resolve():  # type: ignore[arg-type]  # every caller passes a real recorded proxy path here
            return {**result, 'reason': 'ok'}
    except (OSError, RuntimeError) as error:
        # RuntimeError: Path.resolve() on a symlink loop before Python 3.13. Its
        # message quotes the loop's own path, so report the kind of failure only;
        # the cause sentence already names the entry.
        return {**result, 'reason': 'unreadable',
                'error': getattr(error, 'strerror', None) or type(error).__name__}
    if adopt and dependency_entry(result['path'], result['real']):
        return {**result, 'reason': DEPENDENCY_REASON}
    if not enabled:
        return {**result, 'reason': 'auto-disabled'}
    return {**result, 'action': 'reconnect', 'reason': 'replaced'}


def wrapper_drift(settings: dict[str, Any]) -> Drift:
    """Classify the recorded codex entry; same keys as entry_drift, plus 'not-wrapped' and 'relative-record'."""
    wrapper = settings.get('wrapper') or {}
    proxy = wrapper.get('proxy')
    if not proxy or not wrapper.get('path'):
        return {'action': 'skip', 'reason': 'not-wrapped', 'path': wrapper.get('path') or None,
                'target': None, 'real': None, 'proxy': proxy or None, 'error': None}
    # entry_drift stats and re-points the path it is given, and a relative one names a different
    # file in every working directory: never its subject (RELATIVE_RECORD_REASON).
    if not Path(wrapper['path']).is_absolute():
        return {'action': 'skip', 'reason': RELATIVE_RECORD_REASON, 'path': wrapper['path'],
                'target': None, 'real': None, 'proxy': proxy, 'error': None}
    return _late('entry_drift')(wrapper['path'], proxy, bool(settings.get('enabled')))


def describe_drift(drift: Drift, reconnect: str) -> tuple[str, str]:
    """(cause, fix) for a drift result xswap did not act on; ('', '') for 'ok' and 'not-wrapped'.

    `reconnect` is the `xswap auto-enable --accounts ... --wrap-codex` line for the
    caller's account set. Paths and short OS error text only; never secrets.
    """
    reason = drift.get('reason')
    if reason not in DRIFT_CAUSES:
        return '', ''
    values = {**drift, 'reconnect': reconnect}
    return DRIFT_CAUSES[reason].format(**values), DRIFT_FIXES[reason].format(**values)


def codex_path_entries(
    settings: dict[str, Any], env: dict[str, str] | None = None, relative: bool = False
) -> list[dict[str, Any]]:
    """Every `codex` a PATH lookup can run, in lookup order, classified against the recorded wrapper.

    Mirrors shutil.which's per-directory test (an existing file with the execute
    bit; a dangling link or a directory is skipped) over the absolute PATH
    directories -- a `~` element expanded first, because `sh` and `bash` run the
    `codex` it names and shutil.which does not -- so entries[0] is what plain
    `codex` runs from any working directory. A directory listed twice on PATH yields one entry. `kind` is
    'wrapper' when the entry's link target is the recorded proxy or the entry
    resolves to the same file as the proxy, 'foreign' otherwise; `target` is the
    raw link target of a symlink and None for a regular file. `env` supplies PATH
    instead of os.environ, which keeps doctor pure and lets a test pin a PATH
    without patching the process environment.

    `relative` also reports the `codex` a relative PATH element -- an empty element
    included, which is POSIX's working directory -- resolves to in the current
    working directory. Such an entry is kind 'relative' only when it is not the
    wrapper: one that reaches xswap-codex here is what plain `codex` runs here, so
    it is kind 'wrapper' like any other, and its relative `path` is what keeps it
    out of every repair. Nothing may re-point a relative entry, so every repair
    path leaves it out (the default); doctor asks for it because plain `codex` in
    that directory still runs it.
    """
    wrapper = settings.get('wrapper') or {}
    proxy = wrapper.get('proxy')
    entries, seen = [], set()
    for element in os.get_exec_path(env):
        # `sh` and `bash` tilde-expand a PATH element before the lookup, so PATH="~/bin:..."
        # (a quoted export, a Makefile, a plist) really does run ~/bin/codex for them -- while
        # this walk read the literal `~/bin`, found no entry there, and reported the wrapped
        # one as what plain `codex` runs. An element that expands to an absolute path is an
        # ordinary entry from here on; one that does not stays relative (zsh leaves it alone,
        # and nothing may re-point it).
        # os.path.expanduser, not Path.expanduser(): an unresolvable ~user (no such
        # account) must come back unchanged, which is what marks it still relative
        # below; Path.expanduser() raises RuntimeError for that case instead.
        directory = os.path.expanduser(element) if element.startswith('~') else element  # noqa: PTH111
        # A relative PATH element (a project's `bin`, a direnv habit) names a different
        # file in every working directory, so it can never be the recorded entry and must
        # never be re-pointed: xswap would rewrite a `codex` shim inside the user's own
        # repository and store a `path` no other directory can find again. Leaving it out
        # of the report as well is what made the bypass read as OK, so it is still listed
        # on request -- classified so no repair can mistake it for something to act on.
        # An empty element is POSIX's spelling of the working directory (bash and zsh both
        # run ./codex for it, and `export PATH="$UNSET_VAR:$PATH"` writes one), so it is a
        # relative element too; dropping it first left the one element that can bypass
        # xswap out of every report while shutil.which still found it.
        is_relative = not directory or not Path(directory).is_absolute()
        if is_relative and not relative:
            continue
        candidate = Path(directory) / 'codex'
        # Per PATH directory, not per realpath: on this machine several entries
        # resolve to the same xswap-codex script, and collapsing them would hide
        # both the count doctor reports and the recorded entry's place in the order.
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            if not candidate.is_file() or not os.access(candidate, os.X_OK):
                continue
            target = os.readlink(candidate) if candidate.is_symlink() else None
        except OSError:
            continue
        # The proxy test comes first: a relative element whose `codex` reaches xswap-codex here
        # is what plain `codex` runs here, so calling it 'relative' made every reader of this
        # list say plain `codex` bypasses the selection when it does not. It is still not an
        # entry any repair may touch -- the path stays relative, which is what keeps it out of
        # the default list and out of every branch that re-points an entry.
        # Any xswap-codex, not only the recorded one: a second install ahead on PATH runs the
        # same entry point against the same auto.json, so plain `codex` there does go through
        # xswap. Calling it 'foreign' made the shadow repair adopt it and record an xswap-codex
        # as the real Codex, which is the exec loop (wrapped_target).
        kind = ('wrapper' if proxy and (target == proxy or wrapped_target(candidate, settings))
                else 'relative' if is_relative else 'foreign')
        # Path spells both '' and '.' as a bare 'codex'; name the working directory the way the
        # shell prints it, so the reported entry is a file a reader can check.
        shown = str(candidate)
        entries.append({'path': './codex' if shown == 'codex' else shown, 'target': target, 'kind': kind})
    return entries


def wrapper_state(
    settings: dict[str, Any], env: dict[str, str] | None = None, relative: bool = False
) -> tuple[str, dict[str, Any] | None, list[dict[str, Any]]]:
    """What plain `codex` runs relative to the recorded wrapper: (state, first, entries).

    2026-09-10: Codex's standalone installer wrote ~/.local/bin/codex ahead of the
    wrapped /opt/homebrew/bin/codex, so plain `codex` ran its own release against
    ~/.codex while every check that read only auto.json reported a connected
    wrapper. `state` is 'unconfigured' (no wrapper record, or automatic switching
    disabled: the record is dormant and the entry was restored), 'absent' (no
    codex on this PATH), 'connected' (the first entry is xswap-codex), 'drifted'
    (the recorded entry itself no longer points at xswap-codex) or 'shadowed'
    (another entry precedes it). `first` is entries[0] or None.

    `relative` is handed to codex_path_entries, and only a caller that merely
    reports what plain `codex` runs passes it: `state` is then also 'relative',
    meaning the entry a relative PATH element resolves to in this working directory
    runs instead of the wrapped one. A relative entry that resolves to xswap-codex
    here is kind 'wrapper', so the state is 'connected' and no surface claims a
    bypass that is not happening; doctor still warns, because the next directory
    decides again. Nothing may re-point a relative entry, so every repair path
    keeps the default and cannot see it -- shadowing_entry included.
    The 'unconfigured' gate still comes first: with automatic switching off nothing
    claims plain `codex` goes through xswap, so the reason to report is the recorded
    entry's own ('auto-disabled'), not the PATH element. A relative entry that reaches
    xswap-codex is a caveat and never a verdict: it can only keep a 'connected' answer,
    and when the absolute entries say anything else that is what is returned -- state,
    first and entries -- because they are what every other working directory runs.
    """
    entries = codex_path_entries(settings, env, relative=relative)
    state, first = path_state(settings, entries)
    if relative and state == 'connected' and first is not None and not Path(first['path']).is_absolute():
        # This directory's `codex` is xswap-codex, but only this directory's: the element is
        # never re-pointed, so what plain `codex` runs everywhere else is decided by the
        # absolute entries alone. Returning 'connected' from the relative walk let a machine
        # that is permanently bypassed -- a foreign regular file ahead of the wrapped entry,
        # which xswap never overwrites -- report codexWrapped true and print no use/switch
        # notice, as long as the working directory happened to hold an xswap-codex. The
        # caveat can only keep a connected answer; when the absolute entries say anything
        # else, they are the answer (2026-09-13).
        absolute = [entry for entry in entries if Path(entry['path']).is_absolute()]
        absolute_state, absolute_first = path_state(settings, absolute)
        if absolute_state != 'connected':
            return absolute_state, absolute_first, absolute
    return state, first, entries


def path_state(settings: dict[str, Any], entries: list[dict[str, Any]]) -> tuple[str, dict[str, Any] | None]:
    """(state, first) for one codex_path_entries list; the PATH walk is the caller's.

    Split out of wrapper_state so a caller that needs both views -- doctor reports what
    plain `codex` runs in this directory *and* what it runs in every other one -- judges
    them from a single walk instead of racing two (CONC-4).
    """
    wrapper = settings.get('wrapper') or {}
    first = entries[0] if entries else None
    if not wrapper.get('path') or not wrapper.get('proxy') or not settings.get('enabled'):
        return 'unconfigured', first
    if first is None:
        return 'absent', first
    if first['kind'] == 'relative':
        return 'relative', first
    if first['kind'] == 'wrapper':
        return 'connected', first
    if first['path'] == wrapper['path']:
        return 'drifted', first
    return 'shadowed', first


def shadowing_entry(settings: dict[str, Any], env: dict[str, str] | None = None) -> tuple[str, str] | None:
    """(path, target) of a wrappable `codex` entry ahead of the recorded one on PATH, or None.

    Acts only when the recorded entry is itself on this PATH: then the new entry
    is what a Codex install put in front of the wrapped one. A PATH without the
    recorded entry (a launchd job, a test fixture) says nothing about the user's
    shell, so nothing is re-pointed from it. wrapper_state already applied the
    `enabled` gate, so entry_drift keeps its default for that; `adopt` is what it does
    not keep, since this is the one place xswap wraps an entry nobody asked it to.
    """
    wrapper = settings.get('wrapper') or {}
    state, first, entries = wrapper_state(settings, env)
    if state != 'shadowed' or not any(entry['path'] == wrapper['path'] for entry in entries):
        return None
    assert first is not None  # "shadowed" always carries a first PATH entry  # noqa: S101 -- narrows an invariant the checker can't see across the call; not user input
    drift = _late('entry_drift')(first['path'], wrapper['proxy'], adopt=True)
    return (first['path'], drift['target']) if drift['action'] == 'reconnect' else None


def wrapper_records(settings: dict[str, Any]) -> list[dict[str, Any]]:
    """Every wrapped codex entry with rollback data: the primary `wrapper`, then `wrappers` (0.8.0)."""
    records = []
    for record in [settings.get('wrapper'), *(settings.get('wrappers') or [])]:
        if isinstance(record, dict) and record.get('path') and record.get('proxy') and record.get('originalTarget'):
            records.append(record)
    return records


def set_primary_wrapper(settings: dict[str, Any], record: dict[str, Any]) -> None:
    """Make `record` the primary `wrapper` (the entry plain `codex` runs through).

    A previous primary at another path moves to `wrappers` so auto-disable
    restores it too; an older record for the same path is replaced. One record per
    path. Every wrapper record xswap creates passes through here, so there is one
    place to look for what gets stored.
    """
    kept = {}
    for entry in [settings.get('wrapper'), *(settings.get('wrappers') or [])]:
        if isinstance(entry, dict) and entry.get('path') and entry['path'] != record['path'] and entry['path'] not in kept:
            kept[entry['path']] = entry
    settings['wrapper'] = record
    if kept:
        settings['wrappers'] = list(kept.values())
    else:
        settings.pop('wrappers', None)


def _relink(manager: Manager, settings: dict[str, Any], path: Path, proxy: str, problem: str) -> bool:
    """Persist the rollback record, then atomically point `path` at xswap-codex.

    False after one stderr line when the link cannot be replaced; the record
    already names the release, so the next call retries.
    """
    try:
        atomic_json(settings_path(manager.root), settings)
        _late('swap_symlink')(path, proxy)
    except OSError as error:
        print(f'xswap: {problem} but it could not be reconnected ({error.strerror or error}); '
              f'run: xswap auto-enable --accounts {",".join(settings.get("accounts", []))} --wrap-codex',
              file=sys.stderr)
        return False
    return True


def reconnect_wrapper(manager: Manager) -> str | None:
    """Keep plain `codex` connected while automatic switching is enabled.

    Runs on every xswap launch, usage read, and selection, and when a bridged
    session ends (a Codex update usually happens inside one). Two repairs, in
    order: the recorded entry re-pointed by an update is pointed back at
    xswap-codex with the new release recorded as the real Codex (0.7.8); a new
    user-owned `codex` symlink that a Codex install put ahead of the recorded
    entry on PATH is wrapped as well and becomes the primary record, the previous
    one kept in `wrappers` for auto-disable (0.8.0, after the 2026-09-10 bypass).
    Returns the real Codex path after a change, or None when nothing was changed.
    Every skip stays silent here (this runs inside list/usage and so on every
    alert-job tick), a busy registry lock included; wrapper_drift names the
    reason for doctor and use/switch.
    `xswap auto-disable` restores every record.
    """
    from xswap.providers.codex.relocate import canonical_codex_path, inside_root
    # Non-blocking: this runs inside list/usage/launch/selection, and the same lock is held
    # across whole OpenClaw subprocesses. Contention is one more silent skip (Manager.try_locked).
    with manager.try_locked() as acquired:
        if not acquired:
            return None
        settings = read_settings(manager)
        result = None
        drift = wrapper_drift(settings)
        if drift['action'] == 'reconnect':
            wrapper = settings['wrapper']
            path = Path(drift['path'])
            # install.sh writes "$CODEX_HOME/packages/standalone/current/bin/codex" literally;
            # through an owned home's packages link that spells xswap's root, so record it via
            # the reference home instead. The literal link text stays the rollback target only
            # when nothing had to be rewritten.
            real = canonical_codex_path(manager, drift['real'])
            wrapper.update(originalTarget=drift['target'] if real == drift['real'] else real, realCodex=real)
            misplaced = inside_root(manager, real)
            if not _relink(manager, settings, path, wrapper['proxy'], f'a Codex update replaced {path}'):
                return None
            print(f'xswap: a Codex update had replaced {path}; reconnected it to xswap-codex. Codex is now {real}.'
                  + (' It was installed inside the xswap state directory; run: xswap relocate-codex' if misplaced else ''),
                  file=sys.stderr)
            result = real
        shadow = shadowing_entry(settings)
        if shadow is not None:
            shadow_path, shadow_target = shadow
            previous = settings['wrapper']
            # A shadowing entry is what a Codex install just wrote, so it too can spell a
            # release through an owned home's packages link (Mini, 2026-09-13).
            literal = link_target_path(Path(shadow_path), shadow_target)
            real = canonical_codex_path(manager, literal)
            set_primary_wrapper(settings, {'path': shadow_path, 'originalTarget': shadow_target if real == literal else real,
                                           'realCodex': real, 'proxy': previous['proxy']})
            if not _relink(manager, settings, Path(shadow_path), previous['proxy'],
                           f'{shadow_path} appeared ahead of {previous["path"]} on PATH'):
                return result
            print(f'xswap: {shadow_path} had appeared ahead of {previous["path"]} on PATH and bypassed xswap; '
                  f'connected it to xswap-codex. Codex is now {real}.', file=sys.stderr)
            result = real
    return result


def enable(manager: Manager, accounts: str, wrap: bool = False) -> None:
    names = [value.strip() for value in accounts.split(',') if value.strip()]
    AccountPool(manager, names, manager.codex())
    with manager.locked():
        # Inside the lock, like every other auto.json writer. Read outside it, this snapshot
        # was taken while a concurrent reconnect_wrapper (every launch, list, usage read and
        # alert tick runs one) held the lock, and the write below then discarded that commit:
        # the entry it had just wrapped kept running xswap-codex with no record left, so
        # `auto-disable` could not restore it and doctor still reported OK. The competitor's
        # whole lock hold is the window, not the microseconds between these two lines.
        settings = read_settings(manager)
        settings.update({'enabled': True, 'accounts': names})
        if wrap:
            proxy = shutil.which('xswap-codex')
            executable = shutil.which('codex')
            if not proxy or not executable:
                raise LiveError('codex and xswap-codex must both be installed')
            # shutil.which joins the raw PATH element, so a relative one yields a relative
            # path: the same entry codex_path_entries refuses to re-point. Absolutising it
            # would wrap whatever `bin/codex` this directory happens to hold (a project's
            # own shim) and record a `path` no other directory can find again, which no
            # later `auto-disable` could restore -- so refuse instead.
            if not Path(executable).is_absolute():
                raise LiveError(f'codex was found through a relative PATH entry ({executable}), which names a '
                                'different file in every directory; make that PATH entry absolute, then retry')
            # The same for xswap-codex, which had no guard: absolutising a relative hit against
            # the working directory recorded whatever `bin/xswap-codex` that directory happened
            # to hold as the proxy, pointed the global `codex` at it, and every later
            # reconnect re-applied the recorded value -- so a project file stayed the thing
            # plain `codex` runs from every directory, with doctor reporting `-> xswap-codex`.
            if not Path(proxy).is_absolute():
                raise LiveError(f'xswap-codex was found through a relative PATH entry ({proxy}), which names a '
                                'different file in every directory; make that PATH entry absolute, then retry')
            target = Path(executable)
            # "Already wrapped?" compared the entry with the one xswap-codex `which` found now.
            # With a second install ahead on PATH (a project venv, pipx beside uv) the entry
            # resolved to the *other* xswap-codex, so this branch wrapped it and stored an
            # xswap-codex as `realCodex`: the release path was erased from auto.json and plain
            # `codex` exec'd xswap-codex for ever. Any xswap-codex counts as wrapped.
            if target.resolve() != Path(proxy).resolve() and not wrapped_target(target, settings):
                if not target.is_symlink() or target.lstat().st_uid != os.getuid():
                    raise LiveError('codex wrapper installation requires a user-owned codex symlink; use xswap instead')
                original = os.readlink(target)
                from xswap.providers.codex.relocate import (
                    canonical_codex_path,
                    inside_root,
                )
                # A release the installer put under an owned home's packages link is recorded
                # through the reference home, so purging auto/ cannot take plain codex with it.
                literal = link_target_path(target, original)
                real = canonical_codex_path(manager, literal)
                set_primary_wrapper(settings, {'path': str(target), 'originalTarget': original if real == literal else real,
                                               'realCodex': real, 'proxy': proxy})
                if inside_root(manager, real):
                    print(f'xswap: the real Codex ({real}) is inside the xswap state directory; run: xswap relocate-codex',
                          file=sys.stderr)
                # Persist rollback information before the atomic symlink swap.
                atomic_json(settings_path(manager.root), settings)
                _late('swap_symlink')(target, proxy)
            elif not settings.get('wrapper'):
                raise LiveError('existing codex wrapper has no recovery information')
        atomic_json(settings_path(manager.root), settings)
    print('Auto switching enabled: ' + ' -> '.join(names) + '. New xswap CLI/app sessions use the pool.' +
          (' The codex command is also connected.' if settings.get('wrapper') else ''))
    # The record that decides what plain `codex` does went into the root this shell named, and
    # every other shell, launchd job and desktop app reads its own. Said here because this is
    # where the choice is made; doctor's state root row repeats it afterwards.
    chosen = os.environ.get(ROOT_VARIABLE)
    # Only when the variable is what put the record here: a caller that passed its own root
    # (a test, an embedding) never read the variable, so naming it would be a false alarm --
    # and it made `python -m unittest` fail in any shell that exports CODEX_SWAP_HOME.
    selected = chosen and Path(chosen).expanduser().resolve() == manager.root
    if selected and settings.get('wrapper') and manager.root != default_root():
        print(f'xswap: this shell\'s {ROOT_VARIABLE} put that record in {manager.root}; a shell, launchd job or '
              f'app without {ROOT_VARIABLE} reads {default_root()} instead and runs whatever selection it holds. '
              f'Export {ROOT_VARIABLE}={manager.root} wherever codex runs.', file=sys.stderr)


def set_policy(manager: Manager, weekly_remaining: float) -> None:
    threshold = validate_threshold(weekly_remaining)
    with manager.locked():
        settings = read_settings(manager)
        settings['weeklyRemainingThreshold'] = threshold
        atomic_json(settings_path(manager.root), settings)
    print(f'Weekly reserve: {threshold:g}% remaining ({100-threshold:g}% used). '
          'Compatible running bridges apply changes before the next idle turn. '
          'Bridges older than 0.3.2 need a new auto session once; none were stopped.')


def disable(manager: Manager) -> None:
    from xswap.providers.codex.relocate import inside_root
    with manager.locked():
        settings = read_settings(manager)
        primary = settings.get('wrapper')
        # Every secondary record this run did not restore. `wrappers` was popped whatever
        # happened, so the records for the entries disable had just refused to touch -- a
        # relative one it cannot identify, one changed outside xswap -- went with it: the entry
        # kept running xswap-codex with its rollback target gone, and no later command, doctor
        # included, could still name what it should point back at.
        unrestored = []
        for record in wrapper_records(settings):
            if not Path(record['path']).is_absolute():
                if record is not primary:
                    unrestored.append(record)
                # A record written by 0.7.8 can hold the relative `shutil.which` result
                # (RELATIVE_RECORD_REASON). Resolving it here would restore whatever `codex`
                # this working directory holds -- a shim inside an unrelated repository -- and
                # still leave the entry xswap wrapped running xswap-codex, so name it instead.
                print(f'codex entry {record["path"]} is not an absolute path; left it untouched. Point the codex '
                      'entry that still runs xswap-codex back at the real Codex by hand.', file=sys.stderr)
                continue
            path = Path(record['path'])
            if path.is_symlink() and os.readlink(path) == record['proxy']:
                _late('swap_symlink')(path, record['originalTarget'])
                # Disconnecting is the user's call even when the release behind the entry is
                # gone or sits inside xswap's state, but plain codex then fails with no hint.
                restored = Path(link_target_path(path, record['originalTarget']))
                if not restored.is_file():
                    print(f'xswap: restored {path} -> {record["originalTarget"]}, but that Codex executable is missing; '
                          'plain codex will not start until Codex is reinstalled (the installer re-creates the link).',
                          file=sys.stderr)
                elif inside_root(manager, str(restored)):
                    print(f'xswap: {path} now points inside the xswap state directory ({restored}); '
                          'run: xswap relocate-codex', file=sys.stderr)
            else:
                if record is not primary:
                    unrestored.append(record)
                # Several entries can be wrapped now, so name the one left behind.
                print(f'codex entry {path} changed outside xswap; left it untouched.', file=sys.stderr)
        # A restored secondary record has nothing left to recover and is dropped, as before;
        # one disable could not act on keeps its rollback data, because that entry may still
        # run xswap-codex and the record is the only thing that names what it was.
        if unrestored:
            settings['wrappers'] = unrestored
        else:
            settings.pop('wrappers', None)
        settings['enabled'] = False
        atomic_json(settings_path(manager.root), settings)
    print('Auto switching disabled for new sessions. Running auto sessions remain active.')
