"""Keep plugin code inside each runtime's existing trusted CODEX_HOME boundary."""
from __future__ import annotations
import ctypes
import errno
import fcntl
import os
from pathlib import Path
import shutil
import sys
import tempfile


def exchange_paths(left, right):
    """Atomically exchange a legacy symlink and a complete directory (no path gap)."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == 'darwin':
        fn = libc.renamex_np
        fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        result = fn(os.fsencode(left), os.fsencode(right), 2)  # RENAME_SWAP
    elif sys.platform.startswith('linux') and hasattr(libc, 'renameat2'):
        fn = libc.renameat2
        fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        result = fn(-100, os.fsencode(left), -100, os.fsencode(right), 2)  # RENAME_EXCHANGE
    else:
        raise OSError(errno.ENOTSUP, 'atomic plugin migration is unsupported on this platform')
    if result:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def validate_cache(cache):
    """Do not import code through links escaping the plugin cache boundary."""
    root = cache.resolve()
    for path in cache.rglob('*'):
        if path.is_symlink():
            try:
                resolved = path.resolve(strict=True)
                resolved.relative_to(root)
                if path.parent.resolve().is_relative_to(resolved):
                    raise ValueError('cyclic plugin link')
            except (ValueError, OSError, RuntimeError):
                raise ValueError('plugin cache contains an external or broken symlink') from None


def copy_cache(source, target):
    # Copy actual bytes, never hardlink code across independent runtime homes.
    # Internal aliases such as chrome/latest are materialized too.
    validate_cache(source)
    shutil.copytree(source, target, symlinks=False)


def _ensure_plugins(home, source, dry=False):
    """Migrate only the legacy shared link; preserve runtime-owned plugin folders.

    Each home owns its cache after migration. Subsequent plugin updates are the
    responsibility of that home's normal plugin manager, not cross-home syncing.
    No app-server state, credentials, staging, or user sessions are copied.
    """
    from codex_swap import SwapError, atomic_json, private_dir
    home, source = Path(home), Path(source)
    target = home / 'plugins'
    origin = source / 'plugins'
    if home.resolve() == source.resolve():
        return 'source-home'
    if target.exists() and not target.is_symlink():
        return 'already-local'
    if target.is_symlink():
        if target.lstat().st_uid != os.getuid() or target.resolve() != origin.resolve():
            raise SwapError('Refusing to replace an unrelated plugins symlink')
    if not origin.is_dir():
        return 'no-source-plugins'
    if dry:
        return 'would-materialize'
    private_dir(home)
    old_link = os.readlink(target) if target.is_symlink() else None
    stage = Path(tempfile.mkdtemp(prefix='.xswap-plugins-', dir=home))
    try:
        cache = origin / 'cache'
        if cache.is_dir():
            copy_cache(cache, stage / 'cache')
        # Preserve known executable helpers used by already-running plugin managers.
        # Never copy plugin app-server state or staging data.
        for name in ('codex', 'codex-code-mode-host'):
            helper = origin / '.plugin-appserver' / name
            if helper.is_file() and not helper.is_symlink():
                private_dir(stage / '.plugin-appserver')
                shutil.copy2(helper, stage / '.plugin-appserver' / name)
        if old_link is not None:
            # Record recovery metadata before changing the path; contains no secrets.
            atomic_json(home / '.xswap-plugins-migration.json',
                        {'version': 1, 'previousLink': old_link, 'source': str(origin.resolve())})
            if not target.is_symlink() or os.readlink(target) != old_link:
                raise SwapError('Plugins path changed during preparation; left untouched')
            exchange_paths(stage, target)
            stage.unlink()  # The old link, not its target.
        else:
            os.rename(stage, target)
        return 'materialized'
    except ValueError as exc:
        raise SwapError(str(exc)) from None
    finally:
        if stage.is_symlink():
            stage.unlink()
        elif stage.exists():
            shutil.rmtree(stage)


def ensure_plugins(home, source, dry=False):
    from codex_swap import private_dir
    home, source = Path(home), Path(source)
    if dry or home.resolve() == source.resolve():
        return _ensure_plugins(home, source, dry)
    private_dir(home)
    fd = os.open(home / '.xswap-plugins.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return _ensure_plugins(home, source)
    finally:
        os.close(fd)


def repair(manager, dry=False):
    import json
    from codex_swap import SwapError
    homes = [manager.root / 'auto' / 'codex', manager.root / 'auto' / 'cli-codex']
    homes += [Path(value['home']) for value in manager.read()['accounts'].values()]
    sources = [manager.source, *homes]
    results = []
    for home in dict.fromkeys(homes):
        target = home / 'plugins'
        if not target.is_symlink():
            continue
        origin = next((source for source in sources if source != home and
                       (source / 'plugins').is_dir() and
                       (source / 'plugins').resolve() == target.resolve()), None)
        if origin is None:
            raise SwapError('Plugins link does not match a registered source; left untouched')
        results.append({'home': str(home), 'result': ensure_plugins(home, origin, dry)})
    print(json.dumps(results, indent=2))
