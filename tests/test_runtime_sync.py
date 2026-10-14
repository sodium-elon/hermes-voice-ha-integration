import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('runtime_sync', Path(__file__).resolve().parents[1] / 'scripts/sync_installed_runtime.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture
def repository(tmp_path):
    root, home = tmp_path / 'source', tmp_path / 'home'
    root.mkdir()
    subprocess.run(['git', 'init', '-b', 'main'], cwd=root, check=True, capture_output=True)
    files = {'plugins/voice_stack/example.py': 'released plugin', 'skills/profiles/default/alexa-voice-replies/SKILL.md': 'default guidance', 'skills/profiles/sportscoach/alexa-voice-replies/SKILL.md': 'coach guidance'}
    for name, text in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    subprocess.run(['git', 'add', '.'], cwd=root, check=True)
    subprocess.run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'fixture'], cwd=root, check=True, capture_output=True)
    return root, home


def test_install_maps_distinct_profiles_and_preserves_unmanaged_files(repository):
    root, home = repository
    extra = home / 'plugins/voice_stack/local-backup.txt'
    extra.parent.mkdir(parents=True)
    extra.write_text('preserve')
    result = module.sync(root, home, install=True, bootstrap=True)
    assert result['verified'] and result['pluginRestartRequired']
    assert (home / 'skills/smart-home/alexa-voice-replies/SKILL.md').read_text() == 'default guidance'
    assert (home / 'profiles/sportscoach/skills/smart-home/alexa-voice-replies/SKILL.md').read_text() == 'coach guidance'
    assert extra.read_text() == 'preserve'


def test_verify_detects_installed_drift_and_install_preflights_before_writing(repository):
    root, home = repository
    module.sync(root, home, install=True, bootstrap=True)
    installed = home / 'profiles/sportscoach/skills/smart-home/alexa-voice-replies/SKILL.md'
    installed.write_text('uncommitted local lesson')
    with pytest.raises(ValueError, match='Installed drift'):
        module.sync(root, home)
    with pytest.raises(ValueError, match='unexpected installed edits'):
        module.sync(root, home, install=True)
    assert installed.read_text() == 'uncommitted local lesson'


def test_dirty_or_feature_source_cannot_be_installed(repository):
    root, home = repository
    (root / 'plugins/voice_stack/example.py').write_text('unreleased')
    with pytest.raises(ValueError, match='clean main'):
        module.sync(root, home, install=True, bootstrap=True)
    subprocess.run(['git', 'checkout', '--', '.'], cwd=root, check=True)
    subprocess.run(['git', 'switch', '-c', 'feature'], cwd=root, check=True, capture_output=True)
    with pytest.raises(ValueError, match='clean main'):
        module.sync(root, home, install=True, bootstrap=True)
    assert not home.exists()


def test_recorded_previous_deployment_allows_safe_upgrade_and_backup(repository):
    root, home = repository
    module.sync(root, home, install=True, bootstrap=True)
    target = root / 'plugins/voice_stack/example.py'
    target.write_text('next release')
    subprocess.run(['git', 'add', '.'], cwd=root, check=True)
    subprocess.run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'upgrade'], cwd=root, check=True, capture_output=True)
    result = module.sync(root, home, install=True)
    assert (Path(result['backup']) / 'plugins/voice_stack/example.py').read_text() == 'released plugin'
    assert module.sync(root, home)['verified']
    with pytest.raises(ValueError, match='first recorded deployment'):
        module.sync(root, home, install=True, bootstrap=True)
