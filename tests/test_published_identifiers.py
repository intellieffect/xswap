"""Identifiers that live on users' machines must survive every refactor.

The launchd label names the alert job's plist file and is what `launchctl`
targets; the bundle identifier is how `xswap menubar` recognises the app it
installed earlier. Both are strings, so a module-path rename that sweeps
string literals changes them silently -- which happened once (INT-5614) and
would have left every upgraded user with an orphaned alert job and a menu
bar app that xswap refuses to touch.
"""
from xswap.core import alert, menubar


def test_launchd_label_is_the_published_one():
    assert alert.LABEL == "com.intellieffect.xswap.alert"


def test_menu_bar_bundle_identifier_is_the_published_one():
    import inspect
    source = inspect.getsource(menubar)
    assert source.count("'com.intellieffect.xswap.menubar'") == 2
    assert "com.intellieffect.xswap.core.menubar" not in source
