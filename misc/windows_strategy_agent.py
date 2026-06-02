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
import time
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Windows Strategy Lab remote optimizer agent.")
    parser.add_argument("--server", default="https://salavat.home.ro/trading")
    parser.add_argument("--token", default=os.getenv("STRATEGY_WORKER_TOKEN", ""))
    parser.add_argument("--worker", default=os.getenv("COMPUTERNAME", "windows-agent"))
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--work-dir", default=str(PROJECT_ROOT / ".remote_optimizer_work"))
    parser.add_argument("--poll-seconds", type=int, default=10)
    parser.add_argument("--request-timeout", type=int, default=30)
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
        log(f"Agent started. Server={server} Worker={args.worker}", log_file)

        while True:
            if ssh_proc and ssh_proc.poll() is not None:
                raise RuntimeError("SSH tunnel exited.")
            try:
                job = poll_job(server, args.token, args.worker, args.request_timeout)
                if not job:
                    time.sleep(max(1, args.poll_seconds))
                    continue
                log(f"Running job {job['id']} for {job.get('symbol')} {job.get('timeframe')}", log_file)
                payload = run_job(job, args.python, work_dir)
                complete_job(server, args.token, job["id"], payload, args.request_timeout)
                log(f"Completed job {job['id']} returncode={payload['returncode']}", log_file)
            except KeyboardInterrupt:
                return 130
            except Exception as exc:
                log(f"Worker error: {exc}", log_file)
                time.sleep(max(1, args.poll_seconds))
    finally:
        if ssh_proc and ssh_proc.poll() is None:
            ssh_proc.terminate()


if __name__ == "__main__":
    raise SystemExit(main())
