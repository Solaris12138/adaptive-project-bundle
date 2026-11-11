"""Acceptance phase integration examples. Native host evidence is synthetic."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('acceptance_phases_runtime', ROOT/'adaptive-project/scripts/runtime.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def whole_acceptance(manifest, observations):
    """Example outer audit; not a runtime permission or native authentication."""
    expected = manifest['business'] + manifest['post_completion']
    mapped = [c for row in manifest['source_mapping'].values() for c in row]
    if len(expected) != len(set(expected)) or set(mapped) != set(expected):
        return False
    if set(manifest['source_mapping']) != set(manifest['original_requirements']):
        return False
    if set(observations) != set(expected):
        return False
    return all(observations[c].get('passed') is True and observations[c].get('evidence') for c in expected)


class AcceptancePhasesV4Tests(unittest.TestCase):
    def setUp(self):
        parent = ROOT/'work/acceptance-phase-tests'
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='case-', dir=str(parent))
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.seq = 0
        self.wake = {}
        self.manifest = {'original_requirements': ['deliver', 'finish-and-stop'],
            'business': ['report-reviewed-and-exported'],
            'post_completion': ['runtime-completed', 'host-paused-after-prepare', 'shutdown-confirmed', 'required-host-cleanup'],
            'source_mapping': {'deliver': ['report-reviewed-and-exported'],
                'finish-and-stop': ['runtime-completed', 'host-paused-after-prepare', 'shutdown-confirmed', 'required-host-cleanup']}}
        self.write('authority.txt', 'Synthetic fixture only; no native action authorization.')
        self.write('protocol-acceptance.json', json.dumps(self.manifest))
        self.write('review.txt', 'Reviewed exact report bytes and exported artifact before closure.')

    def write(self, ref, value):
        p = self.project/ref
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(value, encoding='utf-8')
        return ref

    def tx(self, operation, actor='coordinator', administrative=False):
        self.seq += 1
        request = {'actor_id': actor, 'idempotency_key': 'phase-'+str(self.seq),
                   'reason': 'Isolated acceptance phase integration', 'operations': [operation]}
        if actor == 'coordinator' and not administrative:
            request.update(self.wake)
        return self.runtime.transact(request)

    def prepare_business(self, cyclic=False):
        acceptance = self.manifest['business'] + (self.manifest['post_completion'] if cyclic else [])
        charter = {'project_id':'phase-fixture','objective':'Deliver then close without a dependency cycle',
            'acceptance': acceptance, 'scope':{'project_root':'.','write_roots':['.']},
            'authority':{'status':'bound','source_refs':['authority.txt','protocol-acceptance.json'],
                         'configure_future_triggers':True,'maintain_project_memory':True,
                         'adjust_units_within_scope':True,'create_long_lived_chats':True,'message_long_lived_chats':True},
            'execution':{'state_writer_id':'coordinator','operator_id':'operator','mode':'background'},
            'scheduler':{'budgets':{'max_wakes':2,'max_claims':2,'max_dispatches':2,'max_actions_per_wake':30,'max_wake_seconds':60},
                         'expires_at':time.time()+300}}
        self.runtime = module.Runtime(self.project)
        self.runtime.init(charter)
        self.tx({'op':'scheduler.bind','expected_scheduler_revision':1,'automation_id':'synthetic-host','host_id':'local',
                 'thread_id':'synthetic-thread','evidence':['authority.txt']},actor='operator')
        self.tx({'op':'scheduler.enable','expected_scheduler_revision':self.runtime.read()['scheduler']['revision'],'enabled':True},actor='operator')
        wake = self.tx({'op':'wake.begin','id':'phase-wake','lease_seconds':60},administrative=True)['results'][0]
        self.wake = {k:wake[k] for k in ['wake_id','wake_fence']}
        self.tx({'op':'task.create','id':'report','title':'Produce report','owner_unit_id':'coordinator',
                 'acceptance':['Report has actual evidence']})
        claim = self.tx({'op':'task.claim','id':'report','expected_task_revision':1,'lease_seconds':60})['results'][0]
        ref = self.write(str(Path(claim['attempt_dir'])/'report.txt'), 'Actual isolated report bytes. Native events remain pending.')
        self.tx({'op':'task.submit','id':'report','expected_task_revision':claim['task_revision'],
                 'lease_id':claim['lease_id'],'fencing_token':claim['fencing_token'],'artifacts':[ref],'summary':'Reviewable report'})
        task = self.runtime.read()['tasks'][0]
        self.tx({'op':'task.review','id':'report','expected_task_revision':task['revision'],'verdict':'pass',
                 'method':'Read report and compare required evidence','evidence':['review.txt']})
        artifact = self.runtime.read()['tasks'][0]['accepted_artifacts'][0]
        self.tx({'op':'artifact.export','blob_id':artifact['blob_id'],'output':'outputs/report.txt'})
        self.tx({'op':'decision.record','id':'stop','expected_exploration_revision':self.runtime.read()['exploration']['revision'],
                 'action':'stop','alternatives':[],'result_ids':[],'rationale':'No further business needed',
                 'evidence':['outputs/report.txt'],'task_dispositions':[],'residual_question_ids':[]})
        return acceptance

    def complete(self, acceptance):
        return self.tx({'op':'project.complete','evidence':['outputs/report.txt'],
                       'acceptance_results':[{'criterion':c,'passed':c in self.manifest['business'],
                                              'evidence':['outputs/report.txt']} for c in acceptance]})

    def test_old_cyclic_charter_truthful_completion_rejected_without_mutation(self):
        acceptance = self.prepare_business(cyclic=True)
        before = self.runtime.read()
        database = self.project/'.adaptive-v4/state.sqlite3'
        digest = hashlib.sha256(database.read_bytes()).hexdigest()
        with self.assertRaises(module.RuntimeErrorDetail) as caught:
            self.complete(acceptance)
        self.assertEqual(caught.exception.code, 'invalid_request')
        self.assertEqual(self.runtime.read(), before)
        self.assertEqual(hashlib.sha256(database.read_bytes()).hexdigest(), digest)
        self.assertEqual(before['charter']['acceptance'], acceptance)

    def test_business_completion_then_shutdown_preserves_external_pending_acceptance(self):
        acceptance = self.prepare_business()
        self.complete(acceptance)
        state = self.runtime.read()
        self.assertEqual(state['project_status'], 'completed')
        self.assertIsNone(state['scheduler']['active_wake'])
        observations = {c:{'passed':False,'evidence':[]} for c in self.manifest['business']+self.manifest['post_completion']}
        observations['report-reviewed-and-exported'] = {'passed':True,'evidence':['outputs/report.txt']}
        observations['runtime-completed'] = {'passed':True,'evidence':['runtime state']}
        self.assertFalse(whole_acceptance(self.manifest, observations))
        intent = self.tx({'op':'shutdown.prepare','id':'stop','evidence':['outputs/report.txt']},administrative=True)['results'][0]
        host = {'automation_id':'synthetic-host','thread_id':'synthetic-thread','host_id':'local','status':'PAUSED',
                'observed_at_unix':time.time()}
        self.write('synthetic-host-paused.json',json.dumps({'synthetic':True,'host':host}))
        self.assertGreaterEqual(host['observed_at_unix'],intent['prepared_at_unix'])
        self.tx({'op':'shutdown.confirm','id':'stop','host_observation':host,
                 'evidence':['synthetic-host-paused.json']},administrative=True)
        observations['host-paused-after-prepare'] = {'passed':True,'evidence':['synthetic-host-paused.json']}
        observations['shutdown-confirmed'] = {'passed':True,'evidence':['runtime state']}
        self.assertFalse(whole_acceptance(self.manifest, observations),'Required cleanup must not disappear from whole acceptance')
        observations['required-host-cleanup'] = {'passed':True,'evidence':['explicitly synthetic cleanup receipt']}
        self.assertTrue(whole_acceptance(self.manifest, observations))
        self.assertEqual(self.runtime.read()['scheduler']['usage'],state['scheduler']['usage'])
        self.assertEqual(self.runtime.read()['charter']['acceptance'],self.manifest['business'])

    def test_external_mapping_missing_unknown_and_evidence_free_are_not_passes(self):
        observations = {c:{'passed':True,'evidence':['synthetic fixture only']} for c in self.manifest['business']+self.manifest['post_completion']}
        self.assertTrue(whole_acceptance(self.manifest, observations))
        for field in [None,False,'unknown',1]:
            variant = copy.deepcopy(observations)
            variant['required-host-cleanup']['passed'] = field
            self.assertFalse(whole_acceptance(self.manifest,variant))
        variant = copy.deepcopy(observations);variant['required-host-cleanup']['evidence']=[]
        self.assertFalse(whole_acceptance(self.manifest,variant))
        variant = copy.deepcopy(self.manifest);variant['source_mapping'].pop('finish-and-stop')
        self.assertFalse(whole_acceptance(variant,observations))
        variant = copy.deepcopy(observations);variant.pop('required-host-cleanup')
        self.assertFalse(whole_acceptance(self.manifest,variant))


if __name__ == '__main__': unittest.main()
