# -*- coding: utf-8 -*-
"""Read-only V4 shutdown planning. Does not pause, delete, confirm, or resume."""
import argparse
import copy
import importlib.util
import json
import math
from pathlib import Path
import sqlite3
import sys
import time

sys.dont_write_bytecode = True
_spec = importlib.util.spec_from_file_location('adaptive_shutdown_recovery', Path(__file__).resolve().with_name('recovery.py'))
_recovery = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_recovery)


def inspect_shutdown(project, automation_root=None):
    result = {'format_version': 1, 'read_only': True, 'automatic_actions': [],
              'decision': 'blocked', 'reason': 'inspection_incomplete', 'exit_code': 1,
              'next_actions': [], 'issues': [],
              'limitations': ['Native configuration is a local observation, not authenticated host-tool evidence.',
                              'Confirmation records caller-supplied evidence; audit the actual native tool timeline independently.',
                              'No new wake, quota reset, user resume, host mutation, or replacement binding is performed.']}
    try:
        root, database, before, state, runtime = _recovery.load_read_only(project)
        runtime.unchanged(state['authority_sources'])
        scheduler = state['scheduler']
        closure = runtime.shutdown_closure(state)
        intents = state.get('shutdown_intents', [])
        current = [i for i in intents if runtime.shutdown_current(state, i)]
        if len(current) > 1:
            raise _recovery.AuditError('shutdown_ambiguous', 'Multiple current shutdown intents')
        intent = current[0] if current else None
        result.update(project_id=state['project_id'], revision=state['revision'],
                      observed_at_unix=time.time(), database_sha256=before,
                      closure_basis=closure, intent=copy.deepcopy(intent),
                      stale_intent_ids=[i['id'] for i in intents if i not in current],
                      preserved={'control': copy.deepcopy(state['control']),
                                 'binding': copy.deepcopy(scheduler['binding']),
                                 'scheduler_usage': copy.deepcopy(scheduler['usage']),
                                 'scheduler_budgets': copy.deepcopy(scheduler['budgets']),
                                 'exploration_usage': copy.deepcopy(state['exploration']['usage']),
                                 'exploration_budgets': copy.deepcopy(state['exploration']['budgets']),
                                 'expires_at': scheduler['expires_at']})
        host = _recovery.inspect_host(scheduler['binding'], state['execution_mode'], automation_root)
        result['host'] = host
        result['host_pause_order'] = 'order_unproven'
        result['strict_order_proven'] = False
        if host['status'] in {'active', 'paused'}:
            raw = _recovery.safe_read(Path(host['configuration_path']))
            if _recovery.digest(raw) != host['configuration_sha256']:
                raise _recovery.AuditError('concurrent_change', 'Host configuration changed during inspection')
            config = _recovery.flat_toml(raw)
            updated = config.get('updated_at')
            # Native automation timestamps are epoch milliseconds; retain the raw value too.
            host['configuration_updated_at'] = updated
            if type(updated) in (int, float) and math.isfinite(updated) and updated >= 0:
                host['configuration_updated_at_unix'] = updated / 1000.0
                if intent and host['status'] == 'paused':
                    result['host_pause_order'] = ('after_prepare' if updated / 1000.0 >= intent['prepared_at_unix']
                                                  else 'preexisting_host_pause')
            if not intent and host['status'] == 'paused':
                result['host_pause_order'] = 'preexisting_host_pause'
        if intent:
            runtime.unchanged(intent['evidence'])
            if intent['status'] == 'confirmed': runtime.unchanged(intent['confirmation']['evidence'])
        if not closure:
            result.update(decision='business_open', reason='business_admission_not_closed')
            result['next_actions'] = [{'code': 'close_business_admission',
                'requires': 'Finish authorized acceptance/export then project.complete, or operator control.pause; host pause is not a closure proof.'}]
        elif not scheduler['binding'] or state['execution_mode'] != 'background':
            result.update(reason='shutdown_unbound')
        elif intent and intent['status'] == 'confirmed':
            # A missing host may be legitimate post-confirmation deletion; do not fabricate its receipt.
            result.update(decision='confirmed', reason='durable_confirmation_present',
                          exit_code=0 if host['status'] == 'paused' else 1)
            result['next_actions'] = [{'code': 'audit_host_cleanup',
                'requires': 'If native policy requires deletion, delete only this bound schedule and independently retain its receipt; absent config alone is not authenticated proof.'}]
        elif host['status'] not in {'active', 'paused'}:
            result.update(reason='host_' + host['status'])
            result['next_actions'] = [{'code': 'inspect_bound_host',
                'requires': 'Pending shutdown cannot be confirmed from a missing, mismatched, or unsupported native host.'}]
        elif not intent:
            result.update(decision='prepare', reason='business_closed_without_shutdown_intent', exit_code=0)
            result['next_actions'] = [{'code': 'shutdown.prepare',
                'requires': 'Persist a fresh standalone prepare transaction before any native host pause; retain an earlier host pause as an ordering defect.'}]
        elif host['status'] == 'active':
            result.update(decision='pause_host', reason='prepared_original_host_active', exit_code=0)
            result['next_actions'] = [{'code': 'pause_bound_host', 'automation_id': intent['binding']['automation_id'],
                'requires': 'Re-read this intent and original binding immediately before native PAUSED; retain tool result and observation timestamps.'}]
        elif result['host_pause_order'] == 'preexisting_host_pause':
            result.update(reason='preexisting_host_pause')
            result['next_actions'] = [{'code': 'audit_shutdown_order',
                'requires': 'Do not enable the host to fabricate order. Retain the preexisting pause and independently report the strict ordering defect.'}]
        else:
            result.update(decision='confirm', reason='prepared_original_host_observed_paused', exit_code=0)
            result['next_actions'] = [{'code': 'shutdown.confirm',
                'requires': 'Record a post-prepare native observation and immutable local evidence; verify the actual pause action followed prepare, not just this observation.'}]
        if host['status'] not in {'active', 'paused'}:
            result['issues'].append({'code': 'host_' + host['status'], 'details': host['issues']})
        _recovery.no_sidecars(database)
        if _recovery.digest(_recovery.safe_read(database)) != before:
            raise _recovery.AuditError('concurrent_change', 'State changed during shutdown inspection; retry')
    except (OSError, ValueError, TypeError, KeyError, IndexError, sqlite3.Error,
            _recovery.RuntimeErrorDetail, _recovery.AuditError) as error:
        code = getattr(error, 'code', 'inspection_error')
        result.update(decision='blocked', reason=code, exit_code=1, next_actions=[])
        result['issues'].append({'code': code, 'message': str(error)})
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True)
    parser.add_argument('--automation-root')
    args = parser.parse_args(argv)
    result = inspect_shutdown(args.project, args.automation_root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result['exit_code']


if __name__ == '__main__':
    sys.exit(main())
