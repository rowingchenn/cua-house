#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from cua_house_common.models import BatchCreateRequest, LeaseCompleteRequest, TaskRequirement


@dataclass
class LeaseRunResult:
    task_id: str
    scenario: str
    snapshot_name: str
    requested_vcpus: int
    requested_memory_gb: int
    source_relpath: str | None
    state: str = "queued"
    worker_id: str | None = None
    lease_id: str | None = None
    lease_endpoint: str | None = None
    ready_s: float | None = None
    stage_runtime_s: float | None = None
    completed_s: float | None = None
    error: str | None = None


@dataclass
class ScenarioSpec:
    name: str
    count: int
    snapshot_name: str
    vcpus: int
    memory_gb: int
    disk_gb: int = 64
    hold_s: float = 20.0
    requires_task_data: bool = False
    source_relpath: str | None = None
    task_category: str | None = None
    task_tag: str | None = None


class StressClient:
    def __init__(self, base_url: str, token: str | None = None, timeout: float = 30.0):
        headers: dict[str, str] = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.client = httpx.AsyncClient(base_url=base_url.rstrip("/"), headers=headers, timeout=timeout)

    async def close(self) -> None:
        await self.client.aclose()

    async def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(5):
            try:
                response = await self.client.request(method, path, **kwargs)
                response.raise_for_status()
                return response
            except (httpx.ConnectError, httpx.ReadError, httpx.ReadTimeout, httpx.RemoteProtocolError) as exc:
                last_error = exc
                if attempt == 4:
                    break
                await asyncio.sleep(min(1.0 * (attempt + 1), 4.0))
        assert last_error is not None
        raise last_error


def utc_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def stable_shuffle(items: list[Any], seed: int) -> list[Any]:
    rnd = random.Random(seed)
    out = list(items)
    rnd.shuffle(out)
    return out


def make_task_data(spec: ScenarioSpec, idx: int) -> TaskRequirement.TaskDataRequest | None:
    if not spec.requires_task_data:
        return None
    rel = spec.source_relpath or f"stress/{spec.name}/shared"
    return TaskRequirement.TaskDataRequest(
        requires_task_data=True,
        task_category=spec.task_category or "stress",
        task_tag=spec.task_tag or spec.name,
        source_relpath=rel,
        input_dir=rf"E:\agenthle\{rel.replace('/', chr(92))}\input",
        software_dir=rf"E:\agenthle\{rel.replace('/', chr(92))}\software",
        reference_dir=rf"E:\agenthle\{rel.replace('/', chr(92))}\reference",
        remote_output_dir=rf"E:\agenthle\{rel.replace('/', chr(92))}\output",
    )


def make_task(spec: ScenarioSpec, idx: int, run_prefix: str) -> TaskRequirement:
    task_id = f"{run_prefix}-{spec.name}-{idx:03d}"
    return TaskRequirement(
        task_id=task_id,
        task_path=f"/stress/{spec.name}/{idx}",
        snapshot_name=spec.snapshot_name,
        vcpus=spec.vcpus,
        memory_gb=spec.memory_gb,
        disk_gb=spec.disk_gb,
        metadata={"scenario": spec.name, "run_prefix": run_prefix},
        task_data=make_task_data(spec, idx),
    )


async def submit_batch(client: StressClient, tasks: list[TaskRequirement], batch_id: str) -> dict[str, Any]:
    payload = BatchCreateRequest(batch_id=batch_id, tasks=tasks)
    resp = await client.request("POST", "/v1/batches", json=payload.model_dump(mode="json"))
    return resp.json()


async def get_task(client: StressClient, task_id: str) -> dict[str, Any]:
    resp = await client.request("GET", f"/v1/tasks/{task_id}")
    return resp.json()


async def stage_runtime(client: StressClient, lease_id: str) -> dict[str, Any]:
    resp = await client.request("POST", f"/v1/leases/{lease_id}/stage-runtime")
    return resp.json()


async def heartbeat(client: StressClient, lease_id: str) -> dict[str, Any]:
    resp = await client.request("POST", f"/v1/leases/{lease_id}/heartbeat")
    return resp.json()


async def complete(client: StressClient, lease_id: str, final_status: str = "completed") -> dict[str, Any]:
    payload = LeaseCompleteRequest(final_status=final_status)
    resp = await client.request("POST", f"/v1/leases/{lease_id}/complete", json=payload.model_dump(mode="json"))
    return resp.json()


async def exercise_task(
    client: StressClient,
    result: LeaseRunResult,
    *,
    poll_interval_s: float,
    ready_timeout_s: float,
    hold_s: float,
    do_stage_runtime: bool,
) -> LeaseRunResult:
    started = time.perf_counter()
    while True:
        task = await get_task(client, result.task_id)
        state = task["state"]
        if state in {"failed", "completed"}:
            result.state = state
            result.error = task.get("error")
            result.completed_s = time.perf_counter() - started
            return result
        if state in {"ready", "leased"} and task.get("assignment"):
            assignment = task["assignment"]
            result.state = state
            result.ready_s = time.perf_counter() - started
            result.worker_id = assignment["host_id"]
            result.lease_id = assignment["lease_id"]
            result.lease_endpoint = assignment.get("lease_endpoint")
            if do_stage_runtime:
                stage_started = time.perf_counter()
                await stage_runtime(client, result.lease_id)
                result.stage_runtime_s = time.perf_counter() - stage_started
            lease_deadline = time.perf_counter() + hold_s
            while time.perf_counter() < lease_deadline:
                await asyncio.sleep(min(5.0, max(lease_deadline - time.perf_counter(), 0)))
                if time.perf_counter() < lease_deadline:
                    await heartbeat(client, result.lease_id)
            await complete(client, result.lease_id, final_status="completed")
            settle_deadline = time.perf_counter() + 60.0
            while True:
                final = await get_task(client, result.task_id)
                result.state = final["state"]
                result.error = final.get("error")
                if result.state in {"completed", "failed"}:
                    result.completed_s = time.perf_counter() - started
                    return result
                if time.perf_counter() >= settle_deadline:
                    result.error = result.error or "task did not converge to terminal state within 60s after complete"
                    result.completed_s = time.perf_counter() - started
                    return result
                await asyncio.sleep(2.0)
        if time.perf_counter() - started > ready_timeout_s:
            result.state = "timeout"
            result.error = f"not ready within {ready_timeout_s}s"
            return result
        await asyncio.sleep(poll_interval_s)


def worker_metrics_command(instance: str, project: str, zone: str) -> list[str]:
    remote = (
        "echo INSTANCE=$(hostname); "
        "echo TS=$(date -u +%Y-%m-%dT%H:%M:%SZ); "
        "echo LOAD; cat /proc/loadavg; "
        "echo MEM; free -g; "
        "echo DF; df -h /mnt/xfs /mnt/agenthle-task-data-ro; "
        "echo DOCKER; docker ps --format '{{.Names}}' | wc -l; "
        "echo PROCS; pgrep -af '[q]emu|[d]ocker|[c]ua_house_server.cli' || true"
    )
    return [
        "gcloud", "compute", "ssh", instance,
        "--project", project,
        "--zone", zone,
        "--command", remote,
    ]


async def collect_master_snapshot(client: StressClient) -> dict[str, Any]:
    health = (await client.request("GET", "/healthz")).json()
    workers = (await client.request("GET", "/v1/cluster/workers")).json()
    return {"ts": utc_ts(), "health": health, "workers": workers}


async def collect_worker_metrics(instances: list[str], project: str, zone: str) -> dict[str, str]:
    loop = asyncio.get_running_loop()
    results: dict[str, str] = {}

    async def run_one(instance: str) -> None:
        def _run() -> str:
            proc = subprocess.run(worker_metrics_command(instance, project, zone), capture_output=True, text=True, check=False)
            return proc.stdout if proc.returncode == 0 else (proc.stdout + "\n" + proc.stderr)
        results[instance] = await loop.run_in_executor(None, _run)

    await asyncio.gather(*(run_one(i) for i in instances))
    return results


async def sample_cluster(
    client: StressClient,
    *,
    instances: list[str],
    project: str,
    zone: str,
    interval_s: float,
    stop_event: asyncio.Event,
    sample_path: Path,
) -> None:
    while not stop_event.is_set():
        payload: dict[str, Any]
        try:
            snapshot = await collect_master_snapshot(client)
            workers = await collect_worker_metrics(instances, project, zone)
            payload = {"snapshot": snapshot, "worker_metrics": workers}
        except Exception as exc:
            payload = {"ts": utc_ts(), "sample_error": repr(exc)}
        sample_path.parent.mkdir(parents=True, exist_ok=True)
        with sample_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            pass


def scenario_library(shared_relpath: str) -> dict[str, ScenarioSpec]:
    return {
        "cache_hit_free": ScenarioSpec("cache_hit_free", count=4, snapshot_name="cpu-free", vcpus=4, memory_gb=15, hold_s=20),
        "cache_hit_license": ScenarioSpec("cache_hit_license", count=4, snapshot_name="cpu-license", vcpus=4, memory_gb=15, hold_s=20),
        "cold_start_license_on_other": ScenarioSpec("cold_start_license_on_other", count=4, snapshot_name="cpu-license", vcpus=4, memory_gb=8, hold_s=20),
        "mixed_medium": ScenarioSpec("mixed_medium", count=6, snapshot_name="cpu-free-ubuntu", vcpus=8, memory_gb=30, hold_s=20),
        "same_task_concurrent": ScenarioSpec(
            "same_task_concurrent",
            count=4,
            snapshot_name="cpu-free-ubuntu",
            vcpus=4,
            memory_gb=8,
            hold_s=20,
            requires_task_data=True,
            source_relpath=shared_relpath,
            task_category="stress",
            task_tag="same-task-concurrent",
        ),
    }


def expand_scenarios(names: list[str], shared_relpath: str) -> list[ScenarioSpec]:
    lib = scenario_library(shared_relpath)
    specs: list[ScenarioSpec] = []
    for name in names:
        if name == "all":
            return list(lib.values())
        if name not in lib:
            raise SystemExit(f"unknown scenario: {name}")
        specs.append(lib[name])
    return specs


def append_temp_doc(doc_path: Path, text: str) -> None:
    doc_path.parent.mkdir(parents=True, exist_ok=True)
    with doc_path.open("a", encoding="utf-8") as handle:
        handle.write(text.rstrip() + "\n")


def parse_count_overrides(raw: str | None) -> dict[str, int]:
    overrides: dict[str, int] = {}
    if not raw:
        return overrides
    for item in raw.split(","):
        entry = item.strip()
        if not entry:
            continue
        name, sep, value = entry.partition("=")
        if not sep:
            raise SystemExit(f"invalid count override: {entry}")
        overrides[name.strip()] = int(value.strip())
    return overrides


async def main() -> int:
    parser = argparse.ArgumentParser(description="Mock stress runner for cua-house cluster.")
    parser.add_argument("--base-url", default=os.environ.get("CUA_HOUSE_SERVER_URL", "http://10.128.0.16:8787"))
    parser.add_argument("--token", default=os.environ.get("CUA_HOUSE_TOKEN"))
    parser.add_argument("--project", default="sunblaze-4")
    parser.add_argument("--zone", default="us-central1-a")
    parser.add_argument("--workers", default="agenthle-nested-kvm-02,agenthle-nested-kvm-03")
    parser.add_argument("--scenarios", default="cache_hit_free,same_task_concurrent")
    parser.add_argument("--run-prefix", default=f"stress-{int(time.time())}")
    parser.add_argument("--shared-relpath", default="stress/shared-output-isolation/base")
    parser.add_argument("--count-overrides", default="")
    parser.add_argument("--hold-s", type=float, default=None)
    parser.add_argument("--poll-interval-s", type=float, default=5.0)
    parser.add_argument("--ready-timeout-s", type=float, default=900.0)
    parser.add_argument("--sample-interval-s", type=float, default=10.0)
    parser.add_argument("--stage-runtime", action="store_true")
    parser.add_argument("--output-dir", default="tmp/stress")
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    result_path = out_dir / f"{args.run_prefix}-results.json"
    sample_path = out_dir / f"{args.run_prefix}-samples.jsonl"
    doc_path = out_dir / "stress-notes.md"

    workers = [w.strip() for w in args.workers.split(",") if w.strip()]
    specs = expand_scenarios([s.strip() for s in args.scenarios.split(",") if s.strip()], args.shared_relpath)
    count_overrides = parse_count_overrides(args.count_overrides)
    for spec in specs:
        if spec.name in count_overrides:
            spec.count = count_overrides[spec.name]
    if args.hold_s is not None:
        for spec in specs:
            spec.hold_s = args.hold_s

    append_temp_doc(
        doc_path,
        f"## {utc_ts()} {args.run_prefix}\n"
        f"- base_url: `{args.base_url}`\n"
        f"- workers: `{', '.join(workers)}`\n"
        f"- scenarios: `{', '.join(spec.name for spec in specs)}`\n"
        f"- stage_runtime: `{args.stage_runtime}`\n"
        f"- sample_interval_s: `{args.sample_interval_s}`\n",
    )

    client = StressClient(args.base_url, token=args.token)
    stop_event = asyncio.Event()
    sampler = asyncio.create_task(
        sample_cluster(
            client,
            instances=workers,
            project=args.project,
            zone=args.zone,
            interval_s=args.sample_interval_s,
            stop_event=stop_event,
            sample_path=sample_path,
        )
    )

    try:
        tasks: list[TaskRequirement] = []
        results: dict[str, LeaseRunResult] = {}
        for spec in specs:
            for idx in range(spec.count):
                task = make_task(spec, idx, args.run_prefix)
                tasks.append(task)
                results[task.task_id] = LeaseRunResult(
                    task_id=task.task_id,
                    scenario=spec.name,
                    snapshot_name=spec.snapshot_name,
                    requested_vcpus=spec.vcpus,
                    requested_memory_gb=spec.memory_gb,
                    source_relpath=task.task_data.source_relpath if task.task_data else None,
                )

        tasks = stable_shuffle(tasks, seed=7)
        batch_id = f"{args.run_prefix}-batch"
        await submit_batch(client, tasks, batch_id)

        append_temp_doc(doc_path, f"- submitted batch `{batch_id}` with `{len(tasks)}` tasks")

        by_name = {spec.name: spec for spec in specs}
        coros = [
            exercise_task(
                client,
                results[task.task_id],
                poll_interval_s=args.poll_interval_s,
                ready_timeout_s=args.ready_timeout_s,
                hold_s=by_name[results[task.task_id].scenario].hold_s,
                do_stage_runtime=args.stage_runtime,
            )
            for task in tasks
        ]
        finished = await asyncio.gather(*coros)
        payload = {
            "run_prefix": args.run_prefix,
            "ts": utc_ts(),
            "batch_id": batch_id,
            "scenarios": [spec.__dict__ for spec in specs],
            "results": [r.__dict__ for r in finished],
        }
        result_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

        succeeded = sum(1 for r in finished if r.state == "completed")
        failed = [r for r in finished if r.state != "completed"]
        workers_seen = sorted({r.worker_id for r in finished if r.worker_id})
        append_temp_doc(
            doc_path,
            f"- completed: `{succeeded}/{len(finished)}`\n"
            f"- workers_seen: `{workers_seen}`\n"
            f"- failed: `{len(failed)}`\n"
            + ("\n".join([f"  - `{r.task_id}` `{r.state}` `{r.error}`" for r in failed]) if failed else ""),
        )
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0 if not failed else 1
    finally:
        stop_event.set()
        try:
            await asyncio.wait_for(sampler, timeout=15)
        except Exception:
            sampler.cancel()
        await client.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
