"""Run with: python -m unittest test_generate_warehouse_realdata."""
import copy
import json
import os
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import generate_warehouse_realdata as g

DATA = Path(os.environ.get('WAREHOUSE_DATASET', str(Path(__file__).parent / 'dataset')))


class AuthBalanceTests(unittest.TestCase):
    def tearDown(self):
        g.configure_auths(4)

    def items(self, n):
        # Identical positions exercise ID tie breaking and shared storage locations.
        return [dict(x=10, y=20, z=1, reference=f'product{i:05d}',
                     item_id=f'product{i:05d}', storage_location_id='shared') for i in range(n)]

    def check_outputs(self, items, robots_per_auth=2):
        tree = g.balance_resource_zones(items)
        expected = len(items) // len(g.ZONES)
        self.assertEqual({z: sum(i['zone'] == z for i in items) for z in g.ZONES},
                         {z: expected for z in g.ZONES})
        restored_tree = json.loads(json.dumps(tree))
        for item in items:
            self.assertEqual(g.partition_zone(item, restored_tree), item['zone'])
        shuffled = copy.deepcopy(items)
        random.Random(42).shuffle(shuffled)
        self.assertEqual(g.balance_resource_zones(shuffled), tree)
        robots = g.generate_robots(robots_per_auth)
        graph = g.build_graph(items, g.generate_supervisors(), robots, '1*day')
        stats = g.entity_distribution(graph)
        self.assertEqual(stats['entities_per_auth'], expected + 3 * robots_per_auth + 1)
        self.assertEqual(stats['total_entities'], len(g.ZONES) * stats['entities_per_auth'])
        self.assertEqual(len(graph['authList']), len(g.ZONES))
        self.assertEqual(len(graph['authTrusts']), len(g.ZONES) * (len(g.ZONES) - 1) // 2)
        ports = [a[k] for a in graph['authList'] for k in ('tcpPort', 'udpPort', 'authPort', 'callbackPort')]
        ports += [e['port'] for e in graph['entityList'] if 'port' in e]
        self.assertEqual(len(ports), len(set(ports)))
        self.assertTrue(all(0 < p <= 65535 for p in ports))
        for entity in graph['entityList']:
            self.assertNotIn(graph['assignments'][entity['name']], entity['backupToAuthIds'])
        layout = g.build_layout(items, dict(xmin=0, xmax=100, ymin=0, ymax=100), [dict(x=10, y=20, z=0)])
        self.assertEqual(layout['num_auths'], len(g.ZONES))
        self.assertIn(layout['navigation_points'][0]['zone'], g.ZONES)
        for item in items:
            self.assertEqual(graph['assignments'][g.resource_entity_name(item)], g.ZONE_AUTH_IDS[item['zone']])
            self.assertEqual(layout['item_locations'][g.resource_group(item)]['zone'], item['zone'])
        return layout, robots

    def test_variable_auth_counts_and_ties(self):
        for auths in (1, 2, 3, 4, 5, 7, 27):
            with self.subTest(auths=auths):
                g.configure_auths(auths)
                layout, _ = self.check_outputs(self.items(auths * 3))
                loc = layout['storage_locations']['shared']
                self.assertEqual(set(loc['zones']), set(g.ZONES))
                self.assertEqual(loc['zone'], 'A' if auths == 1 else None)
                self.assertEqual(g.ZONES[-1], 'AA' if auths == 27 else chr(64 + auths))

    def test_exact_divisibility(self):
        for auths in (1, 2, 4, 5):
            g.configure_auths(auths)
            for n in (0, auths - 1, auths + 1):
                if n > 0 and n % auths == 0:
                    continue
                with self.subTest(auths=auths, n=n), self.assertRaisesRegex(ValueError, 'shared equally'):
                    g.balance_resource_zones(self.items(n))

    def test_config_and_port_limits(self):
        for n in (0, -1, g.MAX_AUTHS + 1):
            with self.assertRaisesRegex(ValueError, 'num-auths'):
                g.configure_auths(n)
        g.configure_auths(g.MAX_AUTHS)
        self.assertLess(max(g.AUTH_TCP_PORT_BASE.values()) + 3, g.RESOURCE_PORT_START)
        g.configure_auths(1)
        self.assertEqual(max(g.assign_resource_ports(self.items(65536 - g.RESOURCE_PORT_START)).values()), 65535)
        with self.assertRaisesRegex(ValueError, 'TCP ports'):
            g.assign_resource_ports(self.items(65537 - g.RESOURCE_PORT_START))

    def test_fixed_entities_per_auth_across_runs(self):
        for auths in (2, 4, 5):
            args = g.parse_args(['--num-auths', str(auths), '--resources-per-auth', '20', '--robots-per-auth', '5'])
            g.configure_auths(auths)
            n = g.requested_resource_count(args)
            self.assertEqual(n, 20 * auths)
            self.check_outputs(self.items(n), 5)
        defaults = g.parse_args([])
        self.assertEqual((defaults.resources_per_auth, defaults.num_auths, defaults.robots_per_auth), (25, 4, 5))
        self.assertEqual(g.requested_resource_count(defaults), 100)
        self.assertEqual(g.parse_args([]).num_auths, 4)
        for argv in (['--resources-per-auth', '0'], ['--resources-per-auth', '-1'], ['--robots-per-auth', '0'], ['--robots-per-auth', '-1']):
            with self.assertRaises(ValueError):
                g.requested_resource_count(g.parse_args(argv))

    def test_real_data_and_reproducibility(self):
        for auths, n in ((4, 100), (5, 100), (3, 99), (1, 9091)):
            with self.subTest(auths=auths):
                g.configure_auths(auths)
                items, picking, bounds, _ = g.load_real_data(DATA/'Storage_Location.csv', DATA/'Picking_Wave.csv', n, 7)
                layout, robots = self.check_outputs(items, 1)
                workload = g.generate_workload(items, picking, robots, bounds, [], 2, min(3, len(robots)), 'random', 7)
                self.assertEqual(workload['metadata']['num_auths'], auths)
                for req in workload['requests']:
                    self.assertEqual(req['resource_zone'], layout['item_locations'][req['resource']]['zone'])
                    self.assertEqual(req['cross_auth'], req['selected_robot_home_zone'] != req['resource_zone'])
                if n == 100:
                    again = g.load_real_data(DATA/'Storage_Location.csv', DATA/'Picking_Wave.csv', n, 7)[0]
                    self.assertEqual(again, items)

    def test_reject_unavailable_or_unequal_data(self):
        for n, message in ((-1, 'resource count'), (10000, 'only'), (101, 'shared equally'), (0, 'resource count')):
            with self.subTest(n=n), self.assertRaisesRegex(ValueError, message):
                g.load_real_data(DATA/'Storage_Location.csv', DATA/'Picking_Wave.csv', n, 7)

    def test_cli_files_and_failure_no_output(self):
        script = str(Path(g.__file__))
        with tempfile.TemporaryDirectory() as tmp:
            base = [sys.executable, script, '--storage-locations', str(DATA/'Storage_Location.csv'),
                    '--picking-wave', str(DATA/'Picking_Wave.csv'), '--requests', '2']
            for auths in (2, 5):
                dest = Path(tmp)/str(auths)
                result = subprocess.run(base + ['--num-auths', str(auths), '--resources-per-auth', '20',
                                                '--output-dir', str(dest)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                layout = json.loads((dest/'warehouse_layout.json').read_text())
                graph = json.loads((dest/'warehouse.graph').read_text())
                self.assertEqual(layout['entities_per_auth'], 36)
                self.assertEqual(len(graph['entityList']), auths * 36)
                self.assertEqual(set(layout['resource_counts_by_zone'].values()), {20})
                self.assertTrue((dest/'warehouse.policy.json').exists())
                self.assertTrue((dest/'workload.json').exists())
            dest = Path(tmp)/'invalid'
            result = subprocess.run(base + ['--num-auths', '3', '--resources-per-auth', '4000', '--output-dir', str(dest)], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('only', result.stderr)
            self.assertFalse(dest.exists())
            for obsolete in ('--num-resources', '--entities-per-auth', '--robots-per-zone'):
                result = subprocess.run(base + [obsolete, '20'], capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('unrecognized arguments', result.stderr)
            help_result = subprocess.run([sys.executable, script, '--help'], capture_output=True, text=True)
            self.assertEqual(help_result.returncode, 0)
            for option in ('--resources-per-auth', '--num-auths', '--robots-per-auth'):
                self.assertIn(option, help_result.stdout)
            for obsolete in ('--num-resources', '--entities-per-auth', '--robots-per-zone'):
                self.assertNotIn(obsolete, help_result.stdout)



if __name__ == '__main__':
    unittest.main()
