"""Independent behavior checks for bounded, direct department collaboration.

All host IDs and receipts are synthetic fixtures. These tests never call a host.
"""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'adaptive-project/scripts/runtime.py'
spec = importlib.util.spec_from_file_location('department_runtime_tests', SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
Runtime = module.Runtime
RuntimeErrorDetail = module.RuntimeErrorDetail


class DepartmentCollaborationTests(unittest.TestCase):
    CAPABILITIES = ['unit.create', 'chat.bind', 'task.create', 'task.review',
                    'recover', 'outbox.enqueue', 'outbox.begin']

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='adaptive-departments-')
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.sequence = 0
        self.write('inputs/authority.txt', 'Local synthetic tests only; no host actions authorized.')
        self.write('evidence/delegation.txt', 'Delegated responsibilities within the fixture project.')
        self.write('evidence/review.txt', 'Observed file content meets the fixture acceptance.')
        self.runtime = Runtime(self.project)
        self.runtime.init({
            'project_id': 'department-test', 'objective': 'Deliver and independently review a local result',
            'acceptance': ['Reviewed result delivered'],
            'scope': {'project_root': str(self.project), 'write_roots': [str(self.project)]},
            'authority': {'status': 'bound', 'source_refs': ['inputs/authority.txt'],
                          'adjust_units_within_scope': True, 'maintain_project_memory': True,
                          'create_long_lived_chats': True, 'message_long_lived_chats': True},
            'execution': {'state_writer_id': 'coordinator', 'operator_id': 'operator', 'mode': 'foreground'},
            'limits': {'max_nonretired_units': 8, 'max_active_chats': 8,
                       'max_running_leases': 3, 'max_unit_depth': 3},
            'scheduler': {'budgets': {'max_wakes': 10, 'max_claims': 12, 'max_dispatches': 10,
                                     'max_actions_per_wake': 20, 'max_wake_seconds': 60},
                          'expires_at': time.time() + 3600},
        })
        self.tx(*(self.unit(uid) for uid in ['build', 'quality', 'unrelated']))
        self.tx(*(dict(op='chat.bind', id=cid, unit_id=uid, host_id='fixture-host', thread_id='fixture-'+cid)
                  for cid, uid in [('build-lead', 'build'), ('build-worker', 'build'),
                                   ('quality-lead', 'quality'), ('quality-worker', 'quality'),
                                   ('unrelated-worker', 'unrelated')]))

    def write(self, relative, contents):
        path = self.project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding='utf-8')
        return path.relative_to(self.project).as_posix()

    def state(self):
        return self.runtime.read()

    def item(self, collection, identity):
        return next(item for item in self.state()[collection] if item['id'] == identity)

    def request(self, *ops, actor='coordinator'):
        self.sequence += 1
        return {'actor_id': actor, 'idempotency_key': 'department-'+str(self.sequence),
                'reason': 'Independent bounded collaboration test', 'operations': list(ops)}

    def tx(self, *ops, actor='coordinator'):
        return self.runtime.transact(self.request(*ops, actor=actor))

    def denied(self, request, code=None):
        before = self.state()
        with self.assertRaises(RuntimeErrorDetail) as error:
            self.runtime.transact(request)
        if code:
            self.assertEqual(error.exception.code, code)
        self.assertEqual(before, self.state(), 'A rejected request must leave durable state unchanged')

    def unit(self, identity, parent='coordinator'):
        return dict(op='unit.create', id=identity, parent_id=parent, name=identity,
                    mission='Own '+identity+' delivery', creation_reason='Independent work requires this responsibility',
                    exit_condition='Assigned delivery is complete')

    def task(self, identity='draft', owner='build', reviewer=None, dependencies=None):
        result = dict(op='task.create', id=identity, owner_unit_id=owner, title='Deliver '+identity,
                      acceptance=['Artifact contains the required fixture result'], inputs=[],
                      dependencies=dependencies or [])
        if reviewer is not None:
            result['reviewer_unit_id'] = reviewer
        return result

    def grant(self, chat='build-lead', peer=None, tasks=8, units=2, dispatches=5, capabilities=None):
        return self.tx(dict(op='delegation.grant', id='grant-'+chat, chat_id=chat,
                            capabilities=self.CAPABILITIES if capabilities is None else capabilities,
                            peer_unit_ids=peer if peer is not None else ['quality'],
                            limits={'max_tasks': tasks, 'max_units': units, 'max_dispatches': dispatches},
                            evidence=['evidence/delegation.txt']))

    def message(self, identity, target='quality-lead', task='draft'):
        return dict(op='outbox.enqueue', id=identity, kind='send_message',
                    payload={'chat_id': target, 'task_id': task,
                             'summary': 'Please inspect the referenced delivery and return evidence.',
                             'refs': ['evidence/review.txt']})

    def begin(self, identity, actor='build-lead'):
        return self.tx(dict(op='outbox.begin', id=identity,
                            expected_outbox_revision=self.item('outbox', identity)['revision']), actor=actor)

    def resolve(self, identity, actor='build-lead', status='delivered'):
        return self.tx(dict(op='outbox.resolve', id=identity,
                            expected_outbox_revision=self.item('outbox', identity)['revision'],
                            status=status, result={'fixture_receipt': identity}), actor=actor)

    def submit(self, task='draft', actor='build-worker'):
        current = self.item('tasks', task)
        lease = self.tx(dict(op='task.claim', id=task, expected_task_revision=current['revision']), actor=actor)['results'][0]
        ref = self.write(lease['attempt_dir']+'/result.txt', 'Required fixture result.')
        operation = dict(op='task.submit', id=task, expected_task_revision=lease['task_revision'],
                         lease_id=lease['lease_id'], fencing_token=lease['fencing_token'],
                         artifacts=[ref], summary='Produced the required result.')
        self.tx(operation, actor=actor)
        return ref

    def review(self, task='draft'):
        return dict(op='task.review', id=task, expected_task_revision=self.item('tasks', task)['revision'],
                    verdict='pass', method='Read the artifact and compare the stated criterion',
                    evidence=['evidence/review.txt'])

    def parallel(self, requests):
        barrier = threading.Barrier(len(requests))
        def apply(request):
            runtime = Runtime(self.project)
            barrier.wait(timeout=10)
            try:
                return {'ok': True, 'result': runtime.transact(request)}
            except RuntimeErrorDetail as error:
                return {'ok': False, 'code': error.code}
        with ThreadPoolExecutor(max_workers=len(requests)) as executor:
            return list(executor.map(apply, requests))

    def test_department_builds_child_assigns_work_and_binds_executor(self):
        self.grant()
        self.tx(self.unit('frontend', 'build'), self.task('component', 'frontend'), actor='build-lead')
        self.tx(dict(op='chat.bind', id='frontend-worker', unit_id='frontend',
                     host_id='fixture-host', thread_id='fixture-frontend'), actor='build-lead')
        task = self.item('tasks', 'component')
        lease = self.tx(dict(op='task.claim', id='component', expected_task_revision=task['revision']),
                        actor='frontend-worker')['results'][0]
        self.assertEqual(lease['task_id'], 'component')
        self.assertEqual(self.item('delegations', 'grant-build-lead')['usage'],
                         {'tasks': 1, 'units': 1, 'dispatches': 0})

    def test_peer_reviews_without_coordinator_and_releases_dependency(self):
        self.grant()
        self.grant('quality-lead', peer=['build'])
        start = len(self.state()['events'])
        self.tx(self.task(reviewer='quality'), actor='build-lead')
        self.tx(self.task('package', 'quality', dependencies=['draft']), actor='quality-lead')
        self.denied(self.request(dict(op='task.claim', id='package', expected_task_revision=1),
                                 actor='quality-worker'), 'dependency_not_ready')
        self.submit()
        self.denied(self.request(self.review(), actor='build-lead'), 'unauthorized')
        self.tx(self.review(), actor='quality-lead')
        package = self.item('tasks', 'package')
        self.tx(dict(op='task.claim', id='package', expected_task_revision=package['revision']), actor='quality-worker')
        self.assertEqual(self.item('tasks', 'draft')['status'], 'done')
        self.assertEqual(self.item('tasks', 'package')['status'], 'running')
        self.assertFalse(any(event['actor_id'] == 'coordinator' for event in self.state()['events'][start:]))

    def test_self_review_and_ungranted_management_are_denied(self):
        self.grant()
        self.tx(self.task(), actor='build-lead')
        self.submit(actor='build-lead')
        self.denied(self.request(self.review(), actor='build-lead'), 'unauthorized')
        self.denied(self.request(self.review(), actor='build-worker'), 'unauthorized')
        self.denied(self.request(self.task('ungranted'), actor='build-worker'), 'unauthorized')

    def test_cross_branch_management_and_redelegation_are_denied(self):
        self.grant()
        self.denied(self.request(self.unit('hijack', 'quality'), actor='build-lead'), 'unauthorized')
        self.denied(self.request(self.task('hijack', 'quality'), actor='build-lead'), 'unauthorized')
        self.denied(self.request(dict(op='chat.bind', id='hijack', unit_id='quality',
                                     host_id='fixture', thread_id='hijack'), actor='build-lead'), 'unauthorized')
        self.denied(self.request(dict(op='delegation.grant', id='rogue', chat_id='build-worker',
                                     capabilities=['task.create'], peer_unit_ids=[],
                                     limits={'max_tasks': 1, 'max_units': 0, 'max_dispatches': 0},
                                     evidence=['evidence/delegation.txt']), actor='build-lead'), 'unauthorized')

    def test_direct_department_handoff_records_sender_and_receipt(self):
        self.grant()
        self.grant('quality-lead', peer=['build'])
        self.tx(self.task(), actor='build-lead')
        start = len(self.state()['events'])
        self.tx(self.message('request-review'), actor='build-lead')
        self.begin('request-review')
        self.resolve('request-review')
        self.tx(self.message('reply-review', target='build-lead'), actor='quality-lead')
        self.begin('reply-review', actor='quality-lead')
        self.resolve('reply-review', actor='quality-lead')
        sent = self.item('outbox', 'request-review')
        self.assertEqual(sent['actor_id'], 'build-lead')
        self.assertEqual(sent['delegation_id'], 'grant-build-lead')
        self.assertEqual(sent['status'], 'delivered')
        self.assertEqual(self.state()['scheduler']['usage']['dispatches'], 2)
        self.assertEqual({event['actor_id'] for event in self.state()['events'][start:]},
                         {'build-lead', 'quality-lead'})

    def test_peer_routes_require_allowed_target_task_context_and_compact_summary(self):
        self.grant()
        self.tx(self.task(), actor='build-lead')
        self.tx(self.task('unrelated-task', 'unrelated'))
        self.denied(self.request(self.message('wrong-target', 'unrelated-worker'), actor='build-lead'), 'unauthorized')
        self.denied(self.request(self.message('wrong-context', task='unrelated-task'), actor='build-lead'), 'unauthorized')
        large = self.message('large')
        large['payload']['summary'] = 'x'*2001
        self.denied(self.request(large, actor='build-lead'))
        no_context = self.message('missing')
        del no_context['payload']['task_id']
        self.denied(self.request(no_context, actor='build-lead'))

    def test_granted_capabilities_do_not_imply_all_department_permissions(self):
        self.grant(capabilities=['task.create'])
        self.tx(self.task(), actor='build-lead')
        self.denied(self.request(self.unit('unexpected', 'build'), actor='build-lead'), 'unauthorized')
        self.denied(self.request(self.message('unexpected'), actor='build-lead'), 'unauthorized')

    def test_handoff_evidence_is_rechecked_before_dispatch(self):
        self.grant()
        self.tx(self.task(), actor='build-lead')
        self.tx(self.message('pending'), actor='build-lead')
        self.write('evidence/review.txt', 'Input changed after the handoff was prepared.')
        self.denied(self.request(dict(op='outbox.begin', id='pending', expected_outbox_revision=1),
                                 actor='build-lead'), 'stale_input')
        self.assertEqual(self.item('outbox', 'pending')['status'], 'pending')
        self.assertEqual(self.state()['scheduler']['usage']['dispatches'], 0)

    def test_task_quota_is_atomic_under_batch_failure_and_concurrent_writers(self):
        self.grant(tasks=1)
        self.denied(self.request(self.task('a'), self.task('b'), actor='build-lead'), 'resource_limit')
        results = self.parallel([self.request(self.task(name), actor='build-lead') for name in ['a', 'b']])
        self.assertEqual(sum(result['ok'] for result in results), 1)
        self.assertEqual([result['code'] for result in results if not result['ok']], ['resource_limit'])
        self.assertEqual(len(self.state()['tasks']), 1)
        self.assertEqual(self.item('delegations', 'grant-build-lead')['usage']['tasks'], 1)

    def test_dispatch_quota_competes_atomically_and_survives_reconstruction(self):
        self.grant(dispatches=1)
        self.tx(self.task(), actor='build-lead')
        self.tx(self.message('one'), self.message('two'), actor='build-lead')
        requests = [self.request(dict(op='outbox.begin', id=name, expected_outbox_revision=1),
                                 actor='build-lead') for name in ['one', 'two']]
        results = self.parallel(requests)
        self.assertEqual(sum(result['ok'] for result in results), 1)
        before = self.state()
        self.runtime = Runtime(self.project)
        self.assertEqual(before, self.state())
        self.assertEqual(self.item('delegations', 'grant-build-lead')['usage']['dispatches'], 1)
        self.assertEqual(self.state()['scheduler']['usage']['dispatches'], 1)
        self.assertEqual(sum(item['status'] == 'inflight' for item in self.state()['outbox']), 1)

    def test_global_dispatch_limit_applies_in_addition_to_department_limit(self):
        self.grant(dispatches=3)
        self.tx(dict(op='budget.set', expected_scheduler_revision=self.state()['scheduler']['revision'],
                     budgets={'max_dispatches': 1}), actor='operator')
        self.tx(self.task(), actor='build-lead')
        self.tx(self.message('one'), self.message('two'), actor='build-lead')
        self.begin('one')
        self.denied(self.request(dict(op='outbox.begin', id='two', expected_outbox_revision=1),
                                 actor='build-lead'), 'resource_limit')
        self.assertEqual(self.item('delegations', 'grant-build-lead')['usage']['dispatches'], 1)

    def test_unknown_dispatch_cannot_be_replayed_or_retried_without_reconciliation(self):
        self.grant()
        self.tx(self.task(), actor='build-lead')
        self.tx(self.message('uncertain'), actor='build-lead')
        begin = self.request(dict(op='outbox.begin', id='uncertain', expected_outbox_revision=1), actor='build-lead')
        self.runtime.transact(begin)
        self.denied(begin, 'dispatch_ambiguous')
        self.resolve('uncertain', status='unknown')
        self.denied(self.request(dict(op='outbox.begin', id='uncertain',
                                     expected_outbox_revision=self.item('outbox', 'uncertain')['revision']),
                                 actor='build-lead'), 'dispatch_ambiguous')
        self.assertEqual(self.state()['scheduler']['usage']['dispatches'], 1)

    def test_revocation_and_pause_allow_receipt_reconciliation_but_no_new_dispatch(self):
        self.grant()
        self.grant('quality-lead', peer=['build'])
        self.tx(self.task(), actor='build-lead')
        self.tx(self.message('uncertain'), self.message('pending'), actor='build-lead')
        self.begin('uncertain')
        grant = self.item('delegations', 'grant-build-lead')
        self.tx(dict(op='delegation.revoke', id=grant['id'], expected_delegation_revision=grant['revision']))
        self.denied(self.request(dict(op='outbox.begin', id='pending', expected_outbox_revision=1),
                                 actor='build-lead'), 'unauthorized')
        self.resolve('uncertain', status='unknown')
        self.tx(dict(op='control.pause', expected_control_revision=self.state()['control']['revision']), actor='operator')
        reconcile = dict(op='outbox.reconcile', id='uncertain',
                         expected_outbox_revision=self.item('outbox', 'uncertain')['revision'],
                         resolution='delivered', result={'fixture_receipt': 'observed'}, evidence=['evidence/review.txt'])
        self.denied(self.request(reconcile, actor='quality-lead'), 'unauthorized')
        self.tx(reconcile, actor='build-lead')
        self.assertEqual(self.item('outbox', 'uncertain')['status'], 'delivered')
        self.denied(self.request(self.task('after-pause'), actor='build-lead'), 'paused')
        self.assertEqual(self.item('delegations', grant['id'])['usage']['dispatches'], 1)

    def test_revoked_grant_cannot_be_replaced_to_reset_quotas(self):
        self.grant(tasks=1)
        self.tx(self.task(), actor='build-lead')
        grant = self.item('delegations', 'grant-build-lead')
        self.tx(dict(op='delegation.revoke', id=grant['id'], expected_delegation_revision=grant['revision']))
        self.denied(self.request(dict(op='delegation.grant', id='reset-budget', chat_id='build-lead',
                                     capabilities=self.CAPABILITIES, peer_unit_ids=['quality'],
                                     limits={'max_tasks': 1, 'max_units': 2, 'max_dispatches': 5},
                                     evidence=['evidence/delegation.txt'])))

    def test_merge_requires_explicit_revocation_of_source_delegation(self):
        self.grant('quality-lead', peer=['build'])
        self.denied(self.request(dict(op='unit.merge', source_id='quality', target_id='unrelated',
                                     expected_source_revision=self.item('units', 'quality')['revision'],
                                     expected_target_revision=self.item('units', 'unrelated')['revision'])))
        self.assertEqual(self.item('delegations', 'grant-quality-lead')['status'], 'active')
        self.assertEqual(self.item('chats', 'quality-lead')['unit_id'], 'quality')

    def test_merge_moves_pending_review_responsibility_and_preserves_revoked_usage(self):
        self.grant('quality-lead', peer=['build'])
        self.tx(self.task('quality-work', 'quality'), actor='quality-lead')
        self.tx(self.task('draft', reviewer='quality'))
        self.submit()
        grant = self.item('delegations', 'grant-quality-lead')
        self.assertEqual(grant['usage']['tasks'], 1)
        self.tx(dict(op='delegation.revoke', id=grant['id'], expected_delegation_revision=grant['revision']))
        revoked = self.item('delegations', grant['id'])
        self.tx(dict(op='unit.merge', source_id='quality', target_id='unrelated',
                     expected_source_revision=self.item('units', 'quality')['revision'],
                     expected_target_revision=self.item('units', 'unrelated')['revision']))
        self.assertEqual(self.item('tasks', 'draft')['owner_unit_id'], 'build')
        self.assertEqual(self.item('tasks', 'draft')['status'], 'review')
        self.assertEqual(self.item('tasks', 'draft')['reviewer_unit_id'], 'unrelated')
        self.assertEqual(self.item('tasks', 'quality-work')['owner_unit_id'], 'unrelated')
        self.assertEqual(self.item('tasks', 'quality-work')['reviewer_unit_id'], 'unrelated')
        self.assertEqual(self.item('delegations', grant['id']), revoked)
        self.assertEqual(self.item('units', 'quality')['status'], 'retired')
        self.assertEqual(self.item('chats', 'quality-lead')['unit_id'], 'unrelated')

    def test_pending_peer_review_prevents_department_retirement_after_chats_retire(self):
        self.tx(self.task('draft', reviewer='quality'))
        self.submit()
        for chat_id in ['quality-lead', 'quality-worker']:
            self.tx(dict(op='chat.status', id=chat_id, status='retired',
                         expected_chat_revision=self.item('chats', chat_id)['revision']))
        retire = dict(op='unit.status', id='quality', status='retired',
                      expected_unit_revision=self.item('units', 'quality')['revision'])
        self.denied(self.request(retire))
        self.assertEqual(self.item('tasks', 'draft')['owner_unit_id'], 'build')
        self.assertEqual(self.item('tasks', 'draft')['status'], 'review')
        self.assertEqual(self.item('units', 'quality')['status'], 'active')
        self.tx(self.review())
        self.tx(retire)
        self.assertEqual(self.item('units', 'quality')['status'], 'retired')

    def test_recover_is_scoped_and_expired_credentials_stay_invalid(self):
        self.grant()
        self.tx(self.task(), actor='build-lead')
        lease = self.tx(dict(op='task.claim', id='draft', expected_task_revision=1,
                             lease_seconds=1), actor='build-worker')['results'][0]
        self.tx(self.task('other', 'unrelated'))
        self.denied(self.request(dict(op='recover'), actor='build-lead'))
        self.denied(self.request(dict(op='recover', task_id='other'), actor='build-lead'), 'unauthorized')
        with patch.object(module.time, 'time', return_value=time.time()+2):
            self.tx(dict(op='recover', task_id='draft'), actor='build-lead')
            self.denied(self.request(dict(op='task.heartbeat', id='draft', lease_id=lease['lease_id'],
                                         fencing_token=lease['fencing_token']), actor='build-worker'), 'stale_lease')
        self.assertIsNone(self.item('tasks', 'draft')['lease'])

    def test_deadline_still_closes_delegated_work(self):
        self.grant()
        with patch.object(module.time, 'time', return_value=self.state()['scheduler']['expires_at']+1):
            self.denied(self.request(self.task(), actor='build-lead'), 'schedule_expired')

    def test_local_views_are_paged_read_only_and_omit_unrelated_history(self):
        self.grant()
        self.tx(self.task('a'), self.task('b'), self.task('c'), actor='build-lead')
        self.tx(self.task('private-unrelated', 'unrelated'))
        self.tx(dict(op='checkpoint', id='verbose-global-history', data={'secret': 'GLOBAL-HISTORY-SENTINEL'}))
        before = self.state()
        first = self.runtime.view('build-lead', offset=0, limit=2)
        second = self.runtime.view('build-lead', offset=2, limit=2)
        self.assertEqual(len(first['tasks']), 2)
        self.assertEqual(len(second['tasks']), 1)
        self.assertEqual({item['id'] for item in first['tasks']+second['tasks']}, {'a', 'b', 'c'})
        for view in [first, second]:
            encoded = json.dumps(view)
            self.assertNotIn('GLOBAL-HISTORY-SENTINEL', encoded)
            self.assertNotIn('private-unrelated', encoded)
            self.assertNotIn('events', view)
        self.assertEqual(before, self.state())
        result = subprocess.run([sys.executable, '-B', '-X', 'utf8', str(SCRIPT), '--project', str(self.project),
                                 'view', '--actor', 'build-lead', '--limit', '2'],
                                capture_output=True, text=True, encoding='utf-8', timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(json.loads(result.stdout)['tasks']), 2)
        self.assertEqual(before, self.state())

    def test_paged_routes_supply_bound_peer_destination_without_global_status(self):
        self.grant()
        self.tx(self.unit('frontend', 'build'), self.task(), actor='build-lead')
        self.tx(dict(op='chat.bind', id='frontend-worker', unit_id='frontend',
                     host_id='fixture-host', thread_id='fixture-frontend'), actor='build-lead')
        self.tx(dict(op='chat.status', id='quality-worker', status='dormant',
                     expected_chat_revision=self.item('chats', 'quality-worker')['revision']))
        before = self.state()
        pages = [self.runtime.view('build-lead', offset=offset, limit=2) for offset in [0, 2]]
        self.assertTrue(all(page['routes_total'] == 4 and len(page['routes']) == 2 for page in pages))
        routes = [route for page in pages for route in page['routes']]
        self.assertEqual({route['id'] for route in routes},
                         {'build-lead', 'build-worker', 'quality-lead', 'frontend-worker'})
        for route in routes:
            self.assertEqual(set(route), {'id', 'unit_id', 'host_id', 'thread_id', 'status'})
            self.assertEqual(route['host_id'], 'fixture-host')
            self.assertEqual(route['status'], 'active')
        self.assertEqual(before, self.state())
        peer = next(route for route in routes if route['unit_id'] == 'quality')
        self.assertEqual(peer['thread_id'], 'fixture-quality-lead')
        intent = self.tx(self.message('discovered-peer', target=peer['id']), actor='build-lead')['results'][0]
        begun = self.tx(dict(op='outbox.begin', id=intent['id'], expected_outbox_revision=intent['revision']),
                        actor='build-lead')['results'][0]
        self.assertEqual(begun['payload']['chat_id'], peer['id'])
        self.assertEqual(begun['status'], 'inflight')

    def test_task_view_expands_declared_cross_department_inputs_only(self):
        self.grant()
        self.tx(self.task('outside-input', 'unrelated'))
        self.submit('outside-input', actor='unrelated-worker')
        self.tx(self.review('outside-input'))
        upstream = self.item('tasks', 'outside-input')
        self.tx(dict(op='memory.put', id='shared-input', kind='fact', statement='Required declared input.',
                     sources=['evidence/review.txt']),
                dict(op='memory.put', id='other-memory', kind='fact', statement='UNRELATED-MEMORY-SENTINEL',
                     sources=['evidence/review.txt']))
        local = self.task('local-work', dependencies=['outside-input'])
        local['inputs'] = [{'memory_id': 'shared-input', 'revision': 1}]
        self.tx(local, actor='build-lead')
        foreign = self.task('unrelated-body', 'unrelated')
        foreign['title'] = 'UNRELATED-TASK-SENTINEL'
        self.tx(foreign)
        before = self.state()
        view = self.runtime.view('build-lead', task_id='local-work')
        self.assertEqual(len(view['tasks']), 1)
        detail = view['tasks'][0]
        self.assertEqual(detail['dependency_summaries'],
                         [{key: upstream[key] for key in ['id', 'revision', 'status', 'accepted_artifacts']}])
        self.assertEqual([memory['id'] for memory in detail['memories']], ['shared-input'])
        self.assertEqual(detail['memories'][0]['statement'], 'Required declared input.')
        self.assertTrue(detail['memories'][0]['sources'])
        self.assertNotIn('UNRELATED-MEMORY-SENTINEL', json.dumps(view))
        self.assertNotIn('UNRELATED-TASK-SENTINEL', json.dumps(view))
        for key in ['authority_sources', 'limits', 'execution_mode']:
            self.assertEqual(view[key], before[key])
        self.assertEqual(view['scope'], before['charter']['scope'])
        self.assertEqual(before, self.state())

    def test_compact_apply_returns_only_receipt_and_replay_does_not_double_charge(self):
        self.grant(tasks=1)
        self.tx(dict(op='checkpoint', id='history', data={'body': 'FULL-HISTORY-SENTINEL'}))
        request = self.request(self.task(), actor='build-lead')
        request_path = self.project / 'request.json'
        request_path.write_text(json.dumps(request), encoding='utf-8')
        command = [sys.executable, '-B', '-X', 'utf8', str(SCRIPT), '--project', str(self.project),
                   'apply', '--request', str(request_path), '--compact']
        for replayed in [False, True]:
            response = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', timeout=20)
            self.assertEqual(response.returncode, 0, response.stderr)
            receipt = json.loads(response.stdout)
            self.assertEqual(set(receipt), {'revision', 'results', 'replayed'})
            self.assertEqual(receipt['replayed'], replayed)
            self.assertEqual(receipt['results'][0]['id'], 'draft')
            self.assertNotIn('FULL-HISTORY-SENTINEL', response.stdout)
        self.assertEqual(self.item('delegations', 'grant-build-lead')['usage']['tasks'], 1)


if __name__ == '__main__':
    unittest.main()
