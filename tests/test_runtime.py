"""Independent checks of exploration decisions, budgets and durable control."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'adaptive-project/scripts/runtime.py'
spec = importlib.util.spec_from_file_location('adaptive_v4_independent', SCRIPT)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
Runtime = module.Runtime
RuntimeErrorDetail = module.RuntimeErrorDetail


class RuntimeBehaviorTests(unittest.TestCase):
    def setUp(self):
        folder = ROOT / 'work/runtime-v4-tests'
        folder.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='v4-', dir=str(folder))
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / 'project'
        self.project.mkdir()
        self.sequence = 0
        self.write('inputs/authorization.md', 'Isolated local test authorization; not external permission.')
        self.write('inputs/brief.md', 'Find and demonstrate a useful local workflow improvement.')
        self.write('evidence/observation.md', 'Observed fixtures, not claims about real users.')
        self.write('evidence/binding.md', 'Fixture-only binding; no live automation.')
        self.charter = {
            'project_id': 'v4-test', 'objective': 'Find a useful workflow improvement',
            'acceptance': ['Evidence and final artifact reviewed'],
            'scope': {'project_root': str(self.project), 'write_roots': [str(self.project)]},
            'authority': {'status': 'bound', 'source_refs': ['inputs/authorization.md'],
                'adjust_units_within_scope': True, 'maintain_project_memory': True,
                'create_long_lived_chats': True, 'message_long_lived_chats': True,
                'configure_future_triggers': True},
            'execution': {'state_writer_id': 'coordinator', 'operator_id': 'operator', 'mode': 'foreground'},
            'limits': {'max_nonretired_units': 6, 'max_active_chats': 4, 'max_running_leases': 3,
                'max_attempts_per_task': 4, 'max_unit_depth': 3, 'max_lease_seconds': 900},
            'scheduler': {'budgets': {'max_wakes': 10, 'max_claims': 10, 'max_dispatches': 10,
                'max_actions_per_wake': 20, 'max_wake_seconds': 60},
                'cooldown_seconds': 0, 'max_backoff_seconds': 1, 'expires_at': time.time()+3600},
        }
        self.runtime = Runtime(self.project)
        self.runtime.init(self.charter)

    def write(self, relative, body='Fixture evidence with a known origin.'):
        path = self.project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding='utf-8')
        return path.relative_to(self.project).as_posix()

    def state(self):
        return self.runtime.read()

    def request(self, *ops, actor='coordinator', **extra):
        self.sequence += 1
        return dict(actor_id=actor, idempotency_key='test-'+str(self.sequence),
                    reason='Independent exploration behavior test', operations=list(ops), **extra)

    def tx(self, *ops, actor='coordinator', **extra):
        return self.runtime.transact(self.request(*ops, actor=actor, **extra))

    def assertRejectedUnchanged(self, request, code=None):
        before = self.state()
        with self.assertRaises(RuntimeErrorDetail) as caught:
            self.runtime.transact(request)
        if code:
            self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.state(), before)
        return caught.exception

    def pause(self):
        return self.tx({'op':'control.pause', 'expected_control_revision':self.state()['control']['revision']}, actor='operator')

    def resume(self):
        return self.tx({'op':'control.resume', 'expected_control_revision':self.state()['control']['revision']}, actor='operator')

    def run_parallel(self, requests):
        folder = Path(tempfile.mkdtemp(prefix='barrier-', dir=str(self.project)))
        gate = folder / 'go'
        ready, processes = [], []
        for index, request in enumerate(requests):
            request_file = folder / ('request-%d.json' % index)
            request_file.write_text(json.dumps(request), encoding='utf-8')
            marker = folder / ('ready-%d' % index)
            ready.append(marker)
            args = [sys.executable, '-X', 'utf8', str(ROOT/'tests/fixtures/parallel_worker.py'),
                    str(SCRIPT), str(self.project), str(request_file), str(marker), str(gate)]
            processes.append(subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                               text=True, encoding='utf-8'))
        deadline = time.monotonic()+10
        while not all(p.exists() for p in ready) and time.monotonic()<deadline:
            time.sleep(.005)
        gate.write_text('go', encoding='ascii')
        results = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            results.append((process.returncode, stdout, stderr))
        self.assertTrue(all(p.exists() for p in ready), results)
        return results

    def test_foreground_does_not_require_fabricated_scheduler_binding(self):
        result = self.tx({'op':'checkpoint', 'id':'foreground-progress', 'data':{'next':'Inspect evidence'}})
        self.assertFalse(result['replayed'])
        self.assertEqual(self.state()['scheduler']['usage']['wakes'], 0)
        self.assertIsNone(self.state()['scheduler']['binding'])

    def test_pause_blocks_foreground_work_and_resume_preserves_usage(self):
        usage = copy.deepcopy(self.state()['scheduler']['usage'])
        self.pause()
        self.assertRejectedUnchanged(self.request({'op':'checkpoint','id':'paused-work','data':{}}), 'paused')
        self.assertRejectedUnchanged(self.request({'op':'control.resume','expected_control_revision':self.state()['control']['revision']}), 'unauthorized')
        self.resume()
        self.tx({'op':'checkpoint','id':'resumed-work','data':{'resumed':True}})
        self.assertEqual(self.state()['scheduler']['usage'], usage)

    def test_read_only_plan_and_reconstruction_preserve_state(self):
        before = self.state()
        self.runtime.plan()
        self.runtime = Runtime(self.project)
        self.runtime.plan()
        self.assertEqual(self.state(), before)

    def test_authorization_change_still_allows_emergency_pause(self):
        self.write('inputs/authorization.md', 'Changed externally after project initialization.')
        self.assertRejectedUnchanged(self.request({'op':'checkpoint','id':'changed-auth','data':{}}), 'stale_input')
        self.pause()
        self.assertEqual(self.state()['control']['status'], 'paused')

    def exploration(self):
        return self.state()['exploration']

    def item(self, collection, item_id):
        return next(x for x in self.exploration()[collection] if x['id']==item_id)

    def question(self, item_id='need'):
        return self.tx({'op':'question.add','id':item_id,'question':'What actually prevents completion?',
                        'importance':'This changes which solution is useful.'})

    def candidate(self, item_id='option-a'):
        if not self.exploration()['questions']:
            self.question()
        return self.tx({'op':'candidate.add','id':item_id,'title':item_id,
                        'proposal':'Test a small workflow intervention.',
                        'assumptions':['The observed bottleneck can be reduced.'],
                        'scope_fit':'Local analysis and prototype, within the original objective.',
                        'question_ids':['need']})

    def validation(self, item_id='check-a', candidate_id='option-a', cost=1, inputs=None):
        if not any(c['id']==candidate_id for c in self.exploration()['candidates']):
            self.candidate(candidate_id)
        return self.tx({'op':'validation.plan','id':item_id,'candidate_id':candidate_id,
                        'hypothesis':'The proposal addresses the observed bottleneck.',
                        'method':'Run the same fixture through the baseline and prototype.',
                        'criteria':{'positive':'Fixture completion improves without losing required data.',
                                    'negative':'Required data is lost or completion does not improve.',
                                    'inconclusive':'Fixture cannot distinguish the alternatives.'},'cost':cost,
                        'inputs':inputs or []})

    def start_request(self, item_id='check-a'):
        return self.request({'op':'validation.start','id':item_id,
                             'expected_validation_revision':self.item('validations',item_id)['revision']})

    def start(self, item_id='check-a'):
        return self.runtime.transact(self.start_request(item_id))

    def finish(self, item_id='check-a', outcome='positive', **extra):
        return self.tx(dict(op='validation.finish',id=item_id,
            expected_validation_revision=self.item('validations',item_id)['revision'],
            outcome=outcome,summary='The observed fixture comparison is '+outcome,
            limitations=['Synthetic fixture cannot establish real user demand.'],
            criteria_assessment={'positive':'Compare preserved fields and completion.',
                                 'negative':'Inspect any dropped fields or unchanged completion.',
                                 'inconclusive':'Note the evidence coverage limits.'},
            evidence=['evidence/observation.md'],**extra))

    def result(self, item_id='check-a', candidate_id='option-a', outcome='positive', cost=1):
        self.validation(item_id,candidate_id,cost)
        self.start(item_id)
        return self.finish(item_id,outcome)

    def decision(self, item_id, action, candidate_id=None, result_ids=None, **extra):
        operation = dict(op='decision.record', id=item_id, action=action,
            expected_exploration_revision=self.exploration()['revision'],
            alternatives=[],result_ids=result_ids or [],rationale='Choose using the observed evidence and scope.',
            evidence=['evidence/observation.md'],task_dispositions=[])
        if candidate_id is not None:
            operation['candidate_id']=candidate_id
        if action in {'stop','deliver'}:
            operation['residual_question_ids']=[q['id'] for q in self.exploration()['questions'] if q['status']=='open']
        operation.update(extra)
        return self.tx(operation)

    def exploration_budget(self, **budgets):
        return self.tx({'op':'exploration.budget.set',
            'expected_exploration_revision':self.exploration()['revision'],'budgets':budgets}, actor='operator')

    def task(self, item_id='draft', candidate_id='option-a'):
        return self.tx({'op':'task.create','id':item_id,'title':'Produce prototype',
            'owner_unit_id':'coordinator','acceptance':['Required fields preserved'],
            'inputs':[{'ref':'inputs/brief.md'}],'candidate_id':candidate_id})

    def claim(self, item_id='draft'):
        task=next(t for t in self.state()['tasks'] if t['id']==item_id)
        return self.tx({'op':'task.claim','id':item_id,'expected_task_revision':task['revision']})['results'][0]

    def done_task(self):
        self.task()
        claim=self.claim()
        artifact=self.write(str(Path(claim['attempt_dir'])/'result.md'),'Prototype evidence and limitations.')
        task=next(t for t in self.state()['tasks'] if t['id']=='draft')
        self.tx({'op':'task.submit','id':'draft','lease_id':claim['lease_id'],
            'fencing_token':claim['fencing_token'],'expected_task_revision':task['revision'],
            'artifacts':[artifact],'summary':'A bounded local prototype.'})
        task=next(t for t in self.state()['tasks'] if t['id']=='draft')
        self.tx({'op':'task.review','id':'draft','expected_task_revision':task['revision'],
            'verdict':'pass','evidence':['evidence/observation.md'],'method':'Inspect the actual artifact.'})

    def complete_request(self):
        return self.request({'op':'project.complete','evidence':['evidence/observation.md'],
            'acceptance_results':[{'criterion':self.charter['acceptance'][0],
                'passed':True,'evidence':['evidence/observation.md']}]})

    def test_candidates_reference_real_questions_and_budget_is_cumulative(self):
        self.question()
        self.exploration_budget(max_candidates=1)
        self.candidate()
        before=self.state()
        with self.assertRaises(RuntimeErrorDetail):self.candidate('option-b')
        self.assertEqual(self.state(),before)
        self.assertEqual(self.exploration()['usage']['candidates'],1)
        self.assertEqual(self.item('candidates','option-a')['evidence_status'],'unvalidated')

    def test_candidate_cannot_reference_missing_question(self):
        self.assertRejectedUnchanged(self.request({'op':'candidate.add','id':'bad','title':'Bad',
            'proposal':'A plausible proposal','assumptions':['Unknown'], 'scope_fit':'Local',
            'question_ids':['missing']}))

    def test_question_resolution_checks_entity_version_and_evidence(self):
        self.question()
        revision=self.item('questions','need')['revision']
        self.tx({'op':'question.resolve','id':'need','expected_question_revision':revision,
            'status':'answered','answer':'The fixture indicates a handoff gap.',
            'evidence':['evidence/observation.md']})
        self.assertEqual(self.item('questions','need')['status'],'answered')
        self.assertRejectedUnchanged(self.request({'op':'question.resolve','id':'need',
            'expected_question_revision':revision,'status':'deferred','answer':'Overwrite stale finding',
            'evidence':['evidence/observation.md']}),'stale_revision')

    def test_validation_must_start_before_result_and_plan_is_immutable(self):
        self.validation()
        initial=self.item('validations','check-a')
        before=self.state()
        with self.assertRaises(RuntimeErrorDetail):self.finish()
        self.assertEqual(self.state(),before)
        self.start()
        before=self.state()
        with self.assertRaises(RuntimeErrorDetail):
            self.finish(method='A different post-hoc method',criteria={'positive':'Anything works'})
        self.assertEqual(self.state(),before)
        self.finish()
        final=self.item('validations','check-a')
        self.assertEqual(final['plan'],initial['plan'])
        self.assertEqual(final['plan_hash'],initial['plan_hash'])

    def test_start_replay_and_failed_multi_operation_do_not_double_charge(self):
        self.validation()
        request=self.start_request()
        self.runtime.transact(request)
        before=self.state()
        replay=self.runtime.transact(request)
        self.assertTrue(replay['replayed'])
        self.assertEqual(self.state(),before)
        self.validation('check-b')
        before=self.state()
        request=self.request({'op':'validation.start','id':'check-b',
            'expected_validation_revision':self.item('validations','check-b')['revision']},
            {'op':'not.a.valid.operation'})
        self.assertRejectedUnchanged(request)
        self.assertEqual(self.state(),before)

    def test_validation_start_rejects_stale_entity_revision(self):
        self.validation()
        stale=self.start_request()
        self.start()
        stale['idempotency_key']='stale-start'
        self.assertRejectedUnchanged(stale,'stale_revision')

    def test_budget_exhaustion_rejects_new_work_but_allows_result(self):
        self.exploration_budget(max_validations=1,max_cost=1)
        self.validation()
        self.validation('check-b')
        self.start()
        self.assertRejectedUnchanged(self.start_request('check-b'),'resource_limit')
        self.finish(outcome='negative')
        self.assertEqual(self.exploration()['usage']['validations'],1)
        self.assertEqual(self.exploration()['usage']['cost'],1)
        self.assertEqual(self.item('validations','check-a')['status'],'completed')

    def test_negative_inconclusive_and_cancelled_work_never_refund(self):
        self.result('negative',outcome='negative',cost=2)
        self.result('uncertain',outcome='inconclusive',cost=3)
        self.validation('cancelled',cost=1)
        self.start('cancelled')
        self.tx({'op':'validation.cancel','id':'cancelled',
            'expected_validation_revision':self.item('validations','cancelled')['revision'],
            'reason':'No further useful information','evidence':['evidence/observation.md']})
        self.assertEqual(self.exploration()['usage']['validations'],3)
        self.assertEqual(self.exploration()['usage']['cost'],6)
        self.pause();self.resume()
        self.assertEqual(self.exploration()['usage']['cost'],6)

    def test_parallel_validation_starts_share_one_atomic_budget(self):
        self.exploration_budget(max_validations=1,max_cost=1)
        self.validation('race-a');self.validation('race-b')
        outputs=self.run_parallel([self.start_request('race-a'),self.start_request('race-b')])
        self.assertEqual(sum(code==0 for code,_,_ in outputs),1,outputs)
        errors=[json.loads(stdout)['code'] for code,stdout,_ in outputs if code]
        self.assertEqual(errors,['resource_limit'],outputs)
        self.assertEqual(self.exploration()['usage']['validations'],1)
        self.assertEqual(self.exploration()['usage']['cost'],1)
        self.assertEqual(sum(v['status']=='running' for v in self.exploration()['validations']),1)

    def test_operator_budget_adjustment_cannot_reset_or_exceed_ceiling(self):
        self.result(cost=2)
        self.assertRejectedUnchanged(self.request({'op':'exploration.budget.set',
            'expected_exploration_revision':self.exploration()['revision'],'budgets':{'max_cost':3}}),'unauthorized')
        for value in [1,self.exploration()['budget_ceiling']['max_cost']+1]:
            self.assertRejectedUnchanged(self.request({'op':'exploration.budget.set',
                'expected_exploration_revision':self.exploration()['revision'],'budgets':{'max_cost':value}},actor='operator'))
        self.exploration_budget(max_cost=3)
        self.assertEqual(self.exploration()['usage']['cost'],2)

    def test_pause_blocks_exploration_finish_and_resume_retains_result_plan(self):
        self.validation();self.start()
        planned=self.item('validations','check-a')['plan_hash']
        self.pause()
        before=self.state()
        with self.assertRaises(RuntimeErrorDetail) as caught:self.finish()
        self.assertEqual(caught.exception.code,'paused')
        self.assertEqual(self.state(),before)
        self.resume();self.finish()
        self.assertEqual(self.item('validations','check-a')['plan_hash'],planned)
        self.assertEqual(self.exploration()['usage']['validations'],1)

    def test_selection_is_tentative_and_cannot_deliver_without_positive_evidence(self):
        self.candidate()
        self.decision('select-a','select','option-a')
        self.assertEqual(self.exploration()['active_candidate_id'],'option-a')
        self.assertEqual(self.item('candidates','option-a')['evidence_status'],'unvalidated')
        before=self.state()
        with self.assertRaises(RuntimeErrorDetail):self.decision('unsupported-delivery','deliver','option-a')
        self.assertEqual(self.state(),before)

    def test_pivot_is_recorded_and_preserves_prior_decision_and_goal(self):
        self.candidate();self.candidate('option-b')
        self.decision('select-a','select','option-a',alternatives=['option-b'])
        self.result('negative-a','option-a','negative')
        original=copy.deepcopy(self.exploration()['decisions'][0])
        self.decision('pivot-b','pivot','option-b',['negative-a'],alternatives=['option-a'])
        self.assertEqual(self.exploration()['active_candidate_id'],'option-b')
        self.assertEqual(self.exploration()['decisions'][0],original)
        self.assertEqual(len(self.exploration()['decisions']),2)
        self.assertEqual(self.state()['charter']['objective'],self.charter['objective'])

    def test_pivot_requires_explicit_disposition_and_revokes_cancelled_task(self):
        self.candidate();self.candidate('option-b')
        self.decision('select-a','select','option-a')
        self.task();claim=self.claim()
        self.result('negative-a','option-a','negative')
        before=self.state()
        with self.assertRaises(RuntimeErrorDetail):self.decision('missing-disposition','pivot','option-b',['negative-a'])
        self.assertEqual(self.state(),before)
        task=next(t for t in self.state()['tasks'] if t['id']=='draft')
        self.decision('pivot-cancel','pivot','option-b',['negative-a'],task_dispositions=[{
            'id':'draft','expected_task_revision':task['revision'],'action':'cancel',
            'reason':'Old prototype no longer tests the selected direction.'}])
        task=next(t for t in self.state()['tasks'] if t['id']=='draft')
        self.assertEqual(task['status'],'cancelled')
        self.assertIsNone(task['lease'])
        self.assertRejectedUnchanged(self.request({'op':'task.checkpoint','id':'draft',
            'lease_id':claim['lease_id'],'fencing_token':claim['fencing_token'],'data':{'late':True}}),'stale_lease')

    def test_decision_refuses_changed_result_evidence(self):
        self.result()
        self.decision('select-a','select','option-a',['check-a'])
        self.write('evidence/observation.md','Changed after the validation result was committed.')
        before=self.state()
        with self.assertRaises(RuntimeErrorDetail) as caught:self.decision('tampered-deliver','deliver','option-a',['check-a'])
        self.assertEqual(caught.exception.code,'stale_input')
        self.assertEqual(self.state(),before)

    def test_finish_requires_real_local_evidence_and_cannot_rewrite_result(self):
        self.validation();self.start()
        request=self.request({'op':'validation.finish','id':'check-a',
            'expected_validation_revision':self.item('validations','check-a')['revision'],
            'outcome':'positive','summary':'Unsupported','limitations':[],
            'criteria_assessment':{'positive':'yes','negative':'no','inconclusive':'no'},
            'evidence':['evidence/missing.md']})
        self.assertRejectedUnchanged(request,'missing_file')
        self.finish(outcome='negative')
        before=self.state()
        with self.assertRaises(RuntimeErrorDetail):self.finish(outcome='positive')
        self.assertEqual(self.state(),before)

    def test_stop_can_close_negative_exploration_and_records_open_questions(self):
        self.result(outcome='negative')
        self.decision('select-a','select','option-a',['check-a'])
        self.decision('stop','stop',result_ids=['check-a'])
        self.assertEqual(self.exploration()['status'],'closed')
        self.assertEqual(self.exploration()['decisions'][-1]['residual_question_ids'],['need'])
        before=self.state()
        with self.assertRaises(RuntimeErrorDetail):self.candidate('post-stop')
        self.assertEqual(self.state(),before)

    def test_close_requires_disposition_of_all_open_questions_and_planned_checks(self):
        self.result();self.decision('select-a','select','option-a',['check-a'])
        before=self.state()
        with self.assertRaises(RuntimeErrorDetail):self.decision('missing-questions','deliver','option-a',['check-a'],residual_question_ids=[])
        self.assertEqual(self.state(),before)
        self.validation('unperformed')
        before=self.state()
        with self.assertRaises(RuntimeErrorDetail):self.decision('unfinished-check','deliver','option-a',['check-a'])
        self.assertEqual(self.state(),before)

    def test_project_complete_requires_exploration_closure_and_acceptance(self):
        self.result();self.decision('select-a','select','option-a',['check-a'])
        self.done_task()
        self.assertRejectedUnchanged(self.complete_request())
        self.decision('deliver','deliver','option-a',['check-a'])
        self.runtime.transact(self.complete_request())
        self.assertEqual(self.state()['project_status'],'completed')
        self.assertEqual(self.exploration()['status'],'closed')
        self.runtime=Runtime(self.project)
        self.assertEqual(self.state()['project_status'],'completed')

    def test_worker_cannot_change_exploration_or_choose_direction(self):
        self.tx({'op':'unit.create','id':'work','name':'Work','mission':'Local task work',
            'creation_reason':'Independent task','exit_condition':'Task accepted'})
        self.tx({'op':'chat.bind','id':'worker','unit_id':'work','host_id':'fixture','thread_id':'worker-fixture'})
        self.assertRejectedUnchanged(self.request({'op':'question.add','id':'worker-question',
            'question':'Change project direction?','importance':'Worker assertion'},actor='worker'),'unauthorized')

    def test_decision_uses_exploration_revision_without_replacing_history(self):
        self.candidate()
        stale=self.exploration()['revision']
        self.question('second-question')
        self.assertRejectedUnchanged(self.request({'op':'decision.record','id':'stale-decision',
            'expected_exploration_revision':stale,'action':'select','candidate_id':'option-a',
            'alternatives':[],'result_ids':[],'rationale':'Old view',
            'evidence':['evidence/observation.md'],'task_dispositions':[]}),'stale_revision')

    def test_background_mode_requires_real_binding_and_wake_credentials(self):
        project=Path(self.temp.name)/'background'
        project.mkdir()
        charter=copy.deepcopy(self.charter)
        charter['execution']['mode']='background'
        charter['scope']={'project_root':str(project),'write_roots':[str(project)]}
        self.project=project
        self.write('inputs/authorization.md','Fixture background authority.')
        self.write('evidence/binding.md','Fixture host receipt.')
        self.runtime=Runtime(project)
        self.runtime.init(charter)
        self.assertRejectedUnchanged(self.request({'op':'question.add','id':'without-wake',
            'question':'Is there a gap?','importance':'High'}),'stale_wake')
        self.tx({'op':'scheduler.bind','expected_scheduler_revision':self.state()['scheduler']['revision'],
            'automation_id':'fixture','thread_id':'fixture-coordinator','host_id':'fixture',
            'evidence':['evidence/binding.md']},actor='operator')
        self.tx({'op':'scheduler.enable','expected_scheduler_revision':self.state()['scheduler']['revision'],
            'enabled':True},actor='operator')
        wake=self.tx({'op':'wake.begin','id':'one-wake'})['results'][0]
        self.tx({'op':'question.add','id':'with-wake','question':'Is there a gap?','importance':'High'},
            wake_id=wake['wake_id'],wake_fence=wake['wake_fence'])
        self.assertEqual(self.exploration()['questions'][0]['id'],'with-wake')
        self.assertEqual(self.state()['scheduler']['usage']['wakes'],1)

    def test_foreground_default_cannot_accidentally_enable_background(self):
        project=Path(self.temp.name)/'default-mode'
        project.mkdir()
        self.project=project
        self.write('inputs/authorization.md','Fixture local test authority.')
        self.write('evidence/binding.md','Fixture binding.')
        charter=copy.deepcopy(self.charter)
        del charter['execution']['mode']
        charter['scope']={'project_root':str(project),'write_roots':[str(project)]}
        self.runtime=Runtime(project)
        self.runtime.init(charter)
        self.assertEqual(self.state()['execution_mode'],'foreground')
        self.assertRejectedUnchanged(self.request({'op':'wake.begin','id':'unexpected'}),'unauthorized')
        self.assertRejectedUnchanged(self.request({'op':'scheduler.bind',
            'expected_scheduler_revision':self.state()['scheduler']['revision'],
            'automation_id':'fixture','thread_id':'fixture','host_id':'fixture',
            'evidence':['evidence/binding.md']},actor='operator'),'unauthorized')

    def test_changed_preregistered_input_blocks_start_and_finish(self):
        self.validation(inputs=['inputs/brief.md'])
        original=(self.project/'inputs/brief.md').read_text(encoding='utf-8')
        self.write('inputs/brief.md','An altered fixture changes the original validation basis.')
        self.assertRejectedUnchanged(self.start_request(),'stale_input')
        self.write('inputs/brief.md',original)
        self.start()
        self.write('inputs/brief.md','An altered fixture after the validation began.')
        before=self.state()
        with self.assertRaises(RuntimeErrorDetail) as caught:self.finish()
        self.assertEqual(caught.exception.code,'stale_input')
        self.assertEqual(self.state(),before)

    def test_retirement_requires_pending_validation_disposition_and_preserves_budget(self):
        self.validation();self.start()
        before=self.state()
        with self.assertRaises(RuntimeErrorDetail):self.decision('retire-pending','retire','option-a')
        self.assertEqual(self.state(),before)
        self.tx({'op':'validation.cancel','id':'check-a',
            'expected_validation_revision':self.item('validations','check-a')['revision'],
            'reason':'Candidate is no longer promising','evidence':['evidence/observation.md']})
        self.decision('retire','retire','option-a')
        self.assertEqual(self.item('candidates','option-a')['status'],'retired')
        self.assertEqual(self.exploration()['usage']['validations'],1)
        self.assertEqual(self.exploration()['usage']['candidates'],1)

    def test_mixed_evidence_does_not_hide_negative_findings(self):
        self.result('positive',outcome='positive')
        self.result('negative',outcome='negative')
        self.assertEqual(self.item('candidates','option-a')['evidence_status'],'mixed')
        self.assertEqual(self.item('validations','negative')['result']['outcome'],'negative')
        self.assertEqual(self.item('validations','positive')['result']['outcome'],'positive')

    def test_budget_stop_can_still_finish_reviewed_negative_report(self):
        self.exploration_budget(max_validations=1,max_cost=1)
        self.result(outcome='negative')
        self.decision('select-a','select','option-a',['check-a'])
        self.done_task()
        self.decision('stop','stop',result_ids=['check-a'])
        self.runtime.transact(self.complete_request())
        self.assertEqual(self.state()['project_status'],'completed')
        self.assertEqual(self.exploration()['usage']['validations'],1)

    def test_invalid_exploration_init_leaves_no_partial_project_and_can_retry(self):
        project=Path(self.temp.name)/'invalid-init'
        project.mkdir()
        self.project=project
        self.write('inputs/authorization.md','Fixture local test authority.')
        charter=copy.deepcopy(self.charter)
        charter['scope']={'project_root':str(project),'write_roots':[str(project)]}
        charter['exploration']={'budgets':{'max_cost':2},'budget_ceiling':{'max_cost':1}}
        runtime=Runtime(project)
        with self.assertRaises(RuntimeErrorDetail):runtime.init(charter)
        self.assertFalse((project/'.adaptive-v4/state.sqlite3').exists())
        charter['exploration']['budget_ceiling']['max_cost']=2
        runtime.init(charter)
        self.assertEqual(runtime.read()['exploration']['usage']['cost'],0)
        self.assertEqual(runtime.read()['revision'],0)

    def test_preregistration_and_start_cannot_be_collapsed_into_one_transaction(self):
        self.candidate()
        request=self.request({'op':'validation.plan','id':'instant','candidate_id':'option-a',
            'hypothesis':'Looks successful after inspecting results','method':'Post-hoc inspection',
            'criteria':{'positive':'Works','negative':'Fails','inconclusive':'Unknown'},'cost':1},
            {'op':'validation.start','id':'instant','expected_validation_revision':1})
        self.assertRejectedUnchanged(request)

    def test_start_and_result_require_separate_durable_transactions(self):
        self.validation()
        request=self.request({'op':'validation.start','id':'check-a',
            'expected_validation_revision':self.item('validations','check-a')['revision']},
            {'op':'validation.finish','id':'check-a','expected_validation_revision':2,
             'outcome':'positive','summary':'Instant result','limitations':[],
             'criteria_assessment':{'positive':'yes','negative':'no','inconclusive':'no'},
             'evidence':['evidence/observation.md']})
        self.assertRejectedUnchanged(request)

    def test_finished_validation_does_not_replay_start_as_execution_permission(self):
        self.validation()
        request=self.start_request()
        self.runtime.transact(request)
        self.finish()
        self.assertRejectedUnchanged(request,'invalid_transition')

    def test_pivot_disposes_transitive_dependencies_not_only_direct_candidate_tasks(self):
        self.candidate();self.candidate('option-b')
        self.decision('select-a','select','option-a')
        self.task()
        self.tx({'op':'task.create','id':'dependent','title':'Deliver old prototype',
            'owner_unit_id':'coordinator','acceptance':['Depends on the prototype'],
            'dependencies':['draft']})
        self.result('negative-a','option-a','negative')
        draft=next(t for t in self.state()['tasks'] if t['id']=='draft')
        dispositions=[{'id':'draft','expected_task_revision':draft['revision'],
            'action':'cancel','reason':'Old direction abandoned'}]
        before=self.state()
        with self.assertRaises(RuntimeErrorDetail):
            self.decision('incomplete-impact','pivot','option-b',['negative-a'],task_dispositions=dispositions)
        self.assertEqual(self.state(),before)
        dependent=next(t for t in self.state()['tasks'] if t['id']=='dependent')
        dispositions.append({'id':'dependent','expected_task_revision':dependent['revision'],
            'action':'cancel','reason':'No longer has a useful upstream result'})
        self.decision('complete-impact','pivot','option-b',['negative-a'],task_dispositions=dispositions)
        self.assertTrue(all(t['status']=='cancelled' for t in self.state()['tasks']))


if __name__ == '__main__':
    unittest.main()
