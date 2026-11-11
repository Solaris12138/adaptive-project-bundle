#!/usr/bin/env python3
"""Check local prerequisites; optionally run a finite demo in a NEW --smoke directory."""
import argparse
import importlib
import json
import platform
from pathlib import Path
import sqlite3
import subprocess
import sys

sys.dont_write_bytecode = True
SCRIPTS = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--smoke', help='New directory under an existing parent; writes synthetic local evidence only')
    args = parser.parse_args()
    result = {'python': platform.python_version(), 'platform': platform.system(),
              'sqlite': sqlite3.sqlite_version, 'local_runtime_ok': False,
              'host_capabilities': {'subagents': 'not_probed', 'long_lived_chats': 'not_probed',
                                    'background_scheduler': 'not_probed', 'skill_discovery': 'not_probed'},
              'note': 'Agent must inspect actual host tools and authorization; local checks cannot grant these capabilities.'}
    try:
        if sys.version_info < (3, 8):
            raise ValueError('Python 3.8+ required')
        for name in ('runtime', 'audit', 'recovery', 'shutdown', 'evidence_time'):
            importlib.import_module(name)
        with sqlite3.connect(':memory:') as db:
            db.execute('CREATE TABLE probe (value TEXT)')
            db.execute('INSERT INTO probe VALUES (?)', ('portable',))
            if db.execute('SELECT value FROM probe').fetchone()[0] != 'portable':
                raise ValueError('SQLite transaction check failed')
        result['local_runtime_ok'] = True
        if args.smoke:
            target = Path(args.smoke).absolute()
            runs = []
            for script in ('demo.py', 'audit.py'):
                proc = subprocess.run([sys.executable, '-B', '-X', 'utf8', str(SCRIPTS / script),
                                       '--project', str(target)], cwd=str(SCRIPTS.parent),
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      encoding='utf-8', timeout=90)
                runs.append({'script': script, 'exit_code': proc.returncode,
                             'stdout': proc.stdout, 'stderr': proc.stderr})
                if proc.returncode:
                    result['smoke'] = runs
                    raise ValueError(script + ' failed; preserve the target for inspection')
            result['smoke'] = runs
            result['synthetic_smoke_passed'] = True
        result['status'] = 'passed'
        code = 0
    except Exception as error:
        result.update(status='failed', error=str(error))
        code = 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code


if __name__ == '__main__':
    raise SystemExit(main())

