# -*- coding: utf-8 -*-
"""Observable local audit checks; all generated files stay under a NEW --output."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import time
import traceback
sys.dont_write_bytecode = True
SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
from audit import AuditError, accepted_path, checked_path
from runtime import Runtime


def digest(data):
    return hashlib.sha256(data).hexdigest()


def inventory(root):
    result = []
    if not os.path.lexists(str(root)):
        return {'exists': False, 'entries': []}
    def walk(path):
        info = path.lstat()
        link = stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)
        row = {'ref': path.relative_to(root).as_posix(), 'mode': info.st_mode, 'mtime_ns': info.st_mtime_ns, 'size': info.st_size, 'link': link}
        if link:
            row['target'] = os.readlink(str(path))
        elif path.is_file():
            row['sha256'] = digest(path.read_bytes())
        result.append(row)
        if not link and path.is_dir():
            for item in sorted(path.iterdir()):
                walk(item)
    walk(root)
    db = root / '.adaptive-v4/state.sqlite3'
    state_hash = None
    if db.is_file() and not any(x['link'] for x in result):
        con = sqlite3.connect(db.as_uri() + '?mode=ro&immutable=1', uri=True)
        try:
            state_hash = digest(con.execute('SELECT state FROM project WHERE id=1').fetchone()[0].encode('utf-8'))
        finally:
            con.close()
    return {'exists': True, 'entries': result, 'state_sha256': state_hash}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    output = Path(args.output).absolute()
    checked_path(output.parent)
    output.mkdir(exist_ok=False)
    logs = output / 'logs'
    logs.mkdir()
    cases, commands = [], []
    def save(path, data):
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    def run(label, argv, expected):
        start = time.time()
        process = subprocess.run(argv, cwd=str(output), stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8', errors='replace')
        (logs / (label + '.stdout.txt')).write_text(process.stdout, encoding='utf-8')
        (logs / (label + '.stderr.txt')).write_text(process.stderr, encoding='utf-8')
        commands.append({'label': label, 'argv': argv, 'cwd': str(output), 'started_unix': start, 'ended_unix': time.time(), 'returncode': process.returncode})
        save(output / 'commands.json', commands)
        assert process.returncode == expected, (label, expected, process.returncode, process.stdout, process.stderr)
        return process
    def audit_check(label, project, expected, codes):
        before = inventory(project)
        save(logs / (label + '.before.json'), before)
        process = run(label, [sys.executable, '-B', '-X', 'utf8', str(SCRIPTS / 'audit.py'), '--project', str(project)], expected)
        after = inventory(project)
        save(logs / (label + '.after.json'), after)
        assert before == after, label + ': audit mutated database/state or inventory'
        data = json.loads(process.stdout)
        found = [issue['code'] for issue in data['issues']]
        assert set(codes).issubset(found), (label, codes, found)
        assert data['automatic_actions'] == [] and data['read_only'] is True
        if expected == 0:
            assert data['status'] == 'clean' and not found
        cases.append({'name': label, 'passed': True, 'exit': expected, 'codes': found, 'project_unchanged': True})
        return data
    def tx(runtime, label, operation):
        request = {'actor_id': 'demo-coordinator', 'idempotency_key': label, 'reason': 'Synthetic boundary fixture using runtime transactions', 'operations': [operation]}
        save(logs / (label + '.request.json'), request)
        receipt = runtime.transact(request)
        save(logs / (label + '.receipt.json'), receipt)
        return receipt
    try:
        project = output / 'healthy-project'
        run('demo-create', [sys.executable, '-B', '-X', 'utf8', str(SCRIPTS / 'demo.py'), '--project', str(project)], 0)
        audit_check('healthy', project, 0, [])
        before = inventory(project)
        run('demo-existing-refused', [sys.executable, '-B', '-X', 'utf8', str(SCRIPTS / 'demo.py'), '--project', str(project)], 2)
        assert before == inventory(project)
        cases.append({'name': 'existing-demo-refused', 'passed': True, 'project_unchanged': True})
        runtime = Runtime(project)
        tx(runtime, 'create-expiring', {'op': 'task.create', 'id': 'expiring-task', 'title': 'Natural expiry fixture', 'owner_unit_id': 'coordinator', 'acceptance': ['Synthetic only']})
        claim = tx(runtime, 'claim-expiring', {'op': 'task.claim', 'id': 'expiring-task', 'expected_task_revision': 1, 'lease_seconds': 0.2})['results'][0]
        time.sleep(0.3)
        expired = audit_check('natural-expired', project, 1, ['expired_lease'])
        issue = next(i for i in expired['issues'] if i['code'] == 'expired_lease')
        assert issue['task_id'] == 'expiring-task' and issue['lease_id'] == claim['lease_id']
        assert issue['suggested_operation'] == {'op': 'recover', 'task_id': 'expiring-task'}
        tx(runtime, 'recover', issue['suggested_operation'])
        audit_check('after-recover', project, 0, [])
        artifact = runtime.read()['tasks'][0]['accepted_artifacts'][0]
        path = project / artifact['ref']
        path.write_bytes(b'controlled synthetic corruption\n')
        corrupt = audit_check('accepted-corrupt', project, 1, ['accepted_hash_mismatch'])
        issue = next(i for i in corrupt['issues'] if i['code'] == 'accepted_hash_mismatch')
        assert issue['task_id'] == 'demo-text' and issue['file'] == artifact['ref'] and issue['blob_id'] == artifact['blob_id']
        tx(runtime, 'restore-corrupt', issue['suggested_operation'])
        audit_check('after-corrupt-restore', project, 0, [])
        path.unlink()  # Only the explicitly created synthetic accepted projection.
        missing = audit_check('accepted-missing', project, 1, ['accepted_missing'])
        issue = next(i for i in missing['issues'] if i['code'] == 'accepted_missing')
        assert issue['blob_id'] == artifact['blob_id']
        tx(runtime, 'restore-missing', issue['suggested_operation'])
        audit_check('after-missing-restore', project, 0, [])
        absent_parent = output / 'absent-parent'
        absent_parent.mkdir()
        parent_before = inventory(absent_parent)
        audit_check('absent-project', absent_parent / 'never-created', 2, [])
        assert parent_before == inventory(absent_parent)
        empty = output / 'empty-project'
        empty.mkdir()
        audit_check('uninitialized-project', empty, 2, [])
        for reference in ['../../outside-secret.txt', '/outside', 'C:/outside', 'C:outside', '.adaptive-v4/accepted/../../outside', '.adaptive-v4/accepted/a/file:stream', '.adaptive-v4/accepted/a/../file']:
            try:
                accepted_path(project, reference)
            except AuditError as error:
                assert error.code == 'unsafe_path'
            else:
                raise AssertionError('Unsafe reference accepted: ' + reference)
        cases.append({'name': 'reference-boundaries', 'passed': True, 'kind': 'direct path helper checks; no state mutation'})
        outside = output / 'external-to-project'
        outside.mkdir()
        secret = outside / 'outside-secret.txt'
        secret.write_bytes(b'OUTSIDE-CONTENT-MUST-NOT-BE-READ-IN-AUDIT')
        parent = path.parent
        backup = parent.with_name(parent.name + '-preserved')
        parent.rename(backup)  # Both paths belong to this synthetic fixture.
        link_available = False
        if os.name == 'nt':
            proc = subprocess.run(['cmd.exe', '/c', 'mklink', '/J', str(parent), str(outside)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            save(logs / 'junction-create.json', {'argv': ['cmd.exe', '/c', 'mklink', '/J', str(parent), str(outside)], 'returncode': proc.returncode, 'stdout_hex': proc.stdout.hex(), 'stderr_hex': proc.stderr.hex()})
            link_available = proc.returncode == 0
        else:
            try:
                os.symlink(str(outside), str(parent), target_is_directory=True)
                link_available = True
            except OSError as error:
                save(logs / 'symlink-unavailable.json', {'error': str(error)})
        if link_available:
            outside_before = inventory(outside)
            unsafe = audit_check('accepted-parent-link', project, 2, ['unsafe_path'])
            assert 'OUTSIDE-CONTENT' not in json.dumps(unsafe)
            assert outside_before == inventory(outside)
            cases.append({'name': 'outside-link-target-unchanged', 'passed': True})
        else:
            cases.append({'name': 'accepted-parent-link', 'skipped': True, 'reason': 'Host cannot create a test link; no passing claim'})
        sidecar_project = output / 'sidecar-project'
        run('sidecar-demo-create', [sys.executable, '-B', '-X', 'utf8', str(SCRIPTS / 'demo.py'), '--project', str(sidecar_project)], 0)
        (sidecar_project / '.adaptive-v4/state.sqlite3-wal').write_bytes(b'synthetic sidecar presence marker')
        sidecar = audit_check('sidecar-refused', sidecar_project, 2, [])
        assert sidecar['error']['code'] == 'busy_or_sidecar'
        result = {'passed': True, 'cases': cases, 'command_count': len(commands), 'synthetic': True, 'real_user_effects_measured': False}
        save(output / 'results.json', result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception:
        failure = traceback.format_exc()
        save(output / 'results.json', {'passed': False, 'cases': cases, 'failure': failure})
        print(failure, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
