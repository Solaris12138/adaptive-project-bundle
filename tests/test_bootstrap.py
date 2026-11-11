"""Independent bootstrap capacity checks. All host IDs are synthetic fixtures.

Set BOOTSTRAP_V4_SCRIPT to a preserved pre-fix runtime for the regression probe.
This suite never calls native chat or automation tools, nor opens trial databases.
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path(os.environ.get('BOOTSTRAP_V4_SCRIPT', str(ROOT/'adaptive-project/scripts/runtime.py')))
spec = importlib.util.spec_from_file_location('bootstrap_v4_independent', SCRIPT)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class BootstrapCapacityTests(unittest.TestCase):
    def setUp(self):
        parent = ROOT/'work/bootstrap-v4-tests'
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='case-', dir=str(parent))
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        (self.project/'authority.md').write_text('Synthetic isolated fixture only; no native actions authorized.', encoding='utf-8')
        (self.project/'host.md').write_text('Synthetic host result and binding, not a live host receipt.', encoding='utf-8')
        self.seq = 0
        self.engine = module.Runtime(self.project)
        self.engine.init({
            'project_id':'bootstrap-fixture','objective':'Verify coordinator/worker accounting',
            'acceptance':['Synthetic accounting invariants hold'],
            'scope':{'project_root':str(self.project),'write_roots':[str(self.project)]},
            'authority':{'status':'bound','source_refs':['authority.md'],
                'adjust_units_within_scope':True,'maintain_project_memory':True,
                'create_long_lived_chats':True,'message_long_lived_chats':True,
                'configure_future_triggers':True},
            'execution':{'state_writer_id':'coordinator','operator_id':'operator','mode':'background'},
            'limits':{'max_active_chats':2,'max_running_leases':2},
            'scheduler':{'budgets':{'max_wakes':10,'max_claims':10,'max_dispatches':10,
                'max_actions_per_wake':40,'max_wake_seconds':60},
                'cooldown_seconds':0,'max_backoff_seconds':1,'expires_at':time.time()+600}})
        self.tx({'op':'unit.create','id':'production','name':'Production','mission':'Synthetic production',
                 'creation_reason':'Independent worker capacity test','exit_condition':'Checks complete'})

    def state(self):return self.engine.read()
    def item(self, kind, key):return next(x for x in self.state()[kind] if x['id']==key)
    def request(self,*ops,**kwargs):
        self.seq+=1
        return dict(actor_id=kwargs.pop('actor','operator'),idempotency_key='fixture-'+str(self.seq),
                    reason='Independent bootstrap capacity fixture',operations=list(ops),**kwargs)
    def tx(self,*ops,**kwargs):return self.engine.transact(self.request(*ops,**kwargs))
    def enqueue_op(self,key,unit='production'):
        return {'op':'outbox.enqueue','id':key,'kind':'create_chat','payload':{'unit_id':unit,'prompt':'Synthetic local fixture'}}
    def enqueue(self,key,unit='production'):return self.tx(self.enqueue_op(key,unit))
    def resolve(self,key,thread='controller-thread',host='fixture-host',status='delivered'):
        item=self.item('outbox',key)
        if item['status']=='pending':self.tx({'op':'outbox.begin','id':key,'expected_outbox_revision':item['revision']})
        item=self.item('outbox',key)
        return self.tx({'op':'outbox.resolve','id':key,'expected_outbox_revision':item['revision'],
            'status':status,'result':{'host_id':host,'thread_id':thread}})
    def bind(self,thread='controller-thread',host='fixture-host',**extra):
        return self.tx(dict(op='scheduler.bind',expected_scheduler_revision=self.state()['scheduler']['revision'],
            automation_id='fixture-automation',thread_id=thread,host_id=host,evidence=['host.md'],**extra))
    def bootstrap(self):
        self.enqueue('controller-create','coordinator');self.resolve('controller-create');self.bind()
    def reject_unchanged(self,request,code='resource_limit'):
        before=self.state()
        with self.assertRaises(module.RuntimeErrorDetail) as caught:self.engine.transact(request)
        self.assertEqual(caught.exception.code,code)
        self.assertEqual(self.state(),before)
        return caught.exception
    def worker_bind_op(self,key,thread):
        return {'op':'chat.bind','id':key,'unit_id':'production','host_id':'fixture-host','thread_id':thread}

    def test_delivered_controller_scheduler_binding_frees_two_worker_slots_without_dummy_chat(self):
        self.bootstrap()
        before=self.state()
        self.assertEqual(before['chats'],[])
        self.tx(self.enqueue_op('worker-one'),self.enqueue_op('worker-two'))
        self.assertEqual(self.engine.creation_reservations(before),0)
        after=self.state()
        self.assertEqual(self.engine.creation_reservations(after),2)
        self.assertEqual(after['limits'],before['limits'])
        self.assertEqual(after['scheduler']['budgets'],before['scheduler']['budgets'])
        self.assertEqual(after['scheduler']['usage'],before['scheduler']['usage'])
        self.reject_unchanged(self.request(self.enqueue_op('worker-three')))

    def test_unknown_controller_result_is_not_released_by_matching_scheduler_binding(self):
        self.enqueue('controller-create','coordinator');self.resolve('controller-create',status='unknown');self.bind()
        self.assertEqual(self.engine.creation_reservations(self.state()),1)
        self.reject_unchanged(self.request(self.enqueue_op('worker-one'),self.enqueue_op('worker-two')))
        self.tx({'op':'outbox.reconcile','id':'controller-create','expected_outbox_revision':3,
            'resolution':'delivered','result':{'host_id':'fixture-host','thread_id':'controller-thread'},'evidence':['host.md']})
        self.assertEqual(self.engine.creation_reservations(self.state()),1)
        self.bind()
        self.assertEqual(self.engine.creation_reservations(self.state()),0)
        self.tx(self.enqueue_op('worker-one'),self.enqueue_op('worker-two'))

    def test_pending_and_inflight_controller_creation_keep_capacity(self):
        self.enqueue('controller-create','coordinator');self.bind()
        self.assertEqual(self.engine.creation_reservations(self.state()),1)
        self.reject_unchanged(self.request(self.enqueue_op('worker-one'),self.enqueue_op('worker-two')))
        self.tx({'op':'outbox.begin','id':'controller-create','expected_outbox_revision':1})
        self.assertEqual(self.engine.creation_reservations(self.state()),1)
        self.reject_unchanged(self.request(self.enqueue_op('worker-one'),self.enqueue_op('worker-two')))

    def test_other_host_or_thread_deliveries_still_reserve_worker_capacity(self):
        self.enqueue('controller-create','coordinator');self.resolve('controller-create')
        for host,thread in [('other-host','controller-thread'),('fixture-host','other-thread')]:
            with self.subTest(host=host,thread=thread):
                self.bind(thread=thread,host=host)
                self.assertEqual(self.engine.creation_reservations(self.state()),1)
                self.reject_unchanged(self.request(self.enqueue_op('worker-one'),self.enqueue_op('worker-two')))

    def test_delivered_unbound_workers_keep_their_reservations(self):
        self.bootstrap();self.tx(self.enqueue_op('worker-one'),self.enqueue_op('worker-two'))
        self.resolve('worker-one','worker-thread-one');self.resolve('worker-two','worker-thread-two')
        self.assertEqual(self.engine.creation_reservations(self.state()),2)
        self.reject_unchanged(self.request(self.enqueue_op('worker-three')))
        self.tx(self.worker_bind_op('worker-one','worker-thread-one'),self.worker_bind_op('worker-two','worker-thread-two'))
        self.assertEqual(self.engine.creation_reservations(self.state()),0)
        self.assertEqual(sum(c['status']=='active' for c in self.state()['chats']),2)
        self.reject_unchanged(self.request(self.enqueue_op('worker-three')))
        self.reject_unchanged(self.request(self.worker_bind_op('worker-three','worker-thread-three')))

    def test_capacity_failure_rolls_back_entire_multi_operation_transaction(self):
        self.bootstrap()
        before=self.state()
        self.reject_unchanged(self.request(self.enqueue_op('one'),self.enqueue_op('two'),self.enqueue_op('three')))
        self.assertEqual(self.state()['revision'],before['revision'])
        self.assertEqual([i['id'] for i in self.state()['outbox']],['controller-create'])

    def test_duplicate_matching_creations_require_explicit_identity_and_only_one_is_exempt(self):
        self.enqueue('controller-one','coordinator');self.resolve('controller-one')
        self.enqueue('controller-two','coordinator');self.resolve('controller-two')
        request=self.request({'op':'scheduler.bind','expected_scheduler_revision':self.state()['scheduler']['revision'],
            'automation_id':'fixture-automation','thread_id':'controller-thread','host_id':'fixture-host','evidence':['host.md']})
        self.reject_unchanged(request,'dispatch_ambiguous')
        self.bind(coordinator_outbox_id='controller-one')
        self.assertEqual(self.state()['scheduler']['binding']['coordinator_outbox_id'],'controller-one')
        self.assertEqual(self.engine.creation_reservations(self.state()),1)
        self.enqueue('worker-one')
        self.reject_unchanged(self.request(self.enqueue_op('worker-two')))

    def test_explicit_registration_rejects_wrong_kind_unit_target_or_unknown_result(self):
        self.enqueue('production-create');self.resolve('production-create')
        self.reject_unchanged(self.request({'op':'scheduler.bind','expected_scheduler_revision':self.state()['scheduler']['revision'],
            'automation_id':'fixture-automation','thread_id':'controller-thread','host_id':'fixture-host','evidence':['host.md'],
            'coordinator_outbox_id':'production-create'}),'invalid_request')
        self.bind()
        self.assertIsNone(self.state()['scheduler']['binding']['coordinator_outbox_id'])
        self.assertEqual(self.engine.creation_reservations(self.state()),1)
        self.enqueue('controller-unknown','coordinator');self.resolve('controller-unknown',status='unknown')
        self.reject_unchanged(self.request({'op':'scheduler.bind','expected_scheduler_revision':self.state()['scheduler']['revision'],
            'automation_id':'fixture-automation','thread_id':'controller-thread','host_id':'fixture-host','evidence':['host.md'],
            'coordinator_outbox_id':'controller-unknown'}),'invalid_request')

    def test_rebinding_does_not_forget_old_unregistered_controller_or_bypass_full_capacity(self):
        self.bootstrap()
        self.bind(thread='replacement-controller')
        self.assertIsNone(self.state()['scheduler']['binding']['coordinator_outbox_id'])
        self.assertEqual(self.engine.creation_reservations(self.state()),1)
        self.bind()
        self.tx(self.enqueue_op('worker-one'),self.enqueue_op('worker-two'))
        self.reject_unchanged(self.request({'op':'scheduler.bind','expected_scheduler_revision':self.state()['scheduler']['revision'],
            'automation_id':'replacement-automation','thread_id':'replacement-controller','host_id':'fixture-host','evidence':['host.md']}))

    def test_existing_coordinator_host_without_create_outbox_remains_supported(self):
        self.bind()
        self.assertIsNone(self.state()['scheduler']['binding']['coordinator_outbox_id'])
        self.tx({'op':'scheduler.enable','expected_scheduler_revision':self.state()['scheduler']['revision'],'enabled':True})
        self.tx(self.enqueue_op('worker-one'),self.enqueue_op('worker-two'))
        self.assertTrue(self.state()['scheduler']['enabled'])

    def test_explicit_wrong_controller_target_does_not_release_reservation(self):
        self.enqueue('controller-create','coordinator');self.resolve('controller-create')
        for host,thread in [('different-host','controller-thread'),('fixture-host','different-thread')]:
            with self.subTest(host=host,thread=thread):
                self.reject_unchanged(self.request({'op':'scheduler.bind','expected_scheduler_revision':self.state()['scheduler']['revision'],
                    'automation_id':'fixture-automation','thread_id':thread,'host_id':host,'evidence':['host.md'],
                    'coordinator_outbox_id':'controller-create'}),'invalid_request')

    def test_binding_idempotency_and_reopened_process_preserve_capacity_and_usage(self):
        self.enqueue('controller-create','coordinator');self.resolve('controller-create')
        req=self.request({'op':'scheduler.bind','expected_scheduler_revision':self.state()['scheduler']['revision'],
            'automation_id':'fixture-automation','thread_id':'controller-thread','host_id':'fixture-host','evidence':['host.md']})
        first=self.engine.transact(req);before=self.state()
        self.engine=module.Runtime(self.project)
        replay=self.engine.transact(req)
        self.assertTrue(replay['replayed']);self.assertEqual(replay['results'],first['results'])
        self.assertEqual(self.state(),before)
        self.assertEqual(self.engine.creation_reservations(self.state()),0)
        self.tx(self.enqueue_op('one'),self.enqueue_op('two'))

    def test_mixed_resolution_registration_and_work_is_rejected_atomically(self):
        self.enqueue('controller-create','coordinator')
        self.tx({'op':'outbox.begin','id':'controller-create','expected_outbox_revision':1})
        request=self.request({'op':'outbox.resolve','id':'controller-create','expected_outbox_revision':2,'status':'delivered',
                 'result':{'host_id':'fixture-host','thread_id':'controller-thread'}},
                {'op':'scheduler.bind','expected_scheduler_revision':self.state()['scheduler']['revision'],
                 'automation_id':'fixture-automation','thread_id':'controller-thread','host_id':'fixture-host','evidence':['host.md']},
                self.enqueue_op('one'),self.enqueue_op('two'))
        self.reject_unchanged(request,'unauthorized')
        self.assertEqual(self.item('outbox','controller-create')['status'],'inflight')

    def test_enable_and_bind_enabled_refuse_unresolved_controller_target(self):
        self.enqueue('controller-create','coordinator');self.resolve('controller-create',status='unknown');self.bind()
        self.assertEqual(self.engine.plan()['chat_capacity']['coordinator_registration']['status'],'unresolved')
        self.reject_unchanged(self.request({'op':'scheduler.enable','expected_scheduler_revision':self.state()['scheduler']['revision'],'enabled':True}),'dispatch_ambiguous')
        self.reject_unchanged(self.request({'op':'scheduler.bind','expected_scheduler_revision':self.state()['scheduler']['revision'],
            'automation_id':'fixture-automation','thread_id':'controller-thread','host_id':'fixture-host','evidence':['host.md'],'enabled':True}),'dispatch_ambiguous')

    def test_plan_capacity_is_read_only_and_duplicate_rebind_preserves_fixed_identity(self):
        self.bootstrap()
        self.enqueue('duplicate-controller','coordinator');self.resolve('duplicate-controller')
        self.bind()
        self.assertEqual(self.state()['scheduler']['binding']['coordinator_outbox_id'],'controller-create')
        self.tx({'op':'scheduler.enable','expected_scheduler_revision':self.state()['scheduler']['revision'],'enabled':True})
        before=self.state();capacity=self.engine.plan()['chat_capacity']
        self.assertEqual(self.state(),before)
        self.assertEqual(capacity['limit'],2)
        self.assertEqual(capacity['active_workers'],0)
        self.assertEqual(capacity['creation_reservations'],1)
        self.assertEqual(capacity['available_workers'],1)
        self.assertEqual(capacity['coordinator_registration'],{'status':'registered','outbox_id':'controller-create'})

    def assert_incomplete_receipt_blocks_enable(self,status,result=None):
        self.enqueue('controller-create','coordinator')
        if status in ('inflight','unknown'):
            self.tx({'op':'outbox.begin','id':'controller-create','expected_outbox_revision':1})
        if status=='unknown':
            self.tx({'op':'outbox.resolve','id':'controller-create','expected_outbox_revision':2,
                     'status':'unknown','result':result or {}})
        self.bind()
        self.reject_unchanged(self.request({'op':'scheduler.enable','expected_scheduler_revision':self.state()['scheduler']['revision'],'enabled':True}),'dispatch_ambiguous')
        self.reject_unchanged(self.request({'op':'scheduler.bind','expected_scheduler_revision':self.state()['scheduler']['revision'],
            'automation_id':'fixture-automation','thread_id':'controller-thread','host_id':'fixture-host','evidence':['host.md'],'enabled':True}),'dispatch_ambiguous')
        self.assertEqual(self.engine.plan()['chat_capacity']['coordinator_registration']['status'],'unresolved')
        self.assertEqual(self.engine.creation_reservations(self.state()),1)

    def test_pending_coordinator_without_host_receipt_cannot_enable(self):
        self.assert_incomplete_receipt_blocks_enable('pending')

    def test_inflight_coordinator_without_host_receipt_cannot_enable(self):
        self.assert_incomplete_receipt_blocks_enable('inflight')

    def test_unknown_coordinator_without_host_receipt_cannot_enable(self):
        self.assert_incomplete_receipt_blocks_enable('unknown')

    def test_unknown_coordinator_with_only_host_cannot_enable(self):
        self.assert_incomplete_receipt_blocks_enable('unknown',{'host_id':'fixture-host'})

    def test_unknown_coordinator_with_only_thread_cannot_enable(self):
        self.assert_incomplete_receipt_blocks_enable('unknown',{'thread_id':'controller-thread'})

    def test_registered_controller_and_other_pending_creation_keep_independent_capacity(self):
        self.bootstrap();self.enqueue('other-controller-intent','coordinator')
        self.tx({'op':'scheduler.enable','expected_scheduler_revision':self.state()['scheduler']['revision'],'enabled':True})
        self.assertTrue(self.state()['scheduler']['enabled'])
        capacity=self.engine.plan()['chat_capacity']
        self.assertEqual(capacity['coordinator_registration']['status'],'registered')
        self.assertEqual(capacity['creation_reservations'],1)
        self.assertEqual(capacity['available_workers'],1)
        self.enqueue('worker-one')
        self.reject_unchanged(self.request(self.enqueue_op('worker-two')))

    def test_send_message_outbox_cannot_be_registered_as_coordinator_creation(self):
        self.tx(self.worker_bind_op('existing-worker','worker-thread'))
        self.tx({'op':'outbox.enqueue','id':'message','kind':'send_message',
                 'payload':{'chat_id':'existing-worker','prompt':'Synthetic fixture message'}})
        self.resolve('message')
        self.reject_unchanged(self.request({'op':'scheduler.bind','expected_scheduler_revision':self.state()['scheduler']['revision'],
            'automation_id':'fixture-automation','thread_id':'controller-thread','host_id':'fixture-host','evidence':['host.md'],
            'coordinator_outbox_id':'message'}),'invalid_request')

    def test_concurrent_worker_creation_is_serialized_at_exact_capacity(self):
        self.bootstrap()
        gate=self.project/'gate';processes=[];markers=[]
        for index in range(3):
            req=self.project/('parallel-%d.json'%index);req.write_text(json.dumps(self.request(self.enqueue_op('parallel-'+str(index)))),encoding='utf-8')
            ready=self.project/('ready-%d'%index);markers.append(ready)
            processes.append(subprocess.Popen([sys.executable,'-B','-X','utf8',str(ROOT/'tests/fixtures/parallel_worker.py'),str(SCRIPT),str(self.project),str(req),str(ready),str(gate)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8'))
        deadline=time.monotonic()+10
        while not all(p.exists() for p in markers) and time.monotonic()<deadline:time.sleep(.005)
        gate.write_text('go',encoding='ascii')
        results=[p.communicate(timeout=20) for p in processes]
        self.assertTrue(all(p.exists() for p in markers),results)
        self.assertEqual(sorted(p.returncode for p in processes),[0,0,1],results)
        failures=[json.loads(stdout) for p,(stdout,stderr) in zip(processes,results) if p.returncode]
        self.assertEqual([f['code'] for f in failures],['resource_limit'])
        self.assertEqual(self.engine.creation_reservations(self.state()),2)


if __name__=='__main__':unittest.main(verbosity=2)
