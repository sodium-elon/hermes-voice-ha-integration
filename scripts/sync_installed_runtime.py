"""Synchronize the source-owned voice plugins and profile-specific Alexa skills."""
import argparse
import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def managed_files(root, home):
    names = subprocess.check_output(['git', 'ls-files', '--', 'plugins/home_assistant', 'plugins/voice_stack', 'skills/profiles'], cwd=root, text=True).splitlines()
    pairs = []
    for name in names:
        parts = Path(name).parts
        if parts[0] == 'plugins':
            target = home / name
        elif len(parts) >= 5 and parts[3] == 'alexa-voice-replies':
            profile = parts[2]
            base = home if profile == 'default' else home / 'profiles' / profile
            target = base / 'skills' / 'smart-home' / Path(*parts[3:])
        else:
            continue
        pairs.append((root / name, target))
    return pairs


def sync(root, home, *, install=False, bootstrap=False):
    root, home = Path(root).resolve(), Path(home).resolve()
    branch = subprocess.check_output(['git', 'branch', '--show-current'], cwd=root, text=True).strip()
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
    manifest_path = home / '.voice-source-sync.json'
    previous = json.loads(manifest_path.read_text()) if manifest_path.exists() else None
    if install:
        if branch != 'main' or subprocess.check_output(['git', 'status', '--porcelain'], cwd=root, text=True).strip():
            raise ValueError('Install requires a clean main checkout')
        if bootstrap and previous:
            raise ValueError('Bootstrap is only allowed for the first recorded deployment')
        if not previous and not bootstrap:
            raise ValueError('First deployment requires an explicit --bootstrap after reviewing installed drift')
    pairs = managed_files(root, home)
    if not pairs:
        raise ValueError('No managed source files found')
    expected, changes = {}, []
    for source, target in pairs:
        relative = str(target.relative_to(home))
        if target.is_symlink() or not target.resolve().is_relative_to(home):
            raise ValueError('Managed target must remain inside the Hermes home: ' + relative)
        expected[relative] = digest(source)
        current = digest(target)
        if current == expected[relative]:
            continue
        if install and previous and current is not None and current != previous['files'].get(relative):
            raise ValueError('Refusing unexpected installed edits: ' + relative)
        changes.append((source, target, relative))
    if not install:
        if changes:
            raise ValueError('Installed drift: ' + ', '.join(item[2] for item in changes))
        return {'verified': True, 'sourceCommit': commit, 'files': len(expected)}
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    backup = home / 'backups' / 'voice-source-sync' / stamp
    for source, target, relative in changes:
        if target.exists():
            saved = backup / relative
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, saved)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    manifest = {'repository': 'sodium-elon/hermes-voice-ha-integration', 'sourceCommit': commit, 'files': expected, 'installedAt': stamp}
    home.mkdir(parents=True, exist_ok=True)
    temporary = manifest_path.with_suffix('.tmp')
    temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
    temporary.replace(manifest_path)
    verified = sync(root, home)
    return {**verified, 'changed': [item[2] for item in changes], 'pluginRestartRequired': any(item[2].startswith('plugins/') for item in changes), 'backup': str(backup) if changes else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--verify', action='store_true')
    mode.add_argument('--install', action='store_true')
    parser.add_argument('--bootstrap', action='store_true')
    parser.add_argument('--hermes-home', type=Path, default=Path.home() / '.hermes')
    args = parser.parse_args()
    try:
        result = sync(Path(__file__).resolve().parents[1], args.hermes_home, install=args.install, bootstrap=args.bootstrap)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, str(error) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
