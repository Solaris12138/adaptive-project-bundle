# -*- coding: utf-8 -*-
"""Read-only cold-start planning for local V4 projects; never grants work rights.

Python 3.8 standard library. Exit 0: an ordinary plan (including pause/wait/stop).
Exit 1: inspection, storage recovery, or reconciliation is required.
"""
import argparse
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time

# Importing this tool must not create bytecode alongside the installed skill.
sys.dont_write_bytecode = True


def sibling_module(name):
    spec = importlib.util.spec_from_file_location('adaptive_recovery_' + name, Path(__file__).resolve().with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_audit = sibling_module('audit')
_runtime = sibling_module('runtime')
AuditError, checked_path, no_sidecars, safe_read = _audit.AuditError, _audit.checked_path, _audit.no_sidecars, _audit.safe_read
Runtime, RuntimeErrorDetail = _runtime.Runtime, _runtime.RuntimeErrorDetail


def digest(data):
    return hashlib.sha256(data).hexdigest()


def flat_toml(data):
    """Accept the native flat scalar format; unsupported TOML fails closed.

This intentionally is not a general TOML parser. Quoted strings, native JSON
string arrays, booleans and decimal integers cover the local heartbeat files.
Unknown syntax, duplicate keys, sections and multiline strings are rejected.
"""
    values = {}
    decoder = json.JSONDecoder()
    for line in data.decode('utf-8-sig').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        match = re.match(r'^([A-Za-z0-9_-]+)\s*=\s*(.*)$', line)
        if not match or match.group(1) in values:
            raise ValueError('Unsupported or duplicate automation field')
        key, raw = match.groups()
        if raw.startswith(('"""', "'''")):
            raise ValueError('Multiline TOML strings are not supported')
        if raw.startswith(('"', '[')):
            value, end = decoder.raw_decode(raw)
            tail = raw[end:].strip()
            if tail and not tail.startswith('#'):
                raise ValueError('Unexpected automation value suffix')
            if not isinstance(value, str) and not (isinstance(value, list) and all(isinstance(x, str) for x in value)):
                raise ValueError('Unsupported automation value')
        elif raw.startswith("'"):
            end = raw.find("'", 1)
            if end < 0 or (raw[end + 1:].strip() and not raw[end + 1:].strip().startswith('#')):
                raise ValueError('Invalid literal automation string')
            value = raw[1:end]
        else:
            raw = raw.split('#', 1)[0].strip()
            if raw in ('true', 'false'):
                value = raw == 'true'
            elif re.fullmatch(r'-?(?:0|[1-9][0-9]*)', raw):
                value = int(raw)
            else:
                raise ValueError('Unsupported automation scalar')
        values[key] = value
    return values


def inspect_host(binding, mode, automation_root):
    if mode == 'foreground':
        return {'status': 'not_applicable', 'issues': []}
    if not binding:
        return {'status': 'unbound', 'issues': ['No persisted scheduler binding']}
    result = {'status': 'unsupported', 'automation_id': binding.get('automation_id'),
              'target_thread_id': binding.get('thread_id'), 'issues': []}
    if binding.get('host_id') != 'local':
        result['issues'].append('Only the local native host is supported')
        return result
    if not isinstance(binding.get('thread_id'), str) or not binding['thread_id'].strip():
        result['issues'].append('Persisted native target thread is missing')
        return result
    identity = binding.get('automation_id')
    if not isinstance(identity, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', identity):
        result['issues'].append('Unsafe or unsupported automation ID')
        return result
    root = Path(automation_root) if automation_root is not None else Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'automations'
    path = root / identity / 'automation.toml'
    result['configuration_path'] = str(path.absolute())
    try:
        # checked_path rejects symlinks/reparse points as well as path escape.
        checked_path(root)
        checked_path(path, regular=True)
        raw = safe_read(path)
        config = flat_toml(raw)
        result['configuration_sha256'] = digest(raw)
        result['observed'] = {k: config.get(k) for k in ['id', 'kind', 'target_thread_id', 'status', 'rrule']}
        if (type(config.get('version', 1)) is not int or config.get('version', 1) != 1 or
                config.get('kind') != 'heartbeat' or config.get('status') not in ('ACTIVE', 'PAUSED') or
                not isinstance(config.get('rrule'), str) or not config['rrule'].strip()):
            result['issues'].append('Unsupported native automation kind, status, or format version')
        elif config.get('id') != identity or config.get('target_thread_id') != binding.get('thread_id'):
            result['status'] = 'mismatch'
            result['issues'].append('Native ID or target differs from the persisted binding')
        else:
            result['status'] = config['status'].lower()
    except FileNotFoundError:
        result['status'] = 'missing'
        result['issues'].append('The bound native automation configuration is absent')
    except (OSError, ValueError, AuditError) as error:
        result['issues'].append(str(error))
    return result


def load_read_only(project):
    root = checked_path(project)
    if not root.is_dir():
        raise AuditError('not_initialized', 'Project directory is unavailable')
    database = checked_path(root / '.adaptive-v4/state.sqlite3', regular=True)
    no_sidecars(database)
    before = digest(safe_read(database))
    connection = sqlite3.connect(database.as_uri() + '?mode=ro&immutable=1', uri=True)
    try:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('PRAGMA trusted_schema=OFF')
        if connection.execute('PRAGMA journal_mode').fetchone()[0].lower() != 'delete':
            raise AuditError('unsupported_journal', 'Only the runtime default DELETE journal mode is supported')
        tables = dict(connection.execute("SELECT name,type FROM sqlite_master WHERE name IN ('project','requests','blobs')"))
        if tables != {'project': 'table', 'requests': 'table', 'blobs': 'table'}:
            raise AuditError('invalid_database', 'Expected the V4 runtime tables')
        rows = connection.execute('SELECT state FROM project WHERE id=1').fetchall()
        if len(rows) != 1:
            raise AuditError('invalid_state', 'Expected exactly one project state')
        state = json.loads(rows[0][0])
    finally:
        connection.close()
    runtime = Runtime(root)
    runtime.validate(state)
    return root, database, before, state, runtime


def inspect_recovery(project, automation_root=None):
    result = {'format_version': 1, 'read_only': True, 'automatic_actions': [],
              'decision': 'blocked', 'reason': 'inspection_incomplete', 'exit_code': 1,
              'next_actions': [], 'issues': [],
              'limitations': ['A recovery plan is not a wake or task lease; re-read and use original runtime transactions before work.',
                              'Only quiescent local V4 DELETE-journal projects and the native flat heartbeat configuration are supported.',
                              'Host files show configuration, not a running application, successful future trigger, or authenticated host receipt.',
                              'No automatic resume, replacement schedule, quota reset, deadline extension, or replay of missed ticks.',
                              'This is not a full artifact, provenance, semantic authorization, or physical power-loss durability audit.']}
    try:
        root, database, before, state, runtime = load_read_only(project)
        scheduler = state['scheduler']; exploration = state['exploration']; now = time.time()
        plan = runtime.plan_state(state)
        result.update(project_id=state['project_id'], revision=state['revision'], observed_at_unix=now,
                      database_sha256=before, runtime_plan=plan)
        result['preserved'] = copy.deepcopy({
            'control': state['control'], 'project_status': state['project_status'], 'execution_mode': state['execution_mode'],
            'binding': scheduler['binding'], 'scheduler_enabled': scheduler['enabled'],
            'scheduler_usage': scheduler['usage'], 'scheduler_budgets': scheduler['budgets'],
            'scheduler_budget_ceiling': scheduler['budget_ceiling'],
            'exploration_usage': exploration['usage'], 'exploration_budgets': exploration['budgets'],
            'exploration_budget_ceiling': exploration['budget_ceiling'],
            'expires_at': scheduler['expires_at'], 'not_before': scheduler['not_before'],
            'next_wake_at': scheduler['next_wake_at'], 'limits': state['limits']})
        active = scheduler['active_wake']
        result['wake'] = {'status': ('live' if active['expires_at'] > now else 'expired') if active else 'none',
                          'record': copy.deepcopy(active), 'reuse_credentials': False}
        result['tasks'] = {'live_leases': [], 'expired_leases': [], 'review': plan['review_tasks'], 'ready': plan['ready_tasks']}
        for task in state['tasks']:
            if task['lease']:
                key = 'live_leases' if task['lease']['expires_at'] > now else 'expired_leases'
                result['tasks'][key].append({'id': task['id'], 'revision': task['revision'], 'lease': copy.deepcopy(task['lease'])})
        result['outbox'] = {name: [] for name in ['pending', 'inflight', 'unknown', 'delivered']}
        for item in state['outbox']:
            if item['status'] not in result['outbox']:
                raise AuditError('invalid_state', 'Unknown outbox status')
            result['outbox'][item['status']].append(copy.deepcopy(item))
        result['checkpoints'] = copy.deepcopy(state['checkpoints'][-5:])
        result['memories'] = [{k: copy.deepcopy(m.get(k)) for k in ['id', 'revision', 'kind', 'status', 'sources', 'valid_until', 'used_by']} for m in state['memories']]
        result['exploration'] = copy.deepcopy(plan['exploration'])
        host = inspect_host(scheduler['binding'], state['execution_mode'], automation_root)
        result['host'] = host
        result['shutdown_intents'] = [{k: copy.deepcopy(i.get(k)) for k in
            ['id', 'status', 'closure_basis', 'prepared_revision', 'prepared_at_unix']}
            for i in state.get('shutdown_intents', [])]
        authority_ok = True
        for source in state['authority_sources']:
            path = runtime.path(source['ref'], file=True)
            if digest(safe_read(path)) != source['sha256']:
                authority_ok = False
                result['issues'].append({'code': 'stale_authority', 'ref': source['ref']})
        result['authority_sources_unchanged'] = authority_ok
        host_ok = host['status'] in {'active', 'paused', 'not_applicable'}
        ambiguous = result['outbox']['inflight'] + result['outbox']['unknown']
        terminal = plan['decision'] in {'completed', 'paused', 'expired', 'budget', 'disabled'}
        actions = result['next_actions']
        result['exit_code'] = 0 if host_ok and authority_ok and not ambiguous else 1
        if terminal:
            result.update(decision=plan['decision'], reason=plan['reason'])
            closure = runtime.shutdown_closure(state)
            actions.append({'code': 'keep_work_closed' if closure else 'close_business_admission',
                            'reason': closure or 'Wake budget or scheduler disable does not close worker admission'})
            if scheduler['binding']:
                actions.append({'code': 'inspect_shutdown_protocol',
                    'requires': 'Use shutdown.py; first globally close business, then persist shutdown.prepare, then pause the original native host, observe and shutdown.confirm. No new wake or user resume for pending cleanup.'})
        elif not authority_ok:
            result.update(decision='blocked', reason='stale_authority')
        elif not host_ok:
            result.update(decision='blocked', reason='host_' + host['status'])
        elif host['status'] == 'paused':
            result.update(decision='host_paused', reason='native_pause_preserved')
            actions.append({'code': 'keep_host_paused', 'requires': 'Explicit existing or new user authorization before any re-enable'})
        elif plan['decision'] in {'live', 'backoff'}:
            result.update(decision='wait', reason=plan['reason'])
            actions.append({'code': 'wait_for_wake_expiry' if plan['decision'] == 'live' else 'wait_until_due',
                            'until': active['expires_at'] if plan['decision'] == 'live' else plan['not_before'],
                            'requires': 'End this turn; use a later native heartbeat, do not steal or reuse old wake credentials'})
        elif ambiguous:
            result.update(decision='reconcile', reason='dispatch_result_uncertain')
        elif plan['decision'] == 'run':
            result.update(decision='ready', reason='original_runtime_may_admit_work')
            actions.append({'code': 'begin_new_wake' if state['execution_mode'] == 'background' else 'continue_foreground',
                            'requires': 'Re-read original state; use a new request and wake ID; admission may still be denied'})
            if result['tasks']['expired_leases']:
                actions.append({'code': 'recover_expired_tasks', 'task_ids': [t['id'] for t in result['tasks']['expired_leases']],
                                'requires': 'Fresh valid wake in background; original recover transaction, then fresh task.claim within remaining budget'})
            actions.append({'code': 'inspect_checkpoint_inputs', 'requires': 'Use persisted checkpoints, memory revisions and existing validation plans; do not restart paid validations'})
        else:
            result.update(decision='blocked', reason=plan['reason'], exit_code=1)
        if not host_ok:
            actions.append({'code': 'inspect_bound_host', 'requires': 'Query exact stored host/thread/automation; no replacement or automatic re-enable'})
        if ambiguous:
            actions.append({'code': 'reconcile_outbox', 'outbox_ids': [x['id'] for x in ambiguous],
                            'requires': 'Observe existing native result; outbox.resolve/reconcile only; never resend unknown/inflight intents'})
        no_sidecars(database)
        if digest(safe_read(database)) != before:
            raise AuditError('concurrent_change', 'State changed during inspection; discard this plan and retry')
    except (OSError, ValueError, TypeError, KeyError, IndexError, sqlite3.Error, RuntimeErrorDetail, AuditError) as error:
        code = getattr(error, 'code', 'inspection_error')
        result.update(decision='blocked', reason='storage_recovery_required' if code == 'busy_or_sidecar' else code, exit_code=1)
        result['issues'].append({'code': code, 'message': str(error)})
        result['next_actions'] = [{'code': 'recover_sqlite_storage' if code == 'busy_or_sidecar' else 'inspect_project',
                                   'requires': 'Do not delete SQLite sidecars or initialize a replacement project. After confirming original writers stopped and the original DELETE journal format, invoke runtime.py storage-recover --acknowledge-stopped-writers, then rerun this read-only inspection. Ordinary status may leave a cold journal.' if code == 'busy_or_sidecar' else 'Resolve the reported read/validation issue before work; no automatic mutation'}]
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True)
    parser.add_argument('--automation-root', help='Explicit native automations directory; isolated fixtures may override it')
    args = parser.parse_args(argv)
    result = inspect_recovery(args.project, args.automation_root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result['exit_code']


if __name__ == '__main__':
    raise SystemExit(main())
