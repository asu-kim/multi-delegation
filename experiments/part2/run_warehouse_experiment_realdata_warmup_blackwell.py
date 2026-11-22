#!/usr/bin/env python3
"""Blackwell worker for the A6000 warehouse runner (SSH JSON-lines control).

Only Auth 101's Java process is placed in a fresh cgroup before exec. CPU comes
from cpu.stat usage_usec; memory.peak is reset after warm-up and read through the
same open descriptor. This measures cgroup memory, NOT Java heap or process RSS.
Requires cgroup v2, a delegated empty parent with memory enabled, and a kernel
supporting writable memory.peak. See README_distributed.md. No third-party libs.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import signal
import sys
import time
import uuid

import run_warehouse_experiment_realdata_warmup_a6000 as runner


class Auth101Cgroup:
    def __init__(self, parent: Path):
        if not (parent / 'cgroup.controllers').is_file():
            raise RuntimeError(f'{parent} is not a cgroup v2 directory')
        if (parent / 'cgroup.procs').read_text().strip():
            raise RuntimeError('The delegated cgroup parent must be empty')
        if 'memory' not in (parent / 'cgroup.subtree_control').read_text().split():
            raise RuntimeError('Enable memory in the parent cgroup.subtree_control first')
        self.path = parent / f'auth101-{uuid.uuid4().hex}'
        self.path.mkdir()
        self.peak_fd = None
        self.started = False
        self.final = None
        self.pid = None
        try:
            self.peak_fd = os.open(self.path / 'memory.peak', os.O_RDWR)
            self.reset_peak()  # Fail before generation/startup on unsupported kernels.
            self.cpu_usec()
        except BaseException:
            self.close()
            raise RuntimeError(
                'Cannot reset memory.peak/read cpu.stat. Check cgroup delegation '
                'and kernel support for writable memory.peak; no RSS fallback is used.'
            ) from None

    def reset_peak(self):
        os.lseek(self.peak_fd, 0, os.SEEK_SET)
        os.write(self.peak_fd, b'0')

    def peak(self):
        os.lseek(self.peak_fd, 0, os.SEEK_SET)
        return int(os.read(self.peak_fd, 128).strip())

    def cpu_usec(self):
        fields = dict(line.split() for line in (self.path / 'cpu.stat').read_text().splitlines())
        return int(fields['usage_usec'])

    def start(self, pid):
        if self.started:
            raise RuntimeError('Measurement already started')
        members = (self.path / 'cgroup.procs').read_text().split()
        if members != [str(pid)]:
            raise RuntimeError(f'Expected only Auth101 PID {pid} in cgroup; got {members}')
        self.pid = pid
        self.reset_peak()
        self.baseline_cpu = self.cpu_usec()
        self.start_time = time.monotonic_ns()
        self.started = True
        return self.read()

    def read(self):
        if self.final is not None:
            return self.final
        if not self.started:
            raise RuntimeError('Measurement has not started')
        cpu = self.cpu_usec() - self.baseline_cpu
        elapsed = (time.monotonic_ns() - self.start_time) / 1e9
        return {
            'pid': self.pid,
            'cgroup': str(self.path),
            'measured_seconds': elapsed,
            'cpu_total_seconds': cpu / 1e6,
            'cpu_utilization_percent': (cpu / 1e6 / elapsed * 100) if elapsed > 0 else None,
            'memory_peak_bytes': self.peak(),
            'cpu_percent_basis': '100_percent_is_one_cpu_core',
            'memory_measure': 'cgroup_v2_memory.peak_not_RSS_or_heap',
            'measurement_scope': 'after_warmup_until_stop_command',
            'memory_peak_reset': 'same_open_file_descriptor_after_warmup',
        }

    def stop(self):
        if self.final is None and self.started:
            self.final = self.read()
        return self.final

    def close(self):
        if self.peak_fd is not None:
            os.close(self.peak_fd)
            self.peak_fd = None
        try:
            self.path.rmdir()
        except OSError as exc:
            print(f'Could not remove cgroup {self.path}: {exc}', file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--graph', required=True)
    parser.add_argument('--project-root', default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument('--cgroup-parent', required=True)
    parser.add_argument('--startup-timeout', type=float, default=10)
    parser.add_argument('--prepare', action='store_true', help='Generate a consistent set for both hosts, then exit')
    parser.add_argument('--results', default=None)
    args = parser.parse_args()
    root = Path(args.project_root).resolve()
    graph_path = Path(args.graph).resolve()
    graph = runner.load_json(graph_path)
    runner.validate_hosts(graph)
    output = Path(args.results) if args.results else root / 'experiments/part2/results/auth101_blackwell.json'
    # Logs must never enter the stdout control channel.
    protocol = sys.stdout
    def reply(value):
        protocol.write(json.dumps(value) + '\n')
        protocol.flush()
    def interrupted(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    with contextlib.redirect_stdout(sys.stderr):
        monitor = Auth101Cgroup(Path(args.cgroup_parent))
        processes = {}
        try:
            if args.prepare:
                runner.run_generate_all(graph_path, root)
                return
            processes, _ = runner.start_auths(
                {**graph, 'authList': [a for a in graph['authList'] if int(a['id']) == 101]},
                root / 'iotauth/auth/auth-server', args.startup_timeout,
                cgroup=monitor.path)
            proc = processes[101]
            reply({'ready': True, 'pid': proc.pid})
            for line in sys.stdin:
                try:
                    command = json.loads(line)['command']
                    if command == 'shutdown':
                        reply({'shutdown': True})
                        break
                    if proc.poll() is not None:
                        raise RuntimeError(f'Auth 101 exited with code {proc.returncode}')
                    if command == 'start':
                        result = monitor.start(proc.pid)
                    elif command == 'read':
                        result = monitor.read()
                    elif command == 'stop':
                        result = monitor.stop()
                        runner.write_json({'auth_id': 101, 'metrics': result}, output)
                    elif command == 'db_size':
                        result = {'bytes': (root / 'iotauth/auth/databases/auth101/auth.db').stat().st_size}
                    else:
                        raise ValueError(f'Unknown command: {command}')
                    reply(result)
                except Exception as exc:
                    reply({'error': f'{type(exc).__name__}: {exc}'})
        finally:
            try:
                result = monitor.stop()
                if result is not None:
                    runner.write_json({'auth_id': 101, 'metrics': result}, output)
            finally:
                for proc in processes.values():
                    runner.terminate_process(proc, 'Auth101')
                monitor.close()


if __name__ == '__main__':
    main()
