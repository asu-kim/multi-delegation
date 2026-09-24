"""Height-dependent delegation and end-to-end runner orchestration tests."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import generate_warehouse_realdata as generator
import run_warehouse_experiment_realdata as runner
from warehouse_delegation import delegation_chain, request_chain


class CascadingDelegationTests(unittest.TestCase):
    def test_graph_ownership_and_privilege_chains(self):
        generator.configure_auths(2)
        try:
            items = [dict(zone=z, z=height, x=0, y=0, reference=f'p{height}{z}',
                          storage_location_id=f'l{height}{z}')
                     for z in generator.ZONES for height in (0, 1, 2)]
            graph = generator.build_graph(items, generator.generate_supervisors(),
                                          generator.generate_robots(2), '1*day')
            counts = generator.entity_distribution(graph)['entity_counts_by_auth']
            for stats in counts.values():
                self.assertEqual([stats[k] for k in ('robots', 'forklifts', 'drones')], [2, 2, 2])
            for e in graph['entityList']:
                self.assertEqual(graph['assignments'][e['name']], 100 + int(e['name'].split('.')[0][3:]))
            privileges = {(p['privilegedGroup'], p['subjectGroup'], p['objectGroup'])
                          for p in graph['privilegeList'] if p['privilegeType'] == 'DelegationGrant'}
            for item in items:
                resource = generator.resource_group(item)
                chain = ['Supervisors'] + delegation_chain('RobotA1', item['z'])
                for a, b in zip(chain, chain[1:]):
                    self.assertIn((a, b, resource), privileges)
                self.assertEqual(('ForkliftA1', 'DroneA1', resource) in privileges, item['z'] == 2)
            self.assertTrue(all(p['RequestingGroup'] == 'Supervisors'
                                for p in generator.build_supervisor_access_policies(graph)))
        finally:
            generator.configure_auths(4)

    def test_invalid_height_and_mismatched_chain(self):
        for height in (-1, 0.5, None):
            with self.assertRaises(ValueError):
                delegation_chain('RobotA1', height)
        self.assertEqual(delegation_chain("RobotA1", 3), ["RobotA1", "ForkliftA1", "DroneA1"])
        with self.assertRaises(ValueError):
            request_chain(dict(selected_robot='RobotA1', resource_position={'z': 2},
                               delegation_chain=['RobotA1']))

    def run_request(self, height, final_status='denied', first_status='success'):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            chain = delegation_chain('RobotA1', height)
            graph = dict(authList=[], assignments={'net1.supervisor': 101}, entityList=[
                {'group': 'Supervisors', 'name': 'net1.supervisor'},
                {'group': 'Item_1', 'name': 'net2.item1'},
                *[{'group': g, 'name': f'net1.{g.lower()}'} for g in chain]])
            req = dict(request_id=0, supervisor='net1.supervisor', selected_robot='RobotA1',
                       resource='Item_1', resource_zone='B', selected_robot_distance_m=1,
                       item_id='1', storage_location_id='loc', resource_position={'z': height})
            (root/'graph.json').write_text(json.dumps(graph))
            (root/'workload.json').write_text(json.dumps({'requests': [req]}))
            args = SimpleNamespace(graph=str(root/'graph.json'), workload=str(root/'workload.json'),
                                   results=str(root/'out.json'), project_root=str(root), startup_timeout=1,
                                   command_timeout=1, access_timeout=1, validity='1*day',
                                   inter_request_delay=0, keep_processes=False)
            procs = {g: Mock(name=g) for g in ['net1.supervisor'] + chain}
            accesses = [dict(status=first_status if i == 0 else 'success', latency_ms=1) for i in range(len(chain))]
            accesses += [dict(status=final_status if i == len(chain)-1 else 'denied', latency_ms=1) for i in range(len(chain))]
            with contextlib.ExitStack() as stack:
                stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                for name in ('run_generate_all', 'terminate_process'):
                    stack.enter_context(patch.object(runner, name))
                stack.enter_context(patch.object(runner, 'parse_args', return_value=args))
                stack.enter_context(patch.object(runner, 'start_auths', return_value=({}, {})))
                stack.enter_context(patch.object(runner, 'start_user_entity', side_effect=lambda g, *a: (procs[g], Mock())))
                stack.enter_context(patch.object(runner, 'start_resource_entity', return_value=(Mock(), Mock())))
                grants = stack.enter_context(patch.object(runner, 'delegate', return_value=dict(status='success', latency_ms=1)))
                revokes = stack.enter_context(patch.object(runner, 'revoke', return_value=dict(status='success', latency_ms=1)))
                access = stack.enter_context(patch.object(runner, 'access_attempt', side_effect=accesses))
                stack.enter_context(patch.object(runner.time, 'sleep'))
                runner.main()
            self.assertEqual(grants.call_count, len(chain))
            for call, parent, child in zip(grants.call_args_list, ['net1.supervisor'] + chain, chain):
                self.assertIs(call.args[0], procs[parent])
                self.assertEqual(call.args[2], child)
            revokes.assert_called_once()
            self.assertEqual(revokes.call_args.kwargs['robot'], 'RobotA1')
            self.assertEqual(access.call_count, 2 * len(chain))
            return json.loads((root/'out_results.json').read_text())['results'][0]

    def test_each_height_and_no_direct_descendant_revokes(self):
        for z in (0, 1, 2):
            with self.subTest(z=z):
                result = self.run_request(z)
                self.assertTrue(result['revocation_verified'])
                self.assertEqual(result['cascading_revocation_verified'], True if z else None)
                self.assertEqual(len(result['after_revoke_access_by_entity']), z + 1)

    def test_timeout_or_initial_denial_cannot_pass_verification(self):
        self.assertFalse(self.run_request(2, final_status='timeout')['cascading_revocation_verified'])
        self.assertFalse(self.run_request(1, first_status='denied')['cascading_revocation_verified'])
        self.assertFalse(self.run_request(2, final_status='success')['cascading_revocation_verified'])


if __name__ == '__main__':
    unittest.main()
