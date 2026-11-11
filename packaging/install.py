#!/usr/bin/env python3
"""Verify or install this extracted release. Python 3.8+, standard library only."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import tempfile

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def plain(path):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise ValueError('Linked/reparse paths are not supported: ' + str(path))


def safe_ancestors(path):
    for p in [path] + list(path.parents):
        if os.path.lexists(str(p)):
            plain(p)


def inventory(root):
    safe_ancestors(root)
    result = {}
    for path in sorted(root.rglob('*')):
        plain(path)
        if path.is_file():
            result[path.relative_to(root).as_posix()] = digest(path)
        elif not path.is_dir():
            raise ValueError('Not an ordinary file or directory: ' + str(path))
    return result


def verify():
    data = json.loads((ROOT / 'SHA256SUMS.json').read_text(encoding='utf-8'))
    if data.get('format') != 1 or data.get('skill') != 'adaptive-project':
        raise ValueError('Unsupported release manifest')
    expected = data['files']
    if not isinstance(expected, dict) or not expected:
        raise ValueError('Empty release manifest')
    for ref, value in expected.items():
        p = PurePosixPath(ref)
        if p.is_absolute() or '..' in p.parts or '\\' in ref or ':' in ref or p.as_posix() != ref:
            raise ValueError('Unsafe manifest path: ' + ref)
        if len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
            raise ValueError('Invalid digest: ' + ref)
    actual = inventory(ROOT)
    actual.pop('SHA256SUMS.json', None)
    if expected != actual:
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        changed = sorted(k for k in set(expected) & set(actual) if expected[k] != actual[k])
        raise ValueError('Release mismatch: ' + json.dumps(dict(missing=missing, extra=extra, changed=changed)))
    if not (ROOT / 'adaptive-project/SKILL.md').is_file():
        raise ValueError('Missing skill entrypoint')
    return data


def install(skills_dir):
    data = verify()  # Verify every byte before making destination changes.
    parent = Path(skills_dir).expanduser().absolute()
    safe_ancestors(parent)
    destination = parent / 'adaptive-project'
    source = ROOT / 'adaptive-project'
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError('Source and install destination must be separate')
    expected = {k[len('adaptive-project/'):]: v for k, v in data['files'].items()
                if k.startswith('adaptive-project/')}
    if os.path.lexists(str(destination)):
        if not destination.is_dir() or inventory(destination) != expected:
            raise ValueError('Existing skill differs; nothing overwritten. Back it up outside skill discovery directories, then retry with an absent target.')
        return dict(status='already_installed', destination=str(destination), files=len(expected))
    parent.mkdir(parents=True, exist_ok=True)
    safe_ancestors(parent)
    # Private staging; an exclusive destination mkdir also refuses concurrent installs.
    staging = Path(tempfile.mkdtemp(prefix='.adaptive-install-', dir=str(parent)))
    try:
        shutil.copytree(str(source), str(staging / 'payload'))
        if inventory(staging / 'payload') != expected:
            raise ValueError('Staged copy verification failed')
        destination.mkdir(exist_ok=False)
        for child in (staging / 'payload').iterdir():
            shutil.move(str(child), str(destination / child.name))
        if inventory(destination) != expected:
            raise ValueError('Installed copy verification failed')
    finally:
        # Only our newly allocated private staging path is removed.
        shutil.rmtree(str(staging))
    return dict(status='installed', destination=str(destination), files=len(expected),
                host_discovery='not_checked', note='Open Codex and invoke $adaptive-project to verify discovery.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('verify')
    cmd = commands.add_parser('install')
    cmd.add_argument('--skills-dir', default=str(Path.home() / '.agents/skills'),
                     help='Parent skill directory, not the adaptive-project folder')
    args = parser.parse_args()
    try:
        if sys.version_info < (3, 8):
            raise ValueError('Python 3.8 or newer is required')
        if args.command == 'verify':
            release = verify()
            result = dict(status='verified', version=release['version'], files=len(release['files']))
        else:
            result = install(args.skills_dir)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as error:
        print(json.dumps(dict(status='error', message=str(error)), ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())

