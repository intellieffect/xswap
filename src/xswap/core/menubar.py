"""Build the local macOS menu app from bundled source; no downloaded binaries."""
from __future__ import annotations

import hashlib
import plistlib
import shutil
import subprocess
import sys
import tempfile
from importlib.resources import files
from pathlib import Path


def launch() -> int:
    from xswap.manager import SwapError, __version__
    if sys.platform != 'darwin':
        raise SwapError('The menu bar app requires macOS.')
    if not shutil.which('swiftc'):
        raise SwapError('Install Apple Command Line Tools (xcode-select --install), then retry.')
    executable = shutil.which('xswap')
    if not executable:
        raise SwapError('Install xswap in PATH before opening the menu bar app.')
    source_text = files('xswap.bridge').joinpath('MenuBar.swift').read_text()
    digest = hashlib.sha256(source_text.encode()).hexdigest()
    app = Path.home() / 'Applications' / 'Xswap.app'
    app.parent.mkdir(exist_ok=True)
    info = app / 'Contents' / 'Info.plist'
    if app.exists():
        try:
            metadata = plistlib.loads(info.read_bytes())
            if app.is_symlink() or metadata.get('CFBundleIdentifier') != 'com.intellieffect.xswap.menubar':
                raise ValueError()
            if metadata.get('CFBundleShortVersionString') == __version__ and metadata.get('XswapSourceSHA256') == digest:
                return subprocess.call(['/usr/bin/open', str(app), '--args', executable])  # noqa: S603 -- argv list, no shell=True; command/args are program-constructed, not user strings
        except (OSError, ValueError, plistlib.InvalidFileException):
            raise SwapError('Existing Xswap.app is not a recognized xswap menu app; left untouched.') from None
    with tempfile.TemporaryDirectory(prefix='xswap-menubar-') as temporary:
        source = Path(temporary) / 'MenuBar.swift'
        source.write_text(source_text)
        binary = Path(temporary) / 'XswapMenu'
        result = subprocess.run(['/usr/bin/xcrun', 'swiftc', str(source), '-o', str(binary), '-framework', 'AppKit'], capture_output=True)  # noqa: S603 -- argv list, no shell=True; command/args are program-constructed, not user strings
        if result.returncode:
            raise SwapError('Menu app compilation failed. Check that Apple Command Line Tools match your macOS version.')
        contents = app / 'Contents'
        (contents / 'MacOS').mkdir(parents=True, exist_ok=True)
        target = contents / 'MacOS' / 'XswapMenu'
        staging = target.with_suffix('.new')
        shutil.copy2(binary, staging)
        Path(staging).replace(target)
        info.write_bytes(plistlib.dumps({'CFBundleIdentifier': 'com.intellieffect.xswap.menubar',
            'CFBundleExecutable': 'XswapMenu', 'CFBundleName': 'Xswap',
            'CFBundlePackageType': 'APPL', 'CFBundleShortVersionString': __version__,
            'XswapSourceSHA256': digest, 'LSUIElement': True, 'NSHighResolutionCapable': True}))
    print('Opened Xswap menu bar. Refreshes every 5 minutes; use its Quit item to stop. Login startup is not enabled.')
    return subprocess.call(['/usr/bin/open', str(app), '--args', executable])  # noqa: S603 -- argv list, no shell=True; command/args are program-constructed, not user strings
