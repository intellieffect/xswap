"""Profile provisioning regressions; all homes are disposable pytest fixtures."""
import pytest

from xswap.manager import Manager, atomic_json


@pytest.fixture
def tooling_manager(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.toml").write_text('model = "example"\n')
    return Manager(tmp_path / "store", source)


@pytest.mark.parametrize("existing", [False, True])
def test_managed_account_receives_discovered_profile(tooling_manager, existing):
    manager = tooling_manager
    if existing:
        manager.prepare("work")
    profile = manager.source / "audit.config.toml"
    profile.write_text('model = "example"\n')

    home = manager.prepare("work")

    assert (home / profile.name).is_symlink()
    assert (home / profile.name).resolve() == profile
    assert manager.prepare("work") == home
    assert (home / profile.name).read_text() == profile.read_text()


@pytest.mark.parametrize("kind", ["file", "link", "broken-link", "directory"])
def test_existing_profile_is_preserved(tooling_manager, tmp_path, kind):
    manager = tooling_manager
    home = manager.prepare("work")
    profile = manager.source / "audit.config.toml"
    profile.write_text('model = "source"\n')
    destination = home / profile.name
    target = tmp_path / "user-profile"
    if kind == "file":
        destination.write_bytes(b"user override\n")
    elif kind == "directory":
        destination.mkdir()
    else:
        if kind == "link":
            target.write_bytes(b"user linked override\n")
        destination.symlink_to(target)
    before = destination.lstat()

    manager.prepare("work")

    assert destination.lstat() == before
    if kind == "file":
        assert destination.read_bytes() == b"user override\n"
    elif kind in ("link", "broken-link"):
        assert destination.readlink() == target


def test_only_regular_profile_targets_are_supplied(tooling_manager):
    manager = tooling_manager
    (manager.source / "directory.config.toml").mkdir()
    (manager.source / "broken.config.toml").symlink_to(manager.source / "missing")
    (manager.source / "unrelated.toml").write_text("unrelated")
    (manager.source / "notes.txt").write_text("notes")

    home = manager.prepare("work")

    for name in ("directory.config.toml", "broken.config.toml", "unrelated.toml", "notes.txt"):
        assert not (home / name).exists()
        assert not (home / name).is_symlink()


def test_external_registered_home_is_not_refreshed(tooling_manager, tmp_path):
    manager = tooling_manager
    external = tmp_path / "external"
    external.mkdir()
    atomic_json(external / "auth.json", {
        "auth_mode": "chatgpt",
        "tokens": {"access_token": "fake-token", "refresh_token": "fake-refresh"},
    })
    manager.register("external", external)
    (manager.source / "audit.config.toml").write_text('model = "example"\n')
    before = {path.name: path.read_bytes() for path in external.iterdir()}

    assert manager.prepare("external") == external
    assert manager.account("external")[1] == external

    assert {path.name: path.read_bytes() for path in external.iterdir()} == before


def test_account_lookup_does_not_refresh_managed_home(tooling_manager):
    manager = tooling_manager
    home = manager.prepare("work")
    (manager.source / "audit.config.toml").write_text('model = "example"\n')

    assert manager.account("work")[1] == home

    assert not (home / "audit.config.toml").exists()
    assert not (home / "audit.config.toml").is_symlink()


@pytest.mark.parametrize("name", ["audit.config.toml", "hooks.json"])
@pytest.mark.parametrize("kind", ["file", "link", "broken-link", "directory"])
def test_definition_destinations_preserved(tooling_manager, tmp_path, name, kind):
    manager = tooling_manager
    home = manager.prepare("work")
    dst = home / name
    if kind == "file":
        dst.write_text("local")
    elif kind == "directory":
        dst.mkdir()
    else:
        target = tmp_path / "override"
        if kind == "link":
            target.write_text("local")
        dst.symlink_to(target)
    before = dst.lstat()
    (manager.source / name).write_text("source")
    manager.prepare("work")
    assert dst.lstat() == before


def test_definitions_do_not_supply_or_change_trust_and_private_state(tooling_manager):
    manager = tooling_manager
    home = manager.prepare("work")
    private = ("auth.json", "hooks-state.json", "memories/state", "plugins/state", "sessions/state")
    for base, content in ((manager.source, "source"), (home, "private")):
        for name in private:
            path = base / name
            path.parent.mkdir(exist_ok=True)
            path.write_text(content)
    # Existing home config (including trust entries) is user-owned.
    (home / "config.toml").unlink()
    (home / "config.toml").write_text('trusted_hash = "old"\n')
    (manager.source / "hooks.json").write_text('{"hooks": {}}')
    manager.prepare("work")
    assert (home / "hooks.json").resolve() == manager.source / "hooks.json"
    (manager.source / "hooks.json").write_text('{"hooks": {"changed": []}}')
    manager.prepare("work")
    assert (home / "config.toml").read_text() == 'trusted_hash = "old"\n'
    for name in private:
        assert (home / name).read_text() == "private"
        assert not (home / name).is_symlink()
    fresh = manager.prepare("fresh")
    for name in private:
        assert not (fresh / name).exists()


def test_runtime_refresh_recreation_and_overrides(tooling_manager):
    from xswap.providers.codex.tooling import supply_runtime
    manager = tooling_manager
    source = manager.prepare("work")
    (source / "custom.config.toml").write_text("custom")
    (source / "hooks.json").write_text("local hooks")
    (manager.source / "audit.config.toml").write_text("late profile")
    (manager.source / "hooks.json").write_text("root hooks")
    for name in ("runtime", "recreated"):
        home = manager.root / name
        home.mkdir()
        supply_runtime(manager, "work", home)
        assert (home / "audit.config.toml").read_text() == "late profile"
        assert (home / "custom.config.toml").read_text() == "custom"
        assert (home / "hooks.json").read_text() == "local hooks"
        (home / "audit.config.toml").unlink()
        supply_runtime(manager, "work", home)
        assert (home / "audit.config.toml").is_symlink()


def test_refresh_refuses_escaped_managed_home(tooling_manager, tmp_path):
    manager = tooling_manager
    home = manager.prepare("work")
    # Replace an otherwise valid managed home with an external directory link.
    import shutil
    shutil.rmtree(home)
    external = tmp_path / "external"
    external.mkdir()
    home.symlink_to(external, target_is_directory=True)
    (manager.source / "hooks.json").write_text("source")
    manager.prepare("work")
    assert list(external.iterdir()) == []


def test_same_source_and_concurrent_link(tooling_manager, monkeypatch):
    from pathlib import Path

    from xswap.providers.codex.tooling import supply_definitions
    source = tooling_manager.source
    (source / "hooks.json").write_text("hooks")
    before = (source / "hooks.json").lstat()
    supply_definitions(source, source)
    assert (source / "hooks.json").lstat() == before
    home = tooling_manager.prepare("work")
    (home / "hooks.json").unlink()
    original = Path.symlink_to

    def competing_link(path, target, **kwargs):
        original(path, target, **kwargs)
        raise FileExistsError()

    monkeypatch.setattr(Path, "symlink_to", competing_link)
    supply_definitions(source, home)
    assert (home / "hooks.json").read_text() == "hooks"


@pytest.mark.parametrize("mode", ["cli", "app", "auto-cli", "auto-app"])
@pytest.mark.parametrize("dry", [False, True])
def test_launch_boundaries(tooling_manager, tmp_path, monkeypatch, mode, dry):
    import subprocess
    from unittest.mock import Mock

    from xswap.providers.codex.codex_cli import launch_cli

    manager = tooling_manager
    home = manager.prepare("work")
    manager.prepare("second")
    (manager.source / "hooks.json").write_text("hooks")
    (manager.source / "audit.config.toml").write_text("profile")
    app = tmp_path / "fixture.app"
    app.mkdir()
    executable = tmp_path / "codex"
    executable.touch()
    monkeypatch.setattr(manager, "codex", lambda: str(executable))
    monkeypatch.setattr("xswap.providers.codex.launcher.sys.platform", "darwin")
    monkeypatch.setattr("xswap.providers.codex.launcher.shutil.which", lambda name: str(executable))
    monkeypatch.setattr("xswap.providers.codex.launcher.subprocess.call", Mock(return_value=0))
    monkeypatch.setattr("xswap.providers.codex.launcher.subprocess.run", Mock(return_value=subprocess.CompletedProcess([], 0)))
    monkeypatch.setattr("xswap.providers.codex.codex_cli.reconnect_wrapper", lambda manager: None)

    async def fake_serve(*args):
        return 0

    monkeypatch.setattr("xswap.providers.codex.codex_cli.serve_cli", fake_serve)
    if mode == "cli":
        manager.launch_cli("work", [], dry=dry)
    elif mode == "app":
        manager.launch_app("work", str(app), dry=dry)
    elif mode == "auto-cli":
        launch_cli(manager, "work,second", [], dry=dry)
    else:
        manager.launch_auto_app("work,second", str(app), dry=dry)
    assert (home / "hooks.json").exists() is (not dry)
    assert (home / "audit.config.toml").exists() is (not dry)
    if mode.startswith("auto"):
        runtime = manager.root / "auto" / ("cli-codex" if mode == "auto-cli" else "codex")
        assert (runtime / "audit.config.toml").exists() is (not dry)
        assert (runtime / "hooks.json").exists() is (not dry)


def test_external_runtime_uses_only_external_definitions(tooling_manager, tmp_path):
    from xswap.providers.codex.tooling import supply_runtime
    manager = tooling_manager
    external = tmp_path / "external"
    external.mkdir()
    atomic_json(external / "auth.json", {"tokens": {"access_token": "fixture"}})
    manager.register("external", external)
    (manager.source / "root.config.toml").write_text("root")
    (manager.source / "hooks.json").write_text("root hooks")
    (external / "custom.config.toml").write_text("custom")
    before = {p.name: p.read_bytes() for p in external.iterdir()}
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    supply_runtime(manager, "external", runtime)
    assert (runtime / "custom.config.toml").read_text() == "custom"
    assert not (runtime / "root.config.toml").exists()
    assert not (runtime / "hooks.json").exists()
    assert {p.name: p.read_bytes() for p in external.iterdir()} == before


def test_other_provider_gets_no_codex_definitions(tooling_manager, monkeypatch):
    from fakes.provider import FakeProvider
    manager = tooling_manager
    monkeypatch.setattr(manager, "provider", FakeProvider())
    (manager.source / "audit.config.toml").write_text("profile")
    (manager.source / "hooks.json").write_text("hooks")
    home = manager.prepare("work")
    manager.prepare("work")
    assert not (home / "audit.config.toml").exists()
    assert not (home / "hooks.json").exists()


@pytest.mark.parametrize("managed", [False, True])
def test_resume_refreshes_only_managed_original_home(tooling_manager, tmp_path, monkeypatch, managed):
    from xswap.providers.codex.codex_cli import launch_cli
    manager = tooling_manager
    manager.prepare("work")
    manager.prepare("second")
    if managed:
        original = manager.account("work")[1]
    else:
        original = tmp_path / "external"
        original.mkdir()
        atomic_json(original / "auth.json", {"tokens": {"access_token": "fixture"}})
        manager.register("external", original)
    (manager.source / "hooks.json").write_text("hooks")
    monkeypatch.setattr(manager, "codex", lambda: "/fixture/codex")
    monkeypatch.setattr("xswap.providers.codex.codex_cli.resume_home", lambda *args: original)
    monkeypatch.setattr("xswap.providers.codex.codex_cli.reconnect_wrapper", lambda manager: None)

    async def fake_serve(*args):
        assert (original / "hooks.json").exists() is managed
        return 0

    monkeypatch.setattr("xswap.providers.codex.codex_cli.serve_cli", fake_serve)
    assert launch_cli(manager, "work,second", ["resume", "--last"]) == 0
