# -*- coding: utf-8 -*-
"""Create one finite synthetic foreground example in a NEW user-chosen directory."""
import argparse
import json
from pathlib import Path
import sys
sys.dont_write_bytecode = True
from audit import checked_path
from runtime import Runtime


def create_demo(destination):
    raw = Path(destination).absolute()
    checked_path(raw.parent)
    raw.mkdir(exist_ok=False)  # Existing files, directories and links are refused.
    (raw / 'inputs').mkdir()
    (raw / 'inputs/authorization.md').write_text(
        'SYNTHETIC LOCAL DEMO ONLY. The explicit demo command permits this new directory only. '
        'No host chats, schedules, purchases, publishing or external messages are authorized.', encoding='utf-8')
    charter = {'project_id': 'synthetic-local-demo', 'objective': 'Write and accept one synthetic local text artifact.',
        'acceptance': ['A synthetic text artifact is accepted; no real user effects claimed.'],
        'scope': {'project_root': '.', 'write_roots': ['.']},
        'authority': {'status': 'bound', 'source_refs': ['inputs/authorization.md'],
            'adjust_units_within_scope': False, 'maintain_project_memory': False,
            'create_long_lived_chats': False, 'message_long_lived_chats': False, 'configure_future_triggers': False},
        'execution': {'state_writer_id': 'demo-coordinator', 'operator_id': 'demo-operator', 'mode': 'foreground'},
        'scheduler': {'budgets': {'max_claims': 4, 'max_dispatches': 1}},
        'exploration': {'budgets': {'max_candidates': 1, 'max_validations': 1, 'max_cost': 1}}}
    runtime = Runtime(raw)
    logs = raw / 'demo-receipts'
    logs.mkdir()
    def save(name, value):
        (logs / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    save('charter.json', charter)
    save('init.json', runtime.init(charter))
    def transact(label, operation):
        request = {'actor_id': 'demo-coordinator', 'idempotency_key': label, 'reason': 'Finite synthetic local example', 'operations': [operation]}
        save(label + '.request.json', request)
        result = runtime.transact(request)
        save(label + '.receipt.json', result)
        return result
    transact('create', {'op': 'task.create', 'id': 'demo-text', 'title': 'Synthetic text', 'owner_unit_id': 'coordinator', 'acceptance': ['Exact bytes synthetic local demo plus LF']})
    claim = transact('claim', {'op': 'task.claim', 'id': 'demo-text', 'expected_task_revision': 1, 'lease_seconds': 60})['results'][0]
    file = raw / claim['attempt_dir'] / 'result.txt'
    file.write_bytes(b'synthetic local demo\n')
    ref = file.relative_to(raw).as_posix()
    transact('submit', {'op': 'task.submit', 'id': 'demo-text', 'expected_task_revision': claim['task_revision'], 'lease_id': claim['lease_id'], 'fencing_token': claim['fencing_token'], 'artifacts': [ref], 'summary': 'Synthetic local artifact, not a real user study'})
    (raw / 'check.txt').write_text('Checked expected text bytes locally; synthetic self-check only.', encoding='utf-8')
    assert file.read_bytes() == b'synthetic local demo\n'
    state = transact('review', {'op': 'task.review', 'id': 'demo-text', 'expected_task_revision': 3, 'verdict': 'pass', 'evidence': ['check.txt'], 'method': 'Compare exact synthetic bytes'})['state']
    return {'project': str(raw), 'synthetic': True, 'task_status': state['tasks'][0]['status'],
            'accepted': state['tasks'][0]['accepted_artifacts'], 'project_completed': False,
            'next': 'Run audit.py --project with this directory. No host services were created.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(create_demo(args.project), ensure_ascii=False, indent=2))
    except Exception as error:
        print(json.dumps({'error': str(error), 'note': 'Existing targets are never overwritten. Preserve any new partial demo directory for inspection.'}, ensure_ascii=False))
        raise SystemExit(2)
