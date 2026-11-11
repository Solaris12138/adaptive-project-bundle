# -*- coding: utf-8 -*-
"""Read-only diagnostics for quiescent, local Runtime 4.0 projects.

No runtime imports, writes, recovery, or automatic external actions. JSON stdout;
exit 0 = checked scope clean, 1 = actionable findings, 2 = incomplete/error.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path, PureWindowsPath
import sqlite3
import stat
import time


class AuditError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def checked_path(path, regular=False):
    """Reject links/reparse points in every existing component, before opening."""
    path = Path(os.path.abspath(str(path)))
    if str(path).startswith(('\\\\', '//')):
        raise AuditError('unsupported_path', 'Use a local project, not a UNC path')
    chain = list(reversed(path.parents)) + [path]
    for index, component in enumerate(chain):
        info = component.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise AuditError('unsafe_path', 'Symbolic links and Windows reparse points are not followed')
        if index < len(chain) - 1 and not stat.S_ISDIR(info.st_mode):
            raise AuditError('unsafe_path', 'Parent component is not a directory')
    if regular and (not stat.S_ISREG(info.st_mode) or info.st_nlink > 1):
        raise AuditError('unsafe_path', 'Expected an ordinary file with one hard link')
    return path


def safe_read(path):
    path = checked_path(path, regular=True)
    before = path.stat()
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(str(path), flags)
    with os.fdopen(descriptor, 'rb') as stream:
        opened = os.fstat(stream.fileno())
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise AuditError('concurrent_change', 'File changed while opening; stop writers and retry')
        content = stream.read()
        after = os.fstat(stream.fileno())
    checked_path(path, regular=True)
    final = path.stat()
    signatures = [(i.st_dev, i.st_ino, i.st_size, i.st_mtime_ns) for i in (before, opened, after, final)]
    if len(set(signatures)) != 1:
        raise AuditError('concurrent_change', 'File changed during read; stop writers and retry')
    return content


def accepted_path(root, reference):
    if not isinstance(reference, str) or not reference:
        raise AuditError('invalid_state', 'Accepted ref must be a nonempty relative path')
    normalized = reference.replace('\\', '/')
    parts = normalized.split('/')
    if (PureWindowsPath(reference).drive or normalized.startswith('/') or
            any(p in ('', '.', '..') or ':' in p for p in parts) or
            parts[:2] != ['.adaptive-v4', 'accepted'] or len(parts) < 4):
        raise AuditError('unsafe_path', 'Accepted ref must remain below .adaptive-v4/accepted')
    return root.joinpath(*parts)


def no_sidecars(database):
    for suffix in ('-wal', '-shm', '-journal'):
        if os.path.lexists(str(database) + suffix):
            raise AuditError('busy_or_sidecar', 'SQLite sidecar exists; stop writers and let the runtime close cleanly. Do not delete it to satisfy audit.')


def audit(project):
    result = {'format_version': 1, 'read_only': True, 'scope': 'V4 4.0 task lease expiry and accepted projections/bytes',
              'status': 'incomplete', 'issues': [], 'checks': {'tasks': 0, 'accepted': 0},
              'automatic_actions': [], 'limitations': ['Quiescent local projects only; no concurrent writers or hostile path replacement.',
                  'Not a full state/provenance/authorization audit; clean does not establish project completion or user effectiveness.']}
    try:
        root = checked_path(project)
        if not root.is_dir():
            raise AuditError('not_initialized', 'Project directory does not exist')
        database = checked_path(root / '.adaptive-v4/state.sqlite3', regular=True)
        no_sidecars(database)
        before = hashlib.sha256(safe_read(database)).hexdigest()
        # immutable avoids lock/journal creation. Sidecars are refused, and the
        # entire DB is hashed again below. Active writers are unsupported.
        con = sqlite3.connect(database.as_uri() + '?mode=ro&immutable=1', uri=True)
        try:
            con.execute('PRAGMA query_only=ON')
            con.execute('PRAGMA trusted_schema=OFF')
            if con.execute('PRAGMA journal_mode').fetchone()[0].lower() != 'delete':
                raise AuditError('unsupported_journal', 'Only the packaged runtime default DELETE journal mode is supported')
            tables = dict(con.execute("SELECT name,type FROM sqlite_master WHERE name IN ('project','blobs')"))
            if tables != {'project': 'table', 'blobs': 'table'}:
                raise AuditError('invalid_database', 'Expected project and blobs tables')
            rows = con.execute('SELECT state FROM project WHERE id=1').fetchall()
            if len(rows) != 1:
                raise AuditError('invalid_state', 'Expected one project state')
            state = json.loads(rows[0][0])
            if state.get('schema_version') != '4.0' or not isinstance(state.get('tasks'), list):
                raise AuditError('unsupported_schema', 'Only Runtime schema 4.0 is supported')
            result.update(project_id=state['project_id'], revision=state['revision'],
                          project_status=state['project_status'], control_status=state['control']['status'],
                          checked_at_unix=time.time(), database_sha256=before)
            now = result['checked_at_unix']
            for task in state['tasks']:
                task_id = task['id']
                result['checks']['tasks'] += 1
                lease = task.get('lease')
                if task['status'] == 'running':
                    if not isinstance(lease, dict) or type(lease.get('expires_at')) not in (int, float) or not math.isfinite(lease['expires_at']):
                        raise AuditError('invalid_state', 'Running task has invalid lease: ' + str(task_id))
                    if lease['expires_at'] <= now:
                        result['issues'].append({'code': 'expired_lease', 'task_id': task_id,
                            'lease_id': lease['lease_id'], 'attempt_id': lease['attempt_id'],
                            'fencing_token': lease['fencing_token'], 'expires_at': lease['expires_at'],
                            'suggested_operation': {'op': 'recover', 'task_id': task_id},
                            'guidance': 'Authorized coordinator/operator: inspect old attempt and process first, then use existing recover. Expiry does not terminate the old model.'})
                artifacts = task.get('accepted_artifacts')
                if not isinstance(artifacts, list):
                    raise AuditError('invalid_state', 'Task accepted_artifacts must be a list')
                for artifact in artifacts:
                    result['checks']['accepted'] += 1
                    context = {'task_id': task_id, 'file': artifact['ref'], 'blob_id': artifact['blob_id'], 'expected_sha256': artifact['sha256']}
                    canonical = con.execute('SELECT sha256,content FROM blobs WHERE id=?', (artifact['blob_id'],)).fetchone()
                    canonical_ok = canonical is not None and isinstance(canonical[1], bytes) and hashlib.sha256(canonical[1]).hexdigest() == canonical[0] == artifact['sha256']
                    if not canonical_ok:
                        result['issues'].append(dict(context, code='canonical_bytes_invalid', guidance='Database authoritative bytes are absent/inconsistent; preserve evidence and seek investigation. Do not assume artifact.restore can repair them.'))
                    try:
                        target = accepted_path(root, artifact['ref'])
                        observed = hashlib.sha256(safe_read(target)).hexdigest()
                        code = 'accepted_hash_mismatch' if observed != artifact['sha256'] else None
                    except FileNotFoundError:
                        observed, code = None, 'accepted_missing'
                    except (AuditError, OSError) as error:
                        result['issues'].append(dict(context, code=getattr(error, 'code', 'unreadable_file'), guidance=str(error)))
                        result['incomplete'] = True
                        continue
                    if code:
                        issue = dict(context, code=code, observed_sha256=observed)
                        if canonical_ok:
                            issue.update(suggested_operation={'op': 'artifact.restore', 'blob_id': artifact['blob_id']},
                                guidance='Authorized coordinator/operator: inspect the path, then restore this canonical projection using the existing runtime. Audit does not write it.')
                        else:
                            issue['guidance'] = 'Investigate invalid database bytes before attempting restoration.'
                        result['issues'].append(issue)
        finally:
            con.close()
        no_sidecars(database)
        after = hashlib.sha256(safe_read(database)).hexdigest()
        if before != after:
            raise AuditError('concurrent_change', 'Database changed during audit; results are incomplete, retry when quiescent')
        result['status'] = 'incomplete' if result.pop('incomplete', False) else ('findings' if result['issues'] else 'clean')
    except (AuditError, OSError, sqlite3.Error, ValueError, TypeError, KeyError, AttributeError) as error:
        result['status'] = 'incomplete'
        result['error'] = {'code': getattr(error, 'code', 'unreadable_or_invalid_project'), 'message': str(error)}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True, help='Existing quiescent local V4 project; never created by audit')
    args = parser.parse_args()
    result = audit(args.project)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return {'clean': 0, 'findings': 1, 'incomplete': 2}[result['status']]


if __name__ == '__main__':
    raise SystemExit(main())
