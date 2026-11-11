"""Independent restart checks: isolated processes and synthetic native host files.

No test quits the desktop application, creates real automations or touches trial
evidence. Application exit/reopen is a separate, explicitly observed live trial.
"""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT/'adaptive-project/scripts/runtime.py'
RECOVERY = ROOT/'adaptive-project/scripts/recovery.py'
spec = importlib.util.spec_from_file_location('restart_v4_independent', SCRIPT)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class RestartV4Tests(unittest.TestCase):
    def setUp(self):
        parent = ROOT/'work/restart-tests'
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='case-', dir=str(parent))
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = self.base/'project'
        self.project.mkdir()
        self.host_root = self.base/'automations'
        self.host_path = self.host_root/'restart-fixture'/'automation.toml'
        self.seq = 0
        self.write('authority.md', 'Synthetic isolated test. No native action authorization.')
        self.write('evidence.md', 'Synthetic observation, not a real host receipt.')
        self.engine = module.Runtime(self.project)
        self.charter = {
            'project_id':'restart-fixture','objective':'Persist and resume a bounded project',
            'acceptance':['Durable state and controls remain intact'],
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
            'exploration':{'budgets':{'max_candidates':3,'max_validations':3,'max_cost':5}}
        }
        self.engine.init(self.charter)
        self.tx({'op':'scheduler.bind','automation_id':'restart-fixture',
                 'host_id':'local','thread_id':'coordinator-thread',
                 'expected_scheduler_revision':1,'evidence':['evidence.md']})
        self.tx({'op':'scheduler.enable','enabled':True,
                 'expected_scheduler_revision':self.state()['scheduler']['revision']})
        self.tx({'op':'unit.create','id':'production','name':'Production','mission':'Isolated worker',
                 'creation_reason':'Test uncertain external dispatch','exit_condition':'Checks complete'})
        self.host()

    def write(self, name, text):
        p = self.project/name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding='utf-8')
        return name

    def host(self, status='ACTIVE', thread='coordinator-thread', automation_id='restart-fixture', extra=''):
        self.host_path.parent.mkdir(parents=True, exist_ok=True)
        self.host_path.write_text('version = 1\nid = '+json.dumps(automation_id)+'\nkind = "heartbeat"\n'
            +'status = '+json.dumps(status)+'\ntarget_thread_id = '+json.dumps(thread)
            +'\nrrule = "FREQ=MINUTELY;INTERVAL=4"\n'+extra, encoding='utf-8')

    def state(self):return self.engine.read()

    def request(self, *ops, **extra):
        self.seq += 1
        actor = extra.pop('actor', 'operator')
        return dict(actor_id=actor,idempotency_key='restart-test-'+str(self.seq),
                    reason='Independent isolated restart fixture',operations=list(ops),**extra)

    def tx(self, *ops, **extra):return self.engine.transact(self.request(*ops, **extra))

    def run_runtime(self, command, request=None, expected=0, fault=False, extra_args=()):
        argv = [sys.executable,'-B','-X','utf8',str(SCRIPT),'--project',str(self.project)]
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        if fault:
            argv.append('--allow-test-faults');env['ADAPTIVE_V4_TEST_FAULT']='before_commit'
        argv.append(command)
        argv.extend(extra_args)
        if request is not None:
            path = self.base/('request-'+str(self.seq)+'.json')
            path.write_text(json.dumps(request), encoding='utf-8')
            argv.extend(['--request',str(path)])
        result = subprocess.run(argv,capture_output=True,text=True,encoding='utf-8',env=env,timeout=20)
        self.assertEqual(result.returncode,expected,result.stdout+result.stderr)
        if expected==91:return result
        return json.loads(result.stdout if result.stdout.strip() else result.stderr)

    def snapshot(self):
        return {p.relative_to(self.base).as_posix():(hashlib.sha256(p.read_bytes()).hexdigest(),p.stat().st_mtime_ns)
                for p in self.base.rglob('*') if p.is_file()}

    def inspect(self, expected=0):
        before = self.snapshot()
        proc = subprocess.run([sys.executable,'-B','-X','utf8',str(RECOVERY),
            '--project',str(self.project),'--automation-root',str(self.host_root)],
            capture_output=True,text=True,encoding='utf-8',timeout=20)
        self.assertEqual(proc.returncode,expected,proc.stdout+proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(self.snapshot(),before,'Inspection modified project or host files')
        self.assertTrue(result['read_only']);self.assertEqual(result['automatic_actions'],[])
        return result

    def assert_preserved(self, result, state):
        p = result['preserved'];s = state['scheduler'];e = state['exploration']
        for key,value in [('control',state['control']),('binding',s['binding']),
            ('scheduler_usage',s['usage']),('scheduler_budgets',s['budgets']),
            ('scheduler_budget_ceiling',s['budget_ceiling']),('exploration_usage',e['usage']),
            ('exploration_budgets',e['budgets']),('exploration_budget_ceiling',e['budget_ceiling']),
            ('expires_at',s['expires_at']),('not_before',s['not_before']),('next_wake_at',s['next_wake_at'])]:
            self.assertEqual(p[key],value,key)

    def wake(self, wid='wake-a', ttl=30):
        return self.tx({'op':'wake.begin','id':wid,'lease_seconds':ttl},actor='coordinator')['results'][0]

    def credentials(self, wake):return {'actor':'coordinator','wake_id':wake['wake_id'],'wake_fence':wake['wake_fence']}

    def spend_exploration(self):
        self.tx({'op':'question.add','id':'q','question':'Which approach is useful?','importance':'Determines next work'})
        self.tx({'op':'candidate.add','id':'c','title':'Candidate','proposal':'Observe fixture',
                 'assumptions':['Local fixture only'],'scope_fit':'Test scope','question_ids':['q']})
        self.tx({'op':'validation.plan','id':'v','candidate_id':'c','hypothesis':'Fixture is usable',
                 'method':'Read fixture','criteria':{'positive':'Observed','negative':'Not observed','inconclusive':'Unknown'},
                 'cost':2,'inputs':['evidence.md']})
        self.tx({'op':'validation.start','id':'v','expected_validation_revision':1})

    def test_ready_new_process_preserves_identity_and_every_budget(self):
        self.spend_exploration();before=self.state()
        result=self.inspect()
        self.assertEqual(result['decision'],'ready');self.assertEqual(result['host']['status'],'active')
        self.assert_preserved(result,before);self.assertEqual(self.run_runtime('status'),before)

    def test_paused_survives_reopen_without_resume_or_budget_refund(self):
        self.spend_exploration();wake=self.wake()
        self.tx({'op':'checkpoint','id':'before-pause','data':{'next':'wait for authorized resume'}},**self.credentials(wake))
        self.tx({'op':'control.pause','expected_control_revision':self.state()['control']['revision']})
        self.host('PAUSED');before=self.state();result=self.inspect()
        self.assertEqual(result['decision'],'paused');self.assert_preserved(result,before)
        reopened=self.run_runtime('status');self.assertEqual(reopened,before)
        self.assertEqual(reopened['scheduler']['usage']['wakes'],1)
        self.assertEqual(reopened['exploration']['usage']['cost'],2)
        denied=self.run_runtime('apply',self.request({'op':'checkpoint','id':'no-resume','data':{}},actor='coordinator'),1)
        self.assertEqual(denied['code'],'paused')

    def test_paused_runtime_with_active_host_still_does_not_admit_work(self):
        self.tx({'op':'control.pause','expected_control_revision':self.state()['control']['revision']})
        result=self.inspect();self.assertEqual(result['decision'],'paused')
        attempt=self.wake();self.assertFalse(attempt['admitted'])
        self.assertEqual(self.state()['scheduler']['usage']['wakes'],0)

    def test_host_pause_is_not_implicitly_reenabled(self):
        self.host('PAUSED');before=self.state();result=self.inspect()
        self.assertEqual(result['decision'],'host_paused');self.assert_preserved(result,before)
        self.assertNotIn('enable',[a['code'] for a in result['next_actions']])

    def test_missing_host_blocks_instead_of_creating_replacement(self):
        self.host_path.unlink();result=self.inspect(1)
        self.assertEqual(result['decision'],'blocked');self.assertEqual(result['host']['status'],'missing')
        self.assertFalse(self.host_path.exists())

    def test_wrong_thread_blocks_resume(self):
        self.host(thread='another-thread');result=self.inspect(1)
        self.assertEqual(result['decision'],'blocked');self.assertEqual(result['host']['status'],'mismatch')

    def test_wrong_automation_identity_blocks_resume(self):
        self.host(automation_id='another-automation');result=self.inspect(1)
        self.assertEqual(result['decision'],'blocked');self.assertEqual(result['host']['status'],'mismatch')

    def test_unknown_host_status_fails_closed(self):
        self.host('MAYBE');result=self.inspect(1)
        self.assertEqual(result['decision'],'blocked');self.assertEqual(result['host']['status'],'unsupported')

    def test_duplicate_host_field_cannot_hide_pause(self):
        self.host('PAUSED',extra='status = "ACTIVE"\n');result=self.inspect(1)
        self.assertEqual(result['decision'],'blocked');self.assertEqual(result['host']['status'],'unsupported')

    def test_incomplete_or_wrong_typed_native_config_fails_closed(self):
        self.host();original=self.host_path.read_text(encoding='utf-8')
        variants=[original.replace('version = 1','version = true'),
                  '\n'.join(line for line in original.splitlines() if not line.startswith('rrule ='))]
        for index,body in enumerate(variants):
            with self.subTest(case=index):
                self.host_path.write_text(body,encoding='utf-8');result=self.inspect(1)
                self.assertEqual(result['decision'],'blocked');self.assertEqual(result['host']['status'],'unsupported')

    def test_missing_database_does_not_initialize_project(self):
        missing=self.base/'not-initialized'
        proc=subprocess.run([sys.executable,'-B','-X','utf8',str(RECOVERY),'--project',str(missing),
            '--automation-root',str(self.host_root)],capture_output=True,text=True,encoding='utf-8',timeout=20)
        self.assertEqual(proc.returncode,1,proc.stdout+proc.stderr)
        self.assertFalse(missing.exists(),'Recovery inspection initialized a missing project')
        self.assertTrue(json.loads(proc.stdout)['read_only'])

    def test_live_wake_waits_and_second_process_cannot_steal_it(self):
        first=self.wake();before=self.state();result=self.inspect()
        self.assertEqual(result['decision'],'wait');self.assertEqual(result['wake']['status'],'live')
        second=self.run_runtime('apply',self.request({'op':'wake.begin','id':'wake-b'},actor='coordinator'))
        self.assertFalse(second['results'][0]['admitted'])
        after=self.state();self.assertEqual(after['scheduler']['usage'],before['scheduler']['usage'])
        self.assertEqual(after['scheduler']['active_wake']['id'],first['wake_id'])
        self.assertEqual(after['scheduler']['fencing_token'],before['scheduler']['fencing_token'])

    def test_expired_wake_and_task_recover_with_fencing_no_budget_reset(self):
        self.tx({'op':'task.create','id':'task','title':'Survive an interrupted process',
                 'owner_unit_id':'coordinator','acceptance':['Durable result']})
        first=self.wake(ttl=.15)
        claim=self.tx({'op':'task.claim','id':'task','expected_task_revision':1,'lease_seconds':.15},**self.credentials(first))['results'][0]
        time.sleep(.22)
        result=self.inspect();self.assertEqual(result['wake']['status'],'expired')
        self.assertEqual(len(result['tasks']['expired_leases']),1)
        second=self.run_runtime('apply',self.request({'op':'wake.begin','id':'wake-b'},actor='coordinator'))['results'][0]
        self.assertGreater(second['wake_fence'],first['wake_fence'])
        creds=self.credentials(second)
        recovered=self.run_runtime('apply',self.request({'op':'recover'},**creds))
        self.assertEqual(recovered['results'][0]['recovered_tasks'],['task'])
        task=next(t for t in self.state()['tasks'] if t['id']=='task')
        claimed=self.tx({'op':'task.claim','id':'task','expected_task_revision':task['revision'],'lease_seconds':30},**creds)['results'][0]
        self.assertGreater(claimed['fencing_token'],claim['fencing_token'])
        old_op={'op':'task.checkpoint','id':'task','lease_id':claim['lease_id'],'fencing_token':claim['fencing_token'],'data':{'stale':True}}
        denied=self.run_runtime('apply',self.request(old_op,**creds),1)
        self.assertEqual(denied['code'],'stale_lease')
        old_wake=self.run_runtime('apply',self.request({'op':'checkpoint','id':'old-wake','data':{}},**self.credentials(first)),1)
        self.assertEqual(old_wake['code'],'stale_wake')
        self.assertEqual(self.state()['scheduler']['usage'],{'wakes':2,'claims':2,'dispatches':0})
        self.assertEqual(self.state()['scheduler']['expires_at'],self.charter['scheduler']['expires_at'])

    def make_uncertain(self, status):
        self.tx({'op':'outbox.enqueue','id':'uncertain','kind':'create_chat','payload':{'unit_id':'production','prompt':'Fixture only'}})
        request=self.request({'op':'outbox.begin','id':'uncertain','expected_outbox_revision':1})
        self.engine.transact(request)
        if status=='unknown':
            self.tx({'op':'outbox.resolve','id':'uncertain','expected_outbox_revision':2,'status':'unknown','result':{}})
        return request

    def test_inflight_outbox_requires_reconciliation_and_cannot_replay(self):
        request=self.make_uncertain('inflight');before=self.state();result=self.inspect(1)
        self.assertEqual(result['decision'],'reconcile');self.assertEqual(len(result['outbox']['inflight']),1)
        self.assertIn('reconcile_outbox',[a['code'] for a in result['next_actions']])
        denied=self.run_runtime('apply',request,1);self.assertEqual(denied['code'],'dispatch_ambiguous')
        self.assertEqual(self.state(),before)

    def test_unknown_outbox_preserves_spent_dispatch_budget(self):
        self.make_uncertain('unknown');before=self.state();result=self.inspect(1)
        self.assertEqual(result['decision'],'reconcile');self.assertEqual(len(result['outbox']['unknown']),1)
        self.assert_preserved(result,before);self.assertEqual(before['scheduler']['usage']['dispatches'],1)

    def test_budget_exhausted_stays_exhausted_across_processes(self):
        self.tx({'op':'budget.set','expected_scheduler_revision':self.state()['scheduler']['revision'],'budgets':{'max_wakes':1}})
        wake=self.wake();self.tx({'op':'wake.end','outcome':'progress'},**self.credentials(wake))
        result=self.inspect();self.assertEqual(result['decision'],'budget')
        denied=self.run_runtime('apply',self.request({'op':'wake.begin','id':'over-budget'},actor='coordinator'))
        self.assertFalse(denied['results'][0]['admitted']);self.assertEqual(self.state()['scheduler']['usage']['wakes'],1)

    def test_disabled_scheduler_does_not_resume_on_active_host(self):
        self.tx({'op':'scheduler.enable','enabled':False,'expected_scheduler_revision':self.state()['scheduler']['revision']})
        result=self.inspect();self.assertEqual(result['decision'],'disabled')

    def test_pending_outbox_is_not_confused_with_unknown_side_effect(self):
        self.tx({'op':'outbox.enqueue','id':'pending','kind':'create_chat','payload':{'unit_id':'production','prompt':'Not dispatched'}})
        result=self.inspect();self.assertEqual(len(result['outbox']['pending']),1)
        self.assertEqual(self.state()['scheduler']['usage']['dispatches'],0)

    def test_committed_idempotency_key_survives_new_process(self):
        request=self.request({'op':'checkpoint','id':'saved','data':{'next':'continue'}})
        first=self.run_runtime('apply',request);before=self.state()
        second=self.run_runtime('apply',request)
        self.assertFalse(first['replayed']);self.assertTrue(second['replayed']);self.assertEqual(self.state(),before)

    def test_process_exit_before_commit_rolls_back_without_budget_loss(self):
        before=self.state();request=self.request({'op':'wake.begin','id':'crashed-wake'},actor='coordinator')
        self.run_runtime('apply',request,expected=91,fault=True)
        reopened=self.run_runtime('status');self.assertEqual(reopened,before)
        committed=self.run_runtime('apply',request);self.assertTrue(committed['results'][0]['admitted'])
        self.assertEqual(self.state()['scheduler']['usage']['wakes'],1)

    def test_terminated_process_keeps_committed_checkpoint_and_usage(self):
        self.spend_exploration();wake=self.wake()
        request=self.request({'op':'checkpoint','id':'before-process-kill','data':{'next':'resume from this durable checkpoint'}},**self.credentials(wake))
        request_file=self.base/'kill-request.json';request_file.write_text(json.dumps(request),encoding='utf-8')
        marker=self.base/'committed.json';helper=self.base/'committed-child.py'
        helper.write_text('import importlib.util,json,sys,time\nfrom pathlib import Path\n'
            'sys.dont_write_bytecode=True\n'
            'script,project,request,marker=sys.argv[1:]\n'
            'spec=importlib.util.spec_from_file_location("restart_kill_fixture",script)\n'
            'm=importlib.util.module_from_spec(spec);sys.modules[spec.name]=m;spec.loader.exec_module(m)\n'
            'result=m.Runtime(project).transact(json.loads(Path(request).read_text(encoding="utf-8")))\n'
            'pending=Path(marker+".tmp");pending.write_text(json.dumps(result["state"]),encoding="utf-8");pending.replace(marker)\n'
            'while True:time.sleep(.1)\n',encoding='utf-8')
        child=subprocess.Popen([sys.executable,'-B','-X','utf8',str(helper),str(SCRIPT),str(self.project),str(request_file),str(marker)],
                               stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            deadline=time.monotonic()+10
            while not marker.exists() and child.poll() is None and time.monotonic()<deadline:time.sleep(.005)
            self.assertTrue(marker.exists(),'Child did not commit before bounded deadline')
            child.kill();child.wait(timeout=5)
            saved=json.loads(marker.read_text(encoding='utf-8'));reopened=self.run_runtime('status')
            self.assertEqual(reopened,saved)
            self.assertEqual(reopened['scheduler']['usage']['wakes'],1)
            self.assertEqual(reopened['exploration']['usage']['cost'],2)
            self.assertEqual(reopened['checkpoints'][-1]['id'],'before-process-kill')
            self.assertEqual(self.inspect()['decision'],'wait','A killed process does not justify stealing a still-live wake')
        finally:
            if child.poll() is None:child.kill();child.wait(timeout=5)
            child.communicate(timeout=5)

    def test_expired_project_does_not_extend_deadline_on_reopen(self):
        self.project=self.base/'expired-project';self.project.mkdir()
        self.write('authority.md','Isolated expiry test');self.write('evidence.md','Synthetic host fixture')
        charter=copy.deepcopy(self.charter);charter['scope']={'project_root':str(self.project),'write_roots':[str(self.project)]}
        charter['scheduler']['expires_at']=time.time()+.2
        self.engine=module.Runtime(self.project);self.engine.init(charter)
        self.tx({'op':'scheduler.bind','automation_id':'restart-fixture','host_id':'local','thread_id':'coordinator-thread',
                 'expected_scheduler_revision':1,'evidence':['evidence.md'],'enabled':True})
        time.sleep(.25);before=self.state();result=self.inspect()
        self.assertEqual(result['decision'],'expired');self.assert_preserved(result,before)
        denied=self.run_runtime('apply',self.request({'op':'wake.begin','id':'too-late'},actor='coordinator'))
        self.assertFalse(denied['results'][0]['admitted']);self.assertEqual(self.state()['scheduler']['expires_at'],charter['scheduler']['expires_at'])

    def test_hot_journal_is_preserved_until_original_runtime_recovers(self):
        before=self.state();request=self.request({'op':'wake.begin','id':'journal-wake'},actor='coordinator')
        self.run_runtime('apply',request,expected=91,fault=True)
        journal=self.project/'.adaptive-v4/state.sqlite3-journal'
        self.assertTrue(journal.exists(),'Fault injection did not leave a rollback journal')
        digest=hashlib.sha256(journal.read_bytes()).hexdigest()
        result=self.inspect(1)
        self.assertEqual(result['reason'],'storage_recovery_required')
        self.assertEqual(hashlib.sha256(journal.read_bytes()).hexdigest(),digest)
        self.assertEqual(self.run_runtime('status'),before)
        # A normal SELECT does not necessarily clear a cold rollback journal.
        self.run_runtime('storage-recover',extra_args=['--acknowledge-stopped-writers'])
        self.assertFalse(journal.exists())
        self.assertEqual(self.run_runtime('status'),before)
        self.assertEqual(self.inspect()['decision'],'ready')

    def test_changed_authority_cannot_become_ready_on_restart(self):
        self.write('authority.md','Changed after binding; not silently authorized')
        result=self.inspect(1)
        self.assertEqual(result['decision'],'blocked');self.assertEqual(result['reason'],'stale_authority')

    def test_missing_host_does_not_erase_durable_pause(self):
        self.tx({'op':'control.pause','expected_control_revision':self.state()['control']['revision']})
        self.host_path.unlink();result=self.inspect(1)
        self.assertEqual(result['decision'],'paused');self.assertEqual(result['host']['status'],'missing')
        self.assertEqual(self.run_runtime('status')['control']['status'],'paused')

    def test_unsafe_automation_identity_is_not_used_as_a_path(self):
        self.tx({'op':'scheduler.bind','automation_id':'../outside','host_id':'local','thread_id':'coordinator-thread',
                 'expected_scheduler_revision':self.state()['scheduler']['revision'],'evidence':['evidence.md'],'enabled':True})
        result=self.inspect(1)
        self.assertEqual(result['decision'],'blocked');self.assertEqual(result['host']['status'],'unsupported')
        self.assertNotIn('configuration_path',result['host'])

    def test_memory_and_paid_validation_are_recovered_as_existing_records(self):
        self.spend_exploration()
        self.tx({'op':'memory.put','id':'durable-memory','kind':'fact','statement':'Read existing evidence before continuing',
                 'sources':['evidence.md'],'confidence':'high'})
        self.tx({'op':'checkpoint','id':'durable-plan','data':{'validation_id':'v','next':'collect results, do not restart'}})
        before=self.state();result=self.inspect()
        self.assertEqual([m['id'] for m in result['memories']],['durable-memory'])
        self.assertEqual(result['memories'][0]['sources'],before['memories'][0]['sources'])
        self.assertEqual(result['checkpoints'][-1]['data']['validation_id'],'v')
        self.assertEqual(self.run_runtime('status')['exploration'],before['exploration'])
        self.assertEqual(before['exploration']['usage']['validations'],1)

    def test_completed_project_stays_closed_even_if_native_host_is_active(self):
        self.tx({'op':'task.create','id':'finished','title':'Terminal fixture','owner_unit_id':'coordinator','acceptance':['Fixture terminal']})
        self.tx({'op':'task.cancel','id':'finished','expected_task_revision':1,'reason':'Fixture complete','evidence':['evidence.md']})
        self.tx({'op':'decision.record','id':'stop','expected_exploration_revision':self.state()['exploration']['revision'],
                 'action':'stop','alternatives':[],'result_ids':[],'rationale':'No more fixture work','evidence':['evidence.md'],
                 'task_dispositions':[],'residual_question_ids':[]})
        self.tx({'op':'project.complete','evidence':['evidence.md'],'acceptance_results':[
            {'criterion':self.charter['acceptance'][0],'passed':True,'evidence':['evidence.md']}]})
        before=self.state();result=self.inspect()
        self.assertEqual(result['decision'],'completed');self.assert_preserved(result,before)
        self.assertNotIn('pause_bound_host',[a['code'] for a in result['next_actions']])
        self.assertIn('inspect_shutdown_protocol',[a['code'] for a in result['next_actions']])
        self.assertEqual(self.run_runtime('status'),before)

    def database_contents(self):
        database=self.project/'.adaptive-v4/state.sqlite3'
        connection=sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True)
        try:
            return {'state':connection.execute('SELECT * FROM project ORDER BY id').fetchall(),
                    'requests':connection.execute('SELECT * FROM requests ORDER BY key').fetchall(),
                    'blobs':connection.execute('SELECT * FROM blobs ORDER BY id').fetchall(),
                    'user_version':connection.execute('PRAGMA user_version').fetchone()[0],
                    'schema_version':connection.execute('PRAGMA schema_version').fetchone()[0]}
        finally:connection.close()

    def test_storage_recovery_requires_explicit_stopped_writer_acknowledgment(self):
        before=self.snapshot()
        self.run_runtime('storage-recover',expected=1)
        self.assertEqual(self.snapshot(),before)

    def test_storage_recovery_preserves_paused_state_all_rows_and_versions(self):
        self.spend_exploration()
        self.tx({'op':'budget.set','expected_scheduler_revision':self.state()['scheduler']['revision'],'budgets':{'max_wakes':1}})
        wake=self.wake()
        self.tx({'op':'task.create','id':'artifact','title':'Accepted fixture bytes','owner_unit_id':'coordinator','acceptance':['Correct bytes']},**self.credentials(wake))
        claim=self.tx({'op':'task.claim','id':'artifact','expected_task_revision':1},**self.credentials(wake))['results'][0]
        artifact=self.write(claim['attempt_dir']+'/result.txt','Accepted bytes must remain intact across storage maintenance.')
        submitted=self.tx({'op':'task.submit','id':'artifact','expected_task_revision':claim['task_revision'],
            'lease_id':claim['lease_id'],'fencing_token':claim['fencing_token'],'artifacts':[artifact],'summary':'Fixture written'},**self.credentials(wake))['results'][0]
        self.tx({'op':'task.review','id':'artifact','expected_task_revision':submitted['task_revision'],
                 'verdict':'pass','method':'Known fixture bytes','evidence':['evidence.md']},**self.credentials(wake))
        self.tx({'op':'control.pause','expected_control_revision':self.state()['control']['revision']})
        self.host('PAUSED');before=self.database_contents();state=self.state()
        self.assertTrue(before['blobs'])
        uncommitted=self.request({'op':'control.pause','expected_control_revision':state['control']['revision']})
        self.run_runtime('apply',uncommitted,expected=91,fault=True)
        recovered=self.run_runtime('storage-recover',extra_args=['--acknowledge-stopped-writers'])
        self.assertTrue(recovered['had_journal']);self.assertTrue(recovered['logical_content_unchanged'])
        self.assertFalse(recovered['project_transaction_created'])
        self.assertEqual(self.database_contents(),before)
        self.assertEqual(self.state(),state)
        self.run_runtime('storage-recover',extra_args=['--acknowledge-stopped-writers'])
        self.assertEqual(self.database_contents(),before,'Repeated storage recovery changed logical records')
        self.assertEqual(self.inspect()['decision'],'paused')

    def test_storage_recovery_refuses_wal_sidecar_without_removing_it(self):
        sidecar=self.project/'.adaptive-v4/state.sqlite3-wal'
        sidecar.write_bytes(b'Unsupported WAL fixture; do not remove')
        before=self.snapshot()
        self.run_runtime('storage-recover',expected=1,extra_args=['--acknowledge-stopped-writers'])
        self.assertEqual(self.snapshot(),before)

    def test_storage_recovery_rejects_database_symlink_even_inside_project(self):
        database=self.project/'.adaptive-v4/state.sqlite3';original=database.with_name('original.sqlite3')
        database.rename(original)
        try:
            try:database.symlink_to(original)
            except (OSError,NotImplementedError) as error:
                original.rename(database)
                self.skipTest('OS does not allow this isolated file symlink: '+str(error))
            digest=hashlib.sha256(original.read_bytes()).hexdigest()
            result=self.run_runtime('storage-recover',expected=1,extra_args=['--acknowledge-stopped-writers'])
            self.assertEqual(result['code'],'unsafe_path')
            self.assertEqual(hashlib.sha256(original.read_bytes()).hexdigest(),digest)
        finally:
            if database.is_symlink():database.unlink()
            if original.exists():original.rename(database)

    def test_storage_recovery_rejects_metadata_directory_junction_or_symlink(self):
        directory=self.project/'.adaptive-v4';original=self.project/'original-metadata'
        self.assertEqual(directory.resolve().parent,self.project.resolve())
        directory.rename(original);created=False
        try:
            if os.name=='nt':
                # Only create an isolated junction; cleanup removes this exact
                # verified link, never recursively deleting the target.
                quote=lambda value:"'"+str(value).replace("'","''")+"'"
                script='New-Item -ItemType Junction -Path '+quote(directory)+' -Target '+quote(original)+' | Out-Null'
                creation=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',script],capture_output=True)
                created=creation.returncode==0 and directory.exists()
                if not created:self.skipTest('OS denied isolated directory junction creation')
            else:
                try:directory.symlink_to(original,target_is_directory=True);created=True
                except (OSError,NotImplementedError) as error:self.skipTest('OS denied directory symlink: '+str(error))
            digest=hashlib.sha256((original/'state.sqlite3').read_bytes()).hexdigest()
            result=self.run_runtime('storage-recover',expected=1,extra_args=['--acknowledge-stopped-writers'])
            self.assertEqual(result['code'],'unsafe_path')
            self.assertEqual(hashlib.sha256((original/'state.sqlite3').read_bytes()).hexdigest(),digest)
        finally:
            if created:
                if os.name=='nt':
                    # rmdir on an NTFS junction removes the link without its target.
                    os.rmdir(str(directory))
                else:directory.unlink()
            if original.exists():original.rename(directory)

    def test_storage_recovery_refuses_an_active_database_writer(self):
        request=self.request({'op':'checkpoint','id':'writer-held','data':{'committed_after_release':True}})
        path=self.base/'writer-request.json';path.write_text(json.dumps(request),encoding='utf-8')
        marker=self.base/'writer-held';gate=self.base/'writer-release';helper=self.base/'held-writer.py'
        helper.write_text('import importlib.util,json,sys,time\nfrom pathlib import Path\n'
            'sys.dont_write_bytecode=True\nscript,project,request,marker,gate=sys.argv[1:]\n'
            'spec=importlib.util.spec_from_file_location("held_writer_fixture",script)\n'
            'm=importlib.util.module_from_spec(spec);sys.modules[spec.name]=m;spec.loader.exec_module(m)\n'
            'class Held(m.Runtime):\n'
            ' def operation(self,*args):\n'
            '  result=super().operation(*args);Path(marker).write_text("held");deadline=time.monotonic()+30\n'
            '  while not Path(gate).exists():\n'
            '   if time.monotonic()>deadline:raise RuntimeError("Bounded writer release expired")\n'
            '   time.sleep(.01)\n'
            '  return result\n'
            'Held(project).transact(json.loads(Path(request).read_text(encoding="utf-8")))\n',encoding='utf-8')
        child=subprocess.Popen([sys.executable,'-B','-X','utf8',str(helper),str(SCRIPT),str(self.project),str(path),str(marker),str(gate)],
                               stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            deadline=time.monotonic()+10
            while not marker.exists() and child.poll() is None and time.monotonic()<deadline:time.sleep(.005)
            self.assertTrue(marker.exists(),'Writer did not acquire its runtime transaction')
            self.run_runtime('storage-recover',expected=1,extra_args=['--acknowledge-stopped-writers'])
            gate.write_text('release',encoding='ascii');stdout,stderr=child.communicate(timeout=10)
            self.assertEqual(child.returncode,0,stderr.decode('utf-8'))
            self.assertEqual(self.state()['checkpoints'][-1]['id'],'writer-held')
        finally:
            if child.poll() is None:child.kill();child.wait(timeout=5)
            child.communicate(timeout=5)

    def test_competing_reopened_processes_only_one_wake_is_admitted(self):
        gate=self.base/'go';ready=[];processes=[]
        for index in range(2):
            request=self.request({'op':'wake.begin','id':'competing-'+str(index)},actor='coordinator')
            request_file=self.base/('parallel-'+str(index)+'.json');request_file.write_text(json.dumps(request),encoding='utf-8')
            marker=self.base/('ready-'+str(index));ready.append(marker)
            argv=[sys.executable,'-B','-X','utf8',str(ROOT/'tests/fixtures/parallel_worker.py'),
                  str(SCRIPT),str(self.project),str(request_file),str(marker),str(gate)]
            p=subprocess.Popen(argv,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8')
            processes.append(p)
        try:
            deadline=time.monotonic()+10
            while not all(p.exists() for p in ready) and time.monotonic()<deadline:time.sleep(.005)
            self.assertTrue(all(p.exists() for p in ready));gate.write_text('go',encoding='ascii')
            results=[]
            for p in processes:
                stdout,stderr=p.communicate(timeout=20);self.assertEqual(p.returncode,0,stderr)
                results.append(json.loads(stdout)['results'][0])
            self.assertEqual(sum(r['admitted'] for r in results),1)
            self.assertEqual(self.state()['scheduler']['usage']['wakes'],1)
        finally:
            for p in processes:
                if p.poll() is None:p.kill();p.wait(timeout=5)


if __name__=='__main__':unittest.main()
