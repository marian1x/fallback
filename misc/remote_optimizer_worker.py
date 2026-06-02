#!/usr/bin/env python3
"""Remote Strategy Lab optimizer worker.

Run this on a stronger machine to offload optimizer CPU work from the PI5.
The PI5 sends historical OHLC bars and optimizer arguments. No Alpaca keys are
sent to the worker.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import requests


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def build_url(base: str, path: str) -> str:
    return urljoin(base.rstrip("/") + "/", path.lstrip("/"))


def substitute_args(args: list[str], bars_csv: str, report_json: str, top_csv: str) -> list[str]:
    return [
        arg.replace("__BARS_CSV__", bars_csv)
        .replace("__REPORT_JSON__", report_json)
        .replace("__TOP_CSV__", top_csv)
        for arg in args
    ]


def poll_job(server: str, token: str, worker: str, timeout: int) -> dict | None:
    response = requests.get(
        build_url(server, "/api/admin/strategy/remote_jobs/next"),
        headers={"X-Strategy-Worker-Token": token, "X-Strategy-Worker": worker},
        params={"worker": worker},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    return payload.get("job")


def complete_job(server: str, token: str, job_id: str, payload: dict, timeout: int) -> None:
    response = requests.post(
        build_url(server, f"/api/admin/strategy/remote_jobs/{job_id}/complete"),
        headers={"X-Strategy-Worker-Token": token},
        json=payload,
        timeout=timeout,
    )
    response.raise_for_status()


def run_job(job: dict, python_bin: str, work_dir: Path) -> dict:
    job_id = job["id"]
    job_dir = work_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    bars_path = job_dir / "bars.csv"
    report_path = job_dir / "report.json"
    top_path = job_dir / "top.csv"
    bars_path.write_text(job.get("bars_csv", ""), encoding="utf-8")

    optimizer_args = substitute_args(
        list(job.get("optimizer_args") or []),
        str(bars_path),
        str(report_path),
        str(top_path),
    )
    command = [python_bin] + optimizer_args
    result = subprocess.run(
        command,
        cwd=str(PROJECT_ROOT),
        text=True,
        capture_output=True,
        timeout=None,
    )

    report = None
    if report_path.exists():
        report = json.loads(report_path.read_text(encoding="utf-8"))
    top_csv = top_path.read_text(encoding="utf-8") if top_path.exists() else ""
    return {
        "returncode": result.returncode,
        "stdout": result.stdout[-20000:],
        "stderr": result.stderr[-20000:],
        "report": report,
        "top_csv": top_csv,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Poll PI5 Strategy Lab jobs and run optimizer remotely.")
    parser.add_argument("--server", required=True, help="Dashboard base URL, e.g. https://salavat.home.ro/trading")
    parser.add_argument("--token", default=os.getenv("STRATEGY_WORKER_TOKEN", ""))
    parser.add_argument("--worker", default=socket.gethostname())
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--work-dir", default=str(PROJECT_ROOT / ".remote_optimizer_work"))
    parser.add_argument("--poll-seconds", type=int, default=10)
    parser.add_argument("--request-timeout", type=int, default=30)
    parser.add_argument("--once", action="store_true", help="Process at most one job and exit.")
    args = parser.parse_args()

    if not args.token:
        print("Missing token. Pass --token or set STRATEGY_WORKER_TOKEN.", file=sys.stderr)
        return 2

    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    while True:
        try:
            job = poll_job(args.server, args.token, args.worker, args.request_timeout)
            if not job:
                if args.once:
                    print("No job available.")
                    return 0
                time.sleep(max(1, args.poll_seconds))
                continue

            print(f"Running job {job['id']} for {job.get('symbol')} {job.get('timeframe')}")
            payload = run_job(job, args.python, work_dir)
            complete_job(args.server, args.token, job["id"], payload, args.request_timeout)
            print(f"Completed job {job['id']} with returncode={payload['returncode']}")
            if args.once:
                return 0
        except KeyboardInterrupt:
            return 130
        except Exception as exc:
            print(f"Worker error: {exc}", file=sys.stderr)
            if args.once:
                return 1
            time.sleep(max(1, args.poll_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
