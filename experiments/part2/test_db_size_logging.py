"""Run with: python3 -m unittest test_db_size_logging -v"""
import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import run_warehouse_experiment_realdata as runner


class DatabaseSizeLoggingTests(unittest.TestCase):
    def make_database(self, root, auth_id, size):
        path = root / 'iotauth/auth/databases' / f'auth{auth_id}' / 'auth.db'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'x' * size)
        return path

    def test_logs_individual_sizes_totals_and_signed_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            first = self.make_database(root, 101, 100)
            second = self.make_database(root, 102, 200)
            logger = runner.AuthDatabaseSizeLogger(root, {'authList': [{'id': 101}, {'id': 102}]})
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                baseline = logger.record('before_workload')
                first.write_bytes(b'x' * 130)
                second.write_bytes(b'x' * 190)
                changed = logger.record('after_delegation', 1, 'request-A')
                unchanged = logger.record('after_revocation', 1, 'request-A')
            self.assertEqual(baseline['auth_db_size_bytes'], 300)
            self.assertIsNone(baseline['auth_db_size_delta_bytes'])
            self.assertEqual(changed['auth_db_size_delta_bytes_by_auth'], {'101': 30, '102': -10})
            self.assertEqual(changed['auth_db_size_bytes'], 320)
            self.assertEqual(changed['auth_db_size_delta_bytes'], 20)
            self.assertEqual(unchanged['auth_db_size_delta_bytes'], 0)
            self.assertIn('Auth101: size=130 bytes delta=+30 bytes', output.getvalue())
            self.assertIn('Auth102: size=190 bytes delta=-10 bytes', output.getvalue())
            self.assertIn('request=1 request_id=request-A total=320 bytes delta=+20 bytes', output.getvalue())

    def test_missing_files_are_not_reported_as_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            self.make_database(root, 101, 0)
            logger = runner.AuthDatabaseSizeLogger(root, {'authList': [{'id': 101}, {'id': 102}]})
            with contextlib.redirect_stdout(io.StringIO()):
                missing = logger.record('before_workload')
                self.make_database(root, 102, 100)
                recovered = logger.record('after_delegation')
            self.assertEqual(missing['auth_db_size_bytes_by_auth'], {'101': 0, '102': None})
            self.assertIsNone(missing['auth_db_size_bytes'])
            self.assertIn('FileNotFoundError', missing['errors']['102'])
            self.assertEqual(recovered['auth_db_size_bytes'], 100)
            self.assertIsNone(recovered['auth_db_size_delta_bytes'])
            self.assertIsNone(recovered['auth_db_size_delta_bytes_by_auth']['102'])

    def test_runner_writes_three_separate_json_files_incrementally(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            db = self.make_database(root, 101, 100)
            graph = {'authList': [{'id': 101}], 'assignments': {'net1.supervisor': 101},
                     'entityList': [
                         {'group': 'Supervisors', 'name': 'net1.supervisor'},
                         {'group': 'Robot1', 'name': 'net1.robot1'},
                         {'group': 'Item_1', 'name': 'net1.item1'},
                     ]}
            request = {'request_id': 0, 'supervisor': 'net1.supervisor', 'selected_robot': 'Robot1',
                       'resource': 'Item_1', 'resource_zone': 'A', 'selected_robot_distance_m': 1,
                       'item_id': '1', 'storage_location_id': 'location1'}
            graph_path = root / 'graph.json'
            workload_path = root / 'workload.json'
            result_path = root / 'results.json'
            graph_path.write_text(json.dumps(graph))
            workload_path.write_text(json.dumps({'requests': [request]}))
            args = SimpleNamespace(graph=str(graph_path), workload=str(workload_path), results=str(result_path),
                                   project_root=str(root), startup_timeout=1, command_timeout=1,
                                   access_timeout=1, validity='1*day', inter_request_delay=0, keep_processes=False)

            def delegation(*args, **kwargs):
                db.write_bytes(b'x' * 140)
                return {'latency_ms': 1.25}

            proc = Mock()
            with contextlib.ExitStack() as stack:
                stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                stack.enter_context(patch.object(runner, 'parse_args', return_value=args))
                stack.enter_context(patch.object(runner, 'run_generate_all'))
                stack.enter_context(patch.object(runner, 'start_auths', return_value=({101: proc}, {})))
                stack.enter_context(patch.object(runner, 'start_user_entity', return_value=(proc, Mock())))
                stack.enter_context(patch.object(runner, 'start_resource_entity', return_value=(proc, Mock())))
                stack.enter_context(patch.object(runner, 'delegate', side_effect=delegation))
                stack.enter_context(patch.object(runner, 'revoke', return_value={'latency_ms': 2.5}))
                stack.enter_context(patch.object(runner, 'access_attempt', side_effect=[
                    {'status': 'success', 'latency_ms': 3.5}, {'status': 'denied', 'latency_ms': 4.5}]))
                stack.enter_context(patch.object(runner, 'terminate_process'))
                stack.enter_context(patch.object(runner.time, 'sleep'))
                writer = stack.enter_context(patch.object(runner, 'write_json', wraps=runner.write_json))
                runner.main()
            result = json.loads(result_path.read_text())
            database_output = json.loads((root / 'results_db_size.json').read_text())
            details_output = json.loads((root / 'results_results.json').read_text())
            self.assertEqual(set(result), {'configuration', 'summary'})
            self.assertEqual(set(database_output), {'db_size'})
            self.assertEqual(set(details_output), {'results'})
            self.assertEqual(result['configuration']['num_selected_requests'], 1)
            self.assertEqual(details_output['results'][0]['request_id'], 0)
            # Both the incremental save and final save must use the split format.
            self.assertEqual(writer.call_count, 6)
            expected_keys = {
                result_path: {'configuration', 'summary'},
                root / 'results_db_size.json': {'db_size'},
                root / 'results_results.json': {'results'},
            }
            for call in writer.call_args_list:
                data, path = call.args
                self.assertEqual(set(data), expected_keys[path])
            snapshots = database_output['db_size']
            self.assertEqual([s['phase'] for s in snapshots], [
                'before_workload', 'after_workload', 'after_delegation', 'after_access_before_revoke',
                'after_revocation', 'after_access_after_revoke'])
            self.assertEqual([s['auth_db_size_delta_bytes'] for s in snapshots], [None, 0, 40, 0, 0, 0])
            self.assertTrue(all(s['request_index'] == 1 and s['request_id'] == 0 for s in snapshots[2:]))
            self.assertEqual(snapshots[1]['auth_db_size_bytes'], snapshots[-1]['auth_db_size_bytes'])
            self.assertIsNone(snapshots[1]['request_index'])
            self.assertIsNone(snapshots[1]['request_id'])
            self.assertEqual(result['summary']['delegation_latency_ms_mean'], 1.25)
            self.assertEqual(result['summary']['revocation_latency_ms_mean'], 2.5)


if __name__ == '__main__':
    unittest.main()
