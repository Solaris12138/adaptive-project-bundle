#!/usr/bin/env python3
"""Build a clean, deterministic portable skill ZIP. Never reads project state."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import stat
import zipfile

VERSION = '4.1.2'
NAME = 'adaptive-project-bundle'
ALLOWED = {'.md', '.py', '.json', '.yaml', '.ps1'}
DIRS = {'agents', 'assets', 'references', 'scripts', 'tests'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, help='adaptive-project skill directory')
    parser.add_argument('--output', required=True, help='New release output directory')
    args = parser.parse_args()
    source = Path(args.source).resolve()
    output = Path(args.output).absolute()
    if output == source or source in output.parents:
        raise ValueError('Output must be outside the source skill')
    selected = []
    for p in sorted(source.rglob('*')):
        rel = p.relative_to(source)
        if '__pycache__' in rel.parts:
            continue
        if any(re.search(r'(?:^|[_-])v\d|\d+\.\d+\.\d+', part, re.I) for part in rel.parts):
            raise ValueError('Versioned release filename: ' + str(rel))
        info = p.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Source links are not packaged: ' + str(rel))
        if p.is_dir():
            continue
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('Non-file source: ' + str(rel))
        if not (rel.as_posix() == 'SKILL.md' or rel.parts[0] in DIRS) or p.suffix not in ALLOWED:
            raise ValueError('Unexpected source file: ' + str(rel))
        selected.append((p, rel))
    if not (source / 'SKILL.md').is_file():
        raise ValueError('Missing skill')
    output.mkdir(parents=True, exist_ok=False)
    bundle = output / NAME
    bundle.mkdir()
    for p, rel in selected:
        target = bundle / 'adaptive-project' / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, target)
    for name in ('install.py', 'README.md'):
        shutil.copyfile(Path(__file__).resolve().parent / name, bundle / name)
    hashes = {p.relative_to(bundle).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted(bundle.rglob('*')) if p.is_file()}
    manifest = {'format': 1, 'skill': 'adaptive-project', 'version': VERSION,
                'python_minimum': '3.8', 'files': hashes}
    (bundle / 'SHA256SUMS.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    archive = output / 'adaptive-project.zip'
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as z:
        for p in sorted(bundle.rglob('*')):
            if p.is_file():
                item = zipfile.ZipInfo(NAME + '/' + p.relative_to(bundle).as_posix(), (2026, 11, 11, 0, 0, 0))
                item.compress_type = zipfile.ZIP_DEFLATED
                item.external_attr = 0o100644 << 16
                z.writestr(item, p.read_bytes())
    value = hashlib.sha256(archive.read_bytes()).hexdigest()
    (output / (archive.name + '.sha256')).write_text(value + '  ' + archive.name + '\n', encoding='utf-8')
    print(json.dumps({'archive': str(archive), 'sha256': value, 'files': len(hashes)}, ensure_ascii=False))


if __name__ == '__main__':
    main()

