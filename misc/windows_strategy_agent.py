#!/usr/bin/env python3
"""Windows-friendly Strategy Lab remote optimizer agent.

This is a small standalone runner for Windows 11. It can either connect to the
dashboard over HTTPS or create an outbound SSH tunnel first, then poll PI5 jobs.
Run it with Task Scheduler to keep it in the background.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from remote_optimizer_worker import complete_job, poll_job, run_job


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def log(message: str, log_file: Path) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    print(line, flush=True)
    with log_file.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def start_ssh_tunnel(args, log_file: Path) -> subprocess.Popen | None:
    if not args.ssh_target:
        return None
    command = [
        "ssh",
        "-N",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-L", f"{args.local_port}:127.0.0.1:{args.remote_dashboard_port}",
    ]
    if args.ssh_key:
        command.extend(["-i", args.ssh_key])
    command.append(args.ssh_target)
    log(f"Starting SSH tunnel: localhost:{args.local_port} -> PI5:127.0.0.1:{args.remote_dashboard_port}", log_file)
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(3)
    if proc.poll() is not None:
        stderr = proc.stderr.read() if proc.stderr else ""
        raise RuntimeError(f"SSH tunnel failed: {stderr.strip()}")
    return proc


def agent_worker_loop(args, server: str, work_dir: Path, log_file: Path, stop_event: threading.Event, worker_name: str) -> int:
    processed = 0
    while not stop_event.is_set():
        try:
            job = poll_job(server, args.token, worker_name, args.request_timeout)
            if not job:
                stop_event.wait(max(1, args.poll_seconds))
                continue
            log(f"[{worker_name}] Running job {job['id']} for {job.get('symbol')} {job.get('timeframe')}", log_file)
            payload = run_job(job, args.python, work_dir, args.accelerator, args.optimizer_jobs)
            complete_job(server, args.token, job["id"], payload, args.request_timeout)
            processed += 1
            log(f"[{worker_name}] Completed job {job['id']} returncode={payload['returncode']}", log_file)
        except KeyboardInterrupt:
            stop_event.set()
            return processed
        except Exception as exc:
            log(f"[{worker_name}] Worker error: {exc}", log_file)
            stop_event.wait(max(1, args.poll_seconds))
    return processed


def main() -> int:
    parser = argparse.ArgumentParser(description="Windows Strategy Lab remote optimizer agent.")
    parser.add_argument("--server", default="https://salavat.home.ro/trading")
    parser.add_argument("--token", default=os.getenv("STRATEGY_WORKER_TOKEN", ""))
    parser.add_argument("--worker", default=os.getenv("COMPUTERNAME", "windows-agent"))
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--work-dir", default=str(PROJECT_ROOT / ".remote_optimizer_work"))
    parser.add_argument("--poll-seconds", type=int, default=10)
    parser.add_argument("--request-timeout", type=int, default=180)
    parser.add_argument("--workers", type=int, default=1, help="Parallel queue workers for multiple queued symbols.")
    parser.add_argument("--accelerator", choices=["auto", "cpu", "gpu"], default=os.getenv("STRATEGY_ACCELERATOR", "auto"))
    parser.add_argument(
        "--optimizer-jobs",
        default=os.getenv("STRATEGY_OPTIMIZER_JOBS", "max"),
        help="Cores for the optimizer on this PC: 'max' (all 16 logical cores on a Ryzen 9 8945HS), 'auto' (cpu_count-1), 'inherit', or an integer.",
    )
    parser.add_argument("--log-file", default=str(PROJECT_ROOT / "strategy_agent.log"))
    parser.add_argument("--ssh-target", default="", help="Optional SSH target, e.g. pi5@salavat.home.ro")
    parser.add_argument("--ssh-key", default="", help="Optional SSH private key path")
    parser.add_argument("--local-port", type=int, default=8765)
    parser.add_argument("--remote-dashboard-port", type=int, default=5050)
    args = parser.parse_args()

    if not args.token:
        print("Missing token. Pass --token or set STRATEGY_WORKER_TOKEN.", file=sys.stderr)
        return 2

    log_file = Path(args.log_file)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    ssh_proc = None
    server = args.server
    try:
        ssh_proc = start_ssh_tunnel(args, log_file)
        if ssh_proc:
            server = f"http://127.0.0.1:{args.local_port}"
        worker_count = max(1, int(args.workers))
        log(f"Agent started. Server={server} Worker={args.worker} Workers={worker_count} Accelerator={args.accelerator} OptimizerJobs={args.optimizer_jobs} (cpu_count={os.cpu_count()})", log_file)
        stop_event = threading.Event()
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [
                executor.submit(agent_worker_loop, args, server, work_dir, log_file, stop_event, f"{args.worker}-{idx + 1}")
                for idx in range(worker_count)
            ]
            if ssh_proc and ssh_proc.poll() is not None:
                raise RuntimeError("SSH tunnel exited.")
            try:
                while True:
                    if ssh_proc and ssh_proc.poll() is not None:
                        raise RuntimeError("SSH tunnel exited.")
                    time.sleep(5)
            except KeyboardInterrupt:
                stop_event.set()
                return 130
            finally:
                stop_event.set()
                for future in futures:
                    future.result()
    finally:
        if ssh_proc and ssh_proc.poll() is None:
            ssh_proc.terminate()


if __name__ == "__main__":
    raise SystemExit(main())
