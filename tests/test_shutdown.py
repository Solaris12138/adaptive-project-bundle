"""Independent two-stage shutdown tests; synthetic hosts, no desktop actions."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT/'adaptive-project/scripts/runtime.py'
HELPER = ROOT/'adaptive-project/scripts/shutdown.py'
RECOVERY = ROOT/'adaptive-project/scripts/recovery.py'
spec = importlib.util.spec_from_file_location('shutdown_independent_runtime', SCRIPT)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class ShutdownV4Tests(unittest.TestCase):
    def setUp(self):
        parent = ROOT/'work/shutdown-tests'
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='case-', dir=str(parent))
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = self.base/'project'
        self.project.mkdir()
        self.host_root = self.base/'automations'
        self.host_path = self.host_root/'shutdown-fixture'/'automation.toml'
        self.seq = 0
        self.write('authority.md', 'Synthetic isolated test. No native action authorization.')
        self.write('evidence.md', 'Synthetic observation; not an actual host receipt.')
        self.engine = module.Runtime(self.project)
        self.charter = {
            'project_id':'shutdown-fixture','objective':'Preserve closure ordering',
            'acceptance':['Durable closure and controls remain intact'],
            'scope':{'project_root':str(self.project),'write_roots':[str(self.project)]},
            'authority':{'status':'bound','source_refs':['authority.md'],
                'adjust_units_within_scope':True,'maintain_project_memory':True,
                'create_long_lived_chats':True,'message_long_lived_chats':True,
                'configure_future_triggers':True},
            'execution':{'state_writer_id':'coordinator','operator_id':'operator','mode':'background'},
            'limits':{'max_active_chats':4,'max_running_leases':2,'max_attempts_per_task':3},
            'scheduler':{'budgets':{'max_wakes':4,'max_claims':5,'max_dispatches':4,
                'max_actions_per_wake':30,'max_wake_seconds':60},
                'cooldown_seconds':0,'max_backoff_seconds':2,'expires_at':time.time()+600},
            'exploration':{'budgets':{'max_candidates':3,'max_validations':3,'max_cost':5}}}
        self.engine.init(self.charter)
        self.bind()
        self.tx({'op':'scheduler.enable','enabled':True,
                 'expected_scheduler_revision':self.state()['scheduler']['revision']})
        self.tx({'op':'unit.create','id':'production','name':'Production','mission':'Isolated worker',
                 'creation_reason':'Test live writer closure','exit_condition':'Checks complete'})
        self.tx({'op':'chat.bind','id':'worker','unit_id':'production','host_id':'local','thread_id':'worker-thread'})
        self.host()

    def write(self, name, body):
        p = self.project/name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding='utf-8')
        return name

    def host(self, status='ACTIVE', thread='coordinator-thread', automation_id='shutdown-fixture'):
        self.host_path.parent.mkdir(parents=True, exist_ok=True)
        self.host_path.write_text('version = 1\nid = '+json.dumps(automation_id)+'\nkind = "heartbeat"\n'
            +'status = '+json.dumps(status)+'\ntarget_thread_id = '+json.dumps(thread)
            +'\nrrule = "FREQ=MINUTELY;INTERVAL=4"\n', encoding='utf-8')

    def state(self): return self.engine.read()

    def request(self, *ops, **extra):
        self.seq += 1
        actor = extra.pop('actor', 'operator')
        return dict(actor_id=actor,idempotency_key='shutdown-test-'+str(self.seq),
                    reason='Independent isolated closure test',operations=list(ops),**extra)

    def tx(self, *ops, **extra): return self.engine.transact(self.request(*ops, **extra))

    def reject(self, request, code=None):
        before = self.state()
        with self.assertRaises(module.RuntimeErrorDetail) as caught:
            self.engine.transact(request)
        if code: self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.state(), before)
        return caught.exception.code

    def bind(self, thread='coordinator-thread'):
        return self.tx({'op':'scheduler.bind','automation_id':'shutdown-fixture',
            'host_id':'local','thread_id':thread,
            'expected_scheduler_revision':self.state()['scheduler']['revision'],'evidence':['evidence.md']})

    def pause(self):
        return self.tx({'op':'control.pause','expected_control_revision':self.state()['control']['revision']})

    def resume(self):
        return self.tx({'op':'control.resume','expected_control_revision':self.state()['control']['revision']})

    def prepare_op(self, sid='close-a'):
        return {'op':'shutdown.prepare','id':sid,'evidence':['evidence.md']}

    def prepare(self, sid='close-a', **extra): return self.tx(self.prepare_op(sid), **extra)['results'][0]

    def confirm_op(self, sid='close-a', **override):
        observation = {'automation_id':'shutdown-fixture','thread_id':'coordinator-thread',
                       'host_id':'local','status':'PAUSED','observed_at_unix':time.time()}
        observation.update(override)
        return {'op':'shutdown.confirm','id':sid,'evidence':['evidence.md'],'host_observation':observation}

    def worker(self):
        self.tx({'op':'task.create','id':'work','title':'Live writer','owner_unit_id':'production','acceptance':['Keep evidence']})
        return self.tx({'op':'task.claim','id':'work','expected_task_revision':1,'lease_seconds':60},actor='worker')['results'][0]

    def checkpoint_op(self, lease):
        task = next(t for t in self.state()['tasks'] if t['id']=='work')
        return {'op':'task.checkpoint','id':'work','expected_task_revision':task['revision'],
                'lease_id':lease['lease_id'],'fencing_token':lease['fencing_token'],'data':{'next':'resume'}}

    def complete(self):
        self.tx({'op':'task.create','id':'finished','title':'Terminal fixture','owner_unit_id':'coordinator','acceptance':['Done']})
        self.tx({'op':'task.cancel','id':'finished','expected_task_revision':1,'reason':'Test done','evidence':['evidence.md']})
        self.tx({'op':'decision.record','id':'stop','expected_exploration_revision':self.state()['exploration']['revision'],
            'action':'stop','alternatives':[],'result_ids':[],'rationale':'Fixture complete','evidence':['evidence.md'],
            'task_dispositions':[],'residual_question_ids':[]})
        self.tx({'op':'project.complete','evidence':['evidence.md'],'acceptance_results':[
            {'criterion':self.charter['acceptance'][0],'passed':True,'evidence':['evidence.md']}]})

    def snapshot(self):
        return {p.relative_to(self.base).as_posix():(hashlib.sha256(p.read_bytes()).hexdigest(),p.stat().st_mtime_ns)
                for p in self.base.rglob('*') if p.is_file()}

    def inspect(self, expected=None, script=HELPER):
        before = self.snapshot()
        proc = subprocess.run([sys.executable,'-B','-X','utf8',str(script),'--project',str(self.project),
            '--automation-root',str(self.host_root)],capture_output=True,text=True,encoding='utf-8',timeout=20,
            env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'))
        if expected is not None: self.assertEqual(proc.returncode,expected,proc.stdout+proc.stderr)
        self.assertEqual(self.snapshot(),before,'Inspection changed persistent files')
        result = json.loads(proc.stdout)
        self.assertTrue(result['read_only'])
        return result

    def parallel(self, requests):
        folder = self.base/'parallel';folder.mkdir()
        gate = folder/'go';ready=[];procs=[]
        for i, request in enumerate(requests):
            path = folder/('request-%s.json'%i);path.write_text(json.dumps(request),encoding='utf-8')
            marker = folder/('ready-%s'%i);ready.append(marker)
            procs.append(subprocess.Popen([sys.executable,'-B','-X','utf8',str(ROOT/'tests/fixtures/parallel_worker.py'),
                str(SCRIPT),str(self.project),str(path),str(marker),str(gate)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                text=True,encoding='utf-8',env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1')))
        deadline=time.monotonic()+10
        while not all(p.exists() for p in ready) and time.monotonic()<deadline: time.sleep(.005)
        gate.write_text('go',encoding='ascii')
        outputs=[]
        for proc in procs:
            stdout,stderr=proc.communicate(timeout=20)
            outputs.append((proc.returncode,json.loads(stdout),stderr))
        self.assertTrue(all(p.exists() for p in ready),outputs)
        return outputs

    def preserved(self, state):
        return {'authority':state['authority_sources'],'control':state['control'],
            'scheduler':{k:state['scheduler'][k] for k in ['binding','budgets','budget_ceiling','usage','expires_at','not_before','next_wake_at']},
            'exploration':state['exploration']}

    def test_live_project_cannot_prepare(self):
        self.reject(self.request(self.prepare_op()),'shutdown_admission_open')

    def test_host_pause_alone_does_not_close_worker_admission(self):
        lease=self.worker();self.host('PAUSED')
        self.reject(self.request(self.prepare_op()),'shutdown_admission_open')
        self.tx(self.checkpoint_op(lease),actor='worker')
        self.assertEqual(self.inspect()['decision'],'business_open')

    def test_disabled_scheduler_does_not_close_worker_admission(self):
        lease=self.worker()
        self.tx({'op':'scheduler.enable','enabled':False,'expected_scheduler_revision':self.state()['scheduler']['revision']})
        self.reject(self.request(self.prepare_op()),'shutdown_admission_open')
        self.tx(self.checkpoint_op(lease),actor='worker')
        self.assertEqual(self.inspect()['decision'],'business_open')

    def test_exhausted_wake_budget_does_not_close_worker_admission(self):
        lease=self.worker()
        self.tx({'op':'budget.set','expected_scheduler_revision':self.state()['scheduler']['revision'],'budgets':{'max_wakes':1}})
        wake=self.tx({'op':'wake.begin','id':'last-wake','lease_seconds':60},actor='coordinator')['results'][0]
        self.assertTrue(wake['admitted'])
        self.reject(self.request(self.prepare_op()),'shutdown_admission_open')
        self.tx(self.checkpoint_op(lease),actor='worker')

    def test_pause_fences_live_worker_before_preparing(self):
        lease=self.worker();self.pause()
        self.reject(self.request(self.checkpoint_op(lease),actor='worker'),'paused')
        intent=self.prepare(actor='coordinator')
        self.assertEqual(intent['closure_basis'],'paused')
        self.assertEqual(intent['status'],'prepared')

    def test_completed_project_can_prepare_without_new_wake(self):
        self.complete();before=self.state();intent=self.prepare(actor='coordinator')
        self.assertEqual(intent['closure_basis'],'completed')
        self.assertEqual(self.state()['scheduler']['usage'],before['scheduler']['usage'])
        self.reject(self.request({'op':'checkpoint','id':'late','data':{}}))

    def test_global_expiry_closes_worker_and_allows_prepare(self):
        lease=self.worker();future=self.state()['scheduler']['expires_at']+1
        with patch.object(module.time,'time',return_value=future):
            self.reject(self.request(self.checkpoint_op(lease),actor='worker'))
            self.assertEqual(self.prepare()['closure_basis'],'expired')

    def test_confirm_requires_durable_prepare(self):
        self.pause();self.reject(self.request(self.confirm_op()),'shutdown_not_prepared')

    def test_shutdown_stages_cannot_share_one_transaction(self):
        self.pause()
        self.reject(self.request(self.prepare_op(),self.confirm_op()),'shutdown_stage_boundary')

    def test_business_closure_and_prepare_cannot_share_transaction(self):
        self.reject(self.request({'op':'control.pause','expected_control_revision':self.state()['control']['revision']},
            self.prepare_op()))

    def test_worker_cannot_prepare_or_confirm(self):
        self.pause();self.reject(self.request(self.prepare_op(),actor='worker'),'unauthorized')
        self.prepare();self.reject(self.request(self.confirm_op(),actor='worker'),'unauthorized')

    def test_wrong_host_binding_is_rejected(self):
        self.pause();self.prepare()
        for field in ['automation_id','thread_id','host_id']:
            with self.subTest(field=field):
                self.reject(self.request(self.confirm_op(**{field:'unrelated'})),'shutdown_host_mismatch')

    def test_active_or_unknown_host_status_is_rejected(self):
        self.pause();self.prepare()
        for status in ['ACTIVE','UNKNOWN','DELETED','']:
            with self.subTest(status=status): self.reject(self.request(self.confirm_op(status=status)))

    def test_observation_must_follow_prepare_and_not_be_future(self):
        self.pause();intent=self.prepare()
        for observed in [intent['prepared_at_unix']-1,time.time()+60,float('nan'),float('inf'),'later',True]:
            with self.subTest(observed=observed):
                self.reject(self.request(self.confirm_op(observed_at_unix=observed)))

    def test_prepare_and_confirm_do_not_change_budgets_or_permissions(self):
        self.tx({'op':'question.add','id':'q','question':'Unknown?','importance':'Choose'})
        self.tx({'op':'candidate.add','id':'c','title':'Candidate','proposal':'Observe','assumptions':['Local'],
                 'scope_fit':'Fixture','question_ids':['q']})
        self.tx({'op':'validation.plan','id':'v','candidate_id':'c','hypothesis':'Observe fixture','method':'Read',
            'criteria':{'positive':'Found','negative':'Missing','inconclusive':'Unknown'},'cost':2,'inputs':['evidence.md']})
        self.tx({'op':'validation.start','id':'v','expected_validation_revision':1})
        self.tx({'op':'wake.begin','id':'paid-wake','lease_seconds':60},actor='coordinator')
        self.pause();before=self.preserved(self.state());self.prepare()
        self.tx(self.confirm_op(),actor='coordinator')
        self.assertEqual(self.preserved(self.state()),before)

    def test_resume_invalidates_prepare_and_its_idempotent_replay(self):
        self.pause();request=self.request(self.prepare_op());self.engine.transact(request)
        self.resume();self.pause()
        self.reject(self.request(self.confirm_op()),'shutdown_stale')
        self.reject(request,'shutdown_stale')
        self.assertEqual(self.prepare('close-b')['status'],'prepared')

    def test_rebinding_invalidates_prepare_and_replay(self):
        self.pause();request=self.request(self.prepare_op());self.engine.transact(request)
        self.bind(thread='replacement-thread')
        self.reject(request,'shutdown_stale')
        self.reject(self.request(self.confirm_op()),'shutdown_stale')
        self.assertEqual(self.prepare('close-b')['binding']['thread_id'],'replacement-thread')

    def test_rebinding_identical_host_still_invalidates_old_intent(self):
        self.pause();request=self.request(self.prepare_op());self.engine.transact(request)
        binding=self.state()['scheduler']['binding'];self.bind()
        self.assertEqual(self.state()['scheduler']['binding'],binding)
        self.reject(request,'shutdown_stale')
        self.reject(self.request(self.confirm_op()),'shutdown_stale')
        self.assertEqual(self.prepare('close-b')['status'],'prepared')

    def test_confirm_replay_is_stale_after_resume_and_reclosure(self):
        self.pause();self.prepare();request=self.request(self.confirm_op());self.engine.transact(request)
        self.resume();self.pause();self.reject(request,'shutdown_stale')

    def test_valid_replays_do_not_append_events_or_spend_budget(self):
        self.pause();prepare=self.request(self.prepare_op());self.engine.transact(prepare)
        before=self.state();self.assertTrue(self.engine.transact(prepare)['replayed']);self.assertEqual(self.state(),before)
        confirm=self.request(self.confirm_op());self.engine.transact(confirm)
        before=self.state();self.assertTrue(self.engine.transact(confirm)['replayed']);self.assertEqual(self.state(),before)

    def test_new_key_does_not_create_duplicate_prepare_or_confirmation(self):
        self.pause();self.prepare()
        self.reject(self.request(self.prepare_op('close-b')),'shutdown_pending')
        self.tx(self.confirm_op())
        self.reject(self.request(self.confirm_op()),'shutdown_already_confirmed')

    def test_changed_evidence_or_authority_blocks_replay(self):
        self.pause();request=self.request(self.prepare_op());self.engine.transact(request)
        self.write('evidence.md','Changed observation')
        self.reject(request)
        self.write('evidence.md','Synthetic observation; not an actual host receipt.')
        self.write('authority.md','Changed authority')
        self.reject(request)

    def test_parallel_prepares_commit_only_one_current_intent(self):
        self.pause();revision=self.state()['revision']
        outputs=self.parallel([self.request(self.prepare_op('close-a')),self.request(self.prepare_op('close-b'))])
        self.assertEqual(sum(code==0 for code,_,_ in outputs),1,outputs)
        self.assertEqual([data['code'] for code,data,_ in outputs if code],['shutdown_pending'])
        self.assertEqual(self.state()['revision'],revision+1)
        self.assertEqual(len(self.state()['shutdown_intents']),1)

    def test_parallel_confirmations_commit_once(self):
        self.pause();self.prepare();revision=self.state()['revision']
        outputs=self.parallel([self.request(self.confirm_op()),self.request(self.confirm_op())])
        self.assertEqual(sum(code==0 for code,_,_ in outputs),1,outputs)
        self.assertEqual([data['code'] for code,data,_ in outputs if code],['shutdown_already_confirmed'])
        self.assertEqual(self.state()['revision'],revision+1)

    def test_new_process_reads_durable_pending_and_confirmed_intent(self):
        self.pause();self.prepare();before=self.state()
        self.assertEqual(module.Runtime(self.project).read(),before)
        self.assertEqual(self.inspect(0)['decision'],'pause_host')
        self.host('PAUSED');self.assertEqual(self.inspect(0)['decision'],'confirm')
        self.tx(self.confirm_op());self.assertEqual(self.inspect(0)['decision'],'confirmed')

    def test_helper_requires_prepare_before_pausing_host(self):
        self.pause();self.assertEqual(self.inspect(0)['decision'],'prepare')
        self.host('PAUSED');self.assertEqual(self.inspect(0)['decision'],'prepare')

    def test_pending_helper_blocks_missing_unknown_and_wrong_host(self):
        self.pause();self.prepare()
        self.host_path.unlink();self.assertEqual(self.inspect(1)['decision'],'blocked')
        self.host('UNKNOWN');self.assertEqual(self.inspect(1)['decision'],'blocked')
        self.host('PAUSED',thread='unrelated');self.assertEqual(self.inspect(1)['decision'],'blocked')

    def test_helper_does_not_reuse_stale_intent_after_resume_reclosure(self):
        self.pause();self.prepare();self.resume();self.pause()
        self.assertEqual(self.inspect(0)['decision'],'prepare')

    def test_storage_recovery_preserves_pending_intent(self):
        self.pause();self.prepare();before=self.state()
        self.engine.storage_recover(acknowledge_stopped_writers=True)
        self.assertEqual(self.state(),before)
        self.assertEqual(self.inspect(0)['decision'],'pause_host')

    def test_unrelated_scheduler_revision_does_not_invalidate_closure(self):
        self.pause();self.prepare()
        self.tx({'op':'scheduler.enable','enabled':False,'expected_scheduler_revision':self.state()['scheduler']['revision']})
        self.assertEqual(self.tx(self.confirm_op())['results'][0]['status'],'confirmed')

    def test_confirmed_host_drift_is_reported_without_erasing_history(self):
        self.pause();self.prepare();self.tx(self.confirm_op());before=self.state()
        self.host('UNKNOWN');result=self.inspect(1)
        self.assertEqual(result['decision'],'confirmed')
        self.assertEqual(self.state(),before)

    def crash_transaction(self, request):
        path=self.base/'crash-request.json';path.write_text(json.dumps(request),encoding='utf-8')
        proc=subprocess.run([sys.executable,'-B','-X','utf8',str(SCRIPT),'--project',str(self.project),
            '--allow-test-faults','apply','--request',str(path)],capture_output=True,text=True,encoding='utf-8',timeout=20,
            env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',ADAPTIVE_V4_TEST_FAULT='before_commit'))
        self.assertEqual(proc.returncode,91,proc.stdout+proc.stderr)

    def test_crash_before_prepare_commit_does_not_authorize_host_pause(self):
        self.pause();before=self.state();self.crash_transaction(self.request(self.prepare_op()))
        self.assertEqual(module.Runtime(self.project).read(),before)
        self.engine.storage_recover(acknowledge_stopped_writers=True)
        self.assertEqual(self.inspect(0)['decision'],'prepare')

    def test_crash_before_confirm_commit_keeps_pending_durable(self):
        self.pause();self.prepare();before=self.state();self.host('PAUSED')
        self.crash_transaction(self.request(self.confirm_op()))
        self.assertEqual(module.Runtime(self.project).read(),before)
        self.engine.storage_recover(acknowledge_stopped_writers=True)
        self.assertEqual(self.inspect(0)['decision'],'confirm')
        self.assertEqual(self.tx(self.confirm_op())['results'][0]['status'],'confirmed')

    def test_preexisting_host_pause_cannot_be_laundered_by_recent_observation(self):
        self.pause();intent=self.prepare();self.host('PAUSED')
        with self.host_path.open('a',encoding='utf-8') as stream:
            stream.write('updated_at = %d\n'%int((intent['prepared_at_unix']-10)*1000))
        result=self.inspect(1)
        self.assertEqual(result['decision'],'blocked')
        self.assertEqual(result['host_pause_order'],'preexisting_host_pause')
        self.assertFalse(result['strict_order_proven'])
        self.assertNotIn('shutdown.confirm',[a['code'] for a in result['next_actions']])
        self.reject(self.request(self.confirm_op(configuration_updated_at_unix=intent['prepared_at_unix']-10)),
                    'shutdown_order_unproven')

    def test_missing_host_update_time_is_explicitly_order_unproven(self):
        self.pause();self.prepare();self.host('PAUSED');result=self.inspect(0)
        self.assertEqual(result['host_pause_order'],'order_unproven')
        self.assertFalse(result['strict_order_proven'])

    def test_host_update_after_prepare_is_retained_without_authenticated_claim(self):
        self.pause();intent=self.prepare();self.host('PAUSED')
        updated=int(time.time()*1000)+1
        with self.host_path.open('a',encoding='utf-8') as stream: stream.write('updated_at = %d\n'%updated)
        result=self.inspect(0)
        self.assertEqual(result['host_pause_order'],'after_prepare')
        self.assertEqual(result['host']['configuration_updated_at_unix'],updated/1000.)
        self.assertFalse(result['strict_order_proven'])

    def test_recovery_disabled_never_recommends_direct_host_pause(self):
        lease=self.worker()
        self.tx({'op':'scheduler.enable','enabled':False,'expected_scheduler_revision':self.state()['scheduler']['revision']})
        result=self.inspect(0,RECOVERY)
        self.assertEqual(result['decision'],'disabled')
        self.assertNotIn('pause_bound_host',[a['code'] for a in result['next_actions']])
        self.tx(self.checkpoint_op(lease),actor='worker')

    def test_recovery_budget_exhaustion_never_recommends_direct_host_pause(self):
        lease=self.worker()
        self.tx({'op':'budget.set','expected_scheduler_revision':self.state()['scheduler']['revision'],'budgets':{'max_wakes':1}})
        wake=self.tx({'op':'wake.begin','id':'last-wake','lease_seconds':60},actor='coordinator')['results'][0]
        self.tx({'op':'wake.end','outcome':'progress'},actor='coordinator',wake_id=wake['wake_id'],wake_fence=wake['wake_fence'])
        result=self.inspect(0,RECOVERY)
        self.assertEqual(result['decision'],'budget')
        self.assertNotIn('pause_bound_host',[a['code'] for a in result['next_actions']])
        self.tx(self.checkpoint_op(lease),actor='worker')

    def test_recovery_completed_requires_protocol_instead_of_direct_host_pause(self):
        self.complete();result=self.inspect(0,RECOVERY)
        self.assertEqual(result['decision'],'completed')
        self.assertNotIn('pause_bound_host',[a['code'] for a in result['next_actions']])
        self.assertIn('inspect_shutdown_protocol',[a['code'] for a in result['next_actions']])

    def test_runtime_plan_requires_prepare_before_completed_host_pause(self):
        self.complete();plan=self.engine.plan()
        self.assertFalse(plan['should_pause_host']);self.assertTrue(plan['shutdown_protocol_required'])
        self.assertEqual(plan['shutdown_closure_basis'],'completed')
        self.prepare();plan=self.engine.plan()
        self.assertTrue(plan['should_pause_host']);self.assertEqual(plan['shutdown_current_intent_ids'],['close-a'])
        self.tx(self.confirm_op());self.assertFalse(self.engine.plan()['should_pause_host'])

    def test_runtime_plan_pause_is_not_host_permission_until_prepared(self):
        self.pause();plan=self.engine.plan()
        self.assertFalse(plan['should_pause_host']);self.assertEqual(plan['shutdown_closure_basis'],'paused')
        self.prepare();self.assertTrue(self.engine.plan()['should_pause_host'])
        self.resume();self.assertFalse(self.engine.plan()['should_pause_host'])

    def test_runtime_plan_disabled_and_budget_are_not_global_closure(self):
        self.tx({'op':'budget.set','expected_scheduler_revision':self.state()['scheduler']['revision'],'budgets':{'max_wakes':1}})
        wake=self.tx({'op':'wake.begin','id':'last-wake','lease_seconds':60},actor='coordinator')['results'][0]
        self.tx({'op':'wake.end','outcome':'progress'},actor='coordinator',wake_id=wake['wake_id'],wake_fence=wake['wake_fence'])
        plan=self.engine.plan();self.assertEqual(plan['decision'],'budget')
        self.assertFalse(plan['should_pause_host']);self.assertIsNone(plan['shutdown_closure_basis'])
        self.tx({'op':'scheduler.enable','enabled':False,'expected_scheduler_revision':self.state()['scheduler']['revision']})
        plan=self.engine.plan();self.assertEqual(plan['decision'],'disabled')
        self.assertFalse(plan['should_pause_host']);self.assertIsNone(plan['shutdown_closure_basis'])


if __name__ == '__main__': unittest.main()
