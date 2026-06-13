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
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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


def set_optimizer_option(args: list[str], option: str, value: str | None) -> list[str]:
    if not value:
        return args
    out = list(args)
    if option in out:
        idx = out.index(option)
        if idx + 1 < len(out):
            out[idx + 1] = value
        else:
            out.append(value)
    else:
        out.extend([option, value])
    return out


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


def resolve_optimizer_jobs_value(spec: str | None) -> str | None:
    """Translate an --optimizer-jobs spec into a concrete --jobs value.

    'max'     -> all logical cores on THIS machine (e.g. 16 on a Ryzen 9 8945HS)
    'auto'    -> 0, letting the optimizer pick cpu_count-1
    'inherit' -> None, keep whatever the dashboard sent
    integer   -> that exact count
    """
    spec = (spec or "max").strip().lower()
    if spec in ("inherit", "", "none"):
        return None
    if spec == "auto":
        return "0"
    if spec == "max":
        return str(os.cpu_count() or 1)
    try:
        return str(max(1, int(spec)))
    except ValueError:
        return str(os.cpu_count() or 1)


def run_job(job: dict, python_bin: str, work_dir: Path, accelerator: str | None = None, optimizer_jobs: str | None = "max") -> dict:
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
    optimizer_args = set_optimizer_option(optimizer_args, "--accelerator", accelerator)
    jobs_value = resolve_optimizer_jobs_value(optimizer_jobs)
    if jobs_value is not None:
        optimizer_args = set_optimizer_option(optimizer_args, "--jobs", jobs_value)
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


def worker_loop(args, work_dir: Path, stop_event: threading.Event, worker_name: str) -> int:
    processed = 0
    while not stop_event.is_set():
        try:
            job = poll_job(args.server, args.token, worker_name, args.request_timeout)
            if not job:
                if args.once:
                    return processed
                stop_event.wait(max(1, args.poll_seconds))
                continue

            print(f"[{worker_name}] Running job {job['id']} for {job.get('symbol')} {job.get('timeframe')}", flush=True)
            payload = run_job(job, args.python, work_dir, args.accelerator, args.optimizer_jobs)
            complete_job(args.server, args.token, job["id"], payload, args.request_timeout)
            processed += 1
            print(f"[{worker_name}] Completed job {job['id']} with returncode={payload['returncode']}", flush=True)
            if args.once:
                return processed
        except KeyboardInterrupt:
            stop_event.set()
            return processed
        except Exception as exc:
            print(f"[{worker_name}] Worker error: {exc}", file=sys.stderr, flush=True)
            if args.once:
                return processed
            stop_event.wait(max(1, args.poll_seconds))
    return processed


def main() -> int:
    parser = argparse.ArgumentParser(description="Poll PI5 Strategy Lab jobs and run optimizer remotely.")
    parser.add_argument("--server", required=True, help="Dashboard base URL, e.g. https://salavat.home.ro/trading")
    parser.add_argument("--token", default=os.getenv("STRATEGY_WORKER_TOKEN", ""))
    parser.add_argument("--worker", default=socket.gethostname())
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--work-dir", default=str(PROJECT_ROOT / ".remote_optimizer_work"))
    parser.add_argument("--poll-seconds", type=int, default=10)
    parser.add_argument("--request-timeout", type=int, default=180)
    parser.add_argument("--workers", type=int, default=1, help="Parallel queue workers. Use with optimizer --jobs carefully.")
    parser.add_argument("--once", action="store_true", help="Process at most one job and exit.")
    parser.add_argument("--accelerator", choices=["auto", "cpu", "gpu"], default=os.getenv("STRATEGY_ACCELERATOR", "auto"))
    parser.add_argument(
        "--optimizer-jobs",
        default=os.getenv("STRATEGY_OPTIMIZER_JOBS", "max"),
        help="Cores for the optimizer on THIS machine: 'max' (all logical cores), 'auto' (cpu_count-1), 'inherit', or an integer.",
    )
    args = parser.parse_args()

    if not args.token:
        print("Missing token. Pass --token or set STRATEGY_WORKER_TOKEN.", file=sys.stderr)
        return 2

    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    worker_count = max(1, int(args.workers))
    stop_event = threading.Event()
    try:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [
                executor.submit(worker_loop, args, work_dir, stop_event, f"{args.worker}-{idx + 1}")
                for idx in range(worker_count)
            ]
            if args.once:
                processed = sum(f.result() for f in futures)
                if processed == 0:
                    print("No job available.")
                return 0
            for future in futures:
                future.result()
    except KeyboardInterrupt:
        stop_event.set()
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
