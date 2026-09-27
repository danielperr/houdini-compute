"""Synthetic but realistic data for development and demos.

Produces the same `HourSnapshot`s the Thanos collector produces, so everything
downstream (ingest, recommendations, pages) runs unchanged. The data contains
the patterns the tool is meant to surface: padded requests (request = limit),
idle workloads, under-requested workloads that get throttled or OOMKilled,
CronJobs, Postgres StatefulSets, uneven nodes and slowly growing requests.
"""

import math
import random
from dataclasses import dataclass
from datetime import timedelta

from django.utils import timezone

from .ingest import ContainerSample, HourSnapshot, NamespaceSample, NodeSample, write_snapshot
from .teams import import_mapping

GIB = 1024**3
MIB = 1024**2

GROUPS = {
    "Payments": ["Ledger", "Checkout", "Fraud", "Billing", "Settlement"],
    "Retail": ["Catalog", "Search", "Cart", "Pricing", "Promotions"],
    "Data": ["Ingest", "Reporting", "Lakehouse", "Streaming"],
    "Core Services": ["Identity", "Notifications", "Gateway", "Scheduler", "Audit", "Files"],
}
UNMAPPED_TENANTS = ["legacy-migration"]  # shows up as "unmapped" until the Governor maps it

PLATFORM_NAMESPACES = [
    "openshift-monitoring",
    "openshift-ingress",
    "openshift-logging",
    "openshift-image-registry",
    "openshift-dns",
    "openshift-gitops",
]


@dataclass
class ClusterSpec:
    name: str
    workers: list  # [(count, cpu, mem_gib)]
    cpu_target: float  # requested / allocatable at the end of the period
    mem_target: float
    ns_per_team: int


CLUSTERS = [
    ClusterSpec("prod-east", [(40, 16, 64), (10, 32, 128)], 0.90, 0.62, 3),
    ClusterSpec("prod-west", [(24, 16, 64)], 0.74, 0.55, 2),
    ClusterSpec("nonprod", [(16, 16, 64)], 0.66, 0.48, 2),
]


@dataclass
class WorkloadSpec:
    namespace: str
    kind: str
    name: str
    container: str
    archetype: str
    replicas: int
    cpu_request: float
    cpu_limit: float
    mem_request: float
    mem_limit: float
    cpu_base: float  # typical usage per replica at the daily peak
    mem_base: float
    start_day: int  # day index the workload appears (models growth)
    run_hour: int = 0  # CronJobs only


def tenant_label(team):
    return f"t-{team.lower().replace(' ', '-')}"


def mapping_rows():
    return [
        {"tenant_label": tenant_label(team), "team": team, "group": group}
        for group, teams in GROUPS.items()
        for team in teams
    ]


def _workload(rng, ns, idx, days):
    archetype = rng.choices(
        ["padded", "right", "bursty", "idle", "hungry", "cron", "postgres"],
        weights=[30, 25, 12, 10, 8, 8, 7],
    )[0]
    start_day = 0 if rng.random() < 0.8 else rng.randint(1, max(1, days - 1))
    cpu_req = rng.choice([0.25, 0.5, 1, 1, 2, 2, 4])
    mem_req = rng.choice([256, 512, 1024, 1024, 2048, 4096]) * MIB
    replicas = rng.choice([1, 2, 2, 3, 3, 4, 6])
    kind, container = "Deployment", "app"

    if archetype == "padded":  # request = limit, uses a fraction
        cpu_base, mem_base = cpu_req * rng.uniform(0.05, 0.25), mem_req * rng.uniform(0.2, 0.45)
        cpu_lim, mem_lim = cpu_req, mem_req
    elif archetype == "right":
        cpu_base, mem_base = cpu_req * rng.uniform(0.55, 0.8), mem_req * rng.uniform(0.6, 0.8)
        cpu_lim, mem_lim = cpu_req * 2, mem_req
    elif archetype == "bursty":
        cpu_base, mem_base = cpu_req * rng.uniform(0.3, 0.6), mem_req * rng.uniform(0.4, 0.6)
        cpu_lim, mem_lim = cpu_req * 3, mem_req * 1.5
    elif archetype == "idle":
        cpu_base, mem_base = 0.003, mem_req * 0.1
        cpu_lim, mem_lim = cpu_req, mem_req
    elif archetype == "hungry":  # under-requested: throttled, close to its memory limit
        cpu_req, mem_req = rng.choice([0.1, 0.25, 0.5]), rng.choice([256, 512]) * MIB
        cpu_base, mem_base = cpu_req * rng.uniform(1.2, 1.8), mem_req * rng.uniform(1.1, 1.3)
        cpu_lim, mem_lim = cpu_req * 1.5, mem_req * 1.35
    elif archetype == "cron":
        kind, replicas = "CronJob", 1
        cpu_base, mem_base = cpu_req * rng.uniform(0.3, 0.7), mem_req * rng.uniform(0.3, 0.7)
        cpu_lim, mem_lim = cpu_req, mem_req
    else:  # postgres: StatefulSet, memory-heavy, steady
        kind, container, replicas = "StatefulSet", "database", rng.choice([2, 3])
        mem_req = rng.choice([4, 8, 16]) * GIB
        cpu_base, mem_base = cpu_req * rng.uniform(0.15, 0.4), mem_req * rng.uniform(0.7, 0.9)
        cpu_lim, mem_lim = cpu_req * 2, mem_req

    name = {"cron": f"job-{idx}", "postgres": f"pg-{idx}"}.get(archetype, f"svc-{idx}")
    return WorkloadSpec(
        ns, kind, name, container, archetype, replicas, cpu_req, cpu_lim, mem_req, mem_lim,
        cpu_base, mem_base, start_day, run_hour=rng.randint(0, 23),
    )


def _platform_workloads(rng, ns):
    return [
        WorkloadSpec(
            ns, "Deployment", f"{ns.removeprefix('openshift-')}-{i}", "main", "right", 2,
            1.0, 2.0, 2 * GIB, 2 * GIB, 0.6, 1.4 * GIB, 0,
        )
        for i in range(rng.randint(2, 4))
    ]


def _daily_curve(hour_of_day, weekday):
    business = 0.55 + 0.45 * max(0.0, math.sin(math.pi * (hour_of_day - 6) / 14)) if 6 <= hour_of_day <= 20 else 0.55
    return business * (0.8 if weekday >= 5 else 1.0)


def generate(days=14, seed=7, now=None, clusters=None, log=print):
    rng = random.Random(seed)
    now = (now or timezone.now()).replace(minute=0, second=0, microsecond=0)
    start = now - timedelta(days=days)
    import_mapping(mapping_rows())

    teams = [t for ts in GROUPS.values() for t in ts]
    specs = [c for c in CLUSTERS if not clusters or c.name in clusters]
    for spec in specs:
        workers = [
            (f"{spec.name}-worker-{cpu}c-{i:02d}", cpu, mem * GIB)
            for count, cpu, mem in spec.workers
            for i in range(count)
        ]
        others = [(f"{spec.name}-master-{i}", "master", 8, 32 * GIB) for i in range(3)] + [
            (f"{spec.name}-infra-{i}", "infra", 16, 64 * GIB) for i in range(3)
        ]
        # Allocatable is capacity minus system reservations.
        alloc = {n: (cpu - 0.5, mem - 3 * GIB) for n, cpu, mem in workers}
        cpu_alloc = sum(a[0] for a in alloc.values())
        mem_alloc = sum(a[1] for a in alloc.values())

        tenants = [tenant_label(t) for t in teams] + UNMAPPED_TENANTS
        workloads, ns_labels = [], {}
        for tenant in tenants:
            for n in range(spec.ns_per_team):
                ns = f"{tenant.removeprefix('t-')}-{['prod', 'stage', 'dev'][n]}"
                ns_labels[ns] = tenant
                workloads += [_workload(rng, ns, i, days) for i in range(rng.randint(3, 6))]
        for ns in PLATFORM_NAMESPACES:
            ns_labels[ns] = ""
            workloads += _platform_workloads(rng, ns)

        # Scale tenant sizes so the cluster ends the period at the target load.
        def final_total(attr):
            return sum(getattr(w, attr) * w.replicas for w in workloads if w.kind != "CronJob")

        cpu_scale = spec.cpu_target * cpu_alloc / final_total("cpu_request")
        mem_scale = spec.mem_target * mem_alloc / final_total("mem_request")
        for w in workloads:
            for attr, scale in (
                ("cpu_request", cpu_scale), ("cpu_limit", cpu_scale), ("cpu_base", cpu_scale),
                ("mem_request", mem_scale), ("mem_limit", mem_scale), ("mem_base", mem_scale),
            ):
                setattr(w, attr, getattr(w, attr) * scale)

        # Quota is sized at the end-of-period footprint times a hoarding factor.
        quota_hard = {}
        for ns in ns_labels:
            f = rng.uniform(1.15, 2.4)
            base = [w for w in workloads if w.namespace == ns and w.kind != "CronJob"]
            quota_hard[ns] = (
                round(sum(w.cpu_request * w.replicas for w in base) * f, 1) or 1.0,
                sum(w.mem_request * w.replicas for w in base) * f or GIB,
            )
        node_weight = {n: alloc[n][0] * rng.uniform(0.9, 1.08) for n, _, _ in workers}
        usage_weight = {n: alloc[n][0] * rng.uniform(0.6, 1.4) for n, _, _ in workers}

        hour = start
        count = 0
        while hour < now:
            write_snapshot(
                _snapshot(rng, spec, hour, start, workloads, ns_labels, quota_hard, workers, others,
                          alloc, node_weight, usage_weight)
            )
            hour += timedelta(hours=1)
            count += 1
        log(f"{spec.name}: {len(workloads)} workloads, {len(workers)} workers, {count} hours")


def _snapshot(rng, spec, hour, start, workloads, ns_labels, quota_hard, workers, others, alloc,
              node_weight, usage_weight):
    day = (hour - start).days
    curve = _daily_curve(hour.hour, hour.weekday())
    containers = []
    ns_req = {ns: [0.0, 0.0, 0.0, 0.0] for ns in ns_labels}  # cpu req, cpu lim, mem req, mem lim
    for w in workloads:
        if day < w.start_day:
            continue
        if w.kind == "CronJob":
            if hour.hour != w.run_hour:
                continue
            replicas = rng.uniform(0.2, 0.6)  # runs for part of the hour
        else:
            replicas = float(w.replicas)
        noise = rng.uniform(0.85, 1.15)
        cpu_avg = w.cpu_base * curve * noise
        spike = {"bursty": rng.uniform(2.5, 4.0), "hungry": 1.3}.get(w.archetype, rng.uniform(1.2, 1.6))
        cpu_p95 = cpu_avg * min(spike, 1.8)
        cpu_max = cpu_avg * spike
        if w.cpu_limit:
            cpu_max, cpu_p95, cpu_avg = (min(v, w.cpu_limit) for v in (cpu_max, cpu_p95, cpu_avg))
        mem_avg = w.mem_base * (0.9 + 0.1 * curve) * rng.uniform(0.95, 1.05)
        mem_max = min(mem_avg * rng.uniform(1.03, 1.12), w.mem_limit)
        oom = 1.0 if w.archetype == "hungry" and rng.random() < 0.04 else 0.0
        throttled = 0.35 * rng.uniform(0.7, 1.3) if w.archetype == "hungry" else rng.uniform(0, 0.02)
        containers.append(
            ContainerSample(
                namespace=w.namespace, workload_kind=w.kind, workload_name=w.name, container=w.container,
                replicas=replicas, cpu_request=w.cpu_request, cpu_limit=w.cpu_limit,
                cpu_usage_avg=cpu_avg, cpu_usage_p95=cpu_p95, cpu_usage_max=cpu_max,
                cpu_throttled_ratio=throttled, mem_request=w.mem_request, mem_limit=w.mem_limit,
                mem_usage_avg=mem_avg, mem_usage_max=mem_max, restarts=oom, oom_kills=oom,
            )
        )
        t = ns_req[w.namespace]
        t[0] += w.cpu_request * replicas
        t[1] += w.cpu_limit * replicas
        t[2] += w.mem_request * replicas
        t[3] += w.mem_limit * replicas

    namespaces = []
    for ns, label in ns_labels.items():
        cpu_r, cpu_l, mem_r, mem_l = ns_req[ns]
        hard_cpu, hard_mem = quota_hard[ns]
        namespaces.append(
            NamespaceSample(
                name=ns, tenant_label=label,
                quota={
                    "cpu_request_hard": hard_cpu, "cpu_request_used": cpu_r,
                    "cpu_limit_hard": round(hard_cpu * 2, 1), "cpu_limit_used": cpu_l,
                    "mem_request_hard": hard_mem, "mem_request_used": mem_r,
                    "mem_limit_hard": hard_mem * 2, "mem_limit_used": mem_l,
                },
            )
        )

    total = {k: sum(getattr(c, k) * c.replicas for c in containers) for k in
             ("cpu_request", "mem_request", "cpu_usage_avg", "cpu_usage_max", "mem_usage_avg", "mem_usage_max")}
    wsum = sum(node_weight.values())
    usum = sum(usage_weight.values())
    nodes = []
    for name, cpu, mem in workers:
        share, ushare = node_weight[name] / wsum, usage_weight[name] / usum
        a_cpu, a_mem = alloc[name]
        nodes.append(
            NodeSample(
                name=name, roles=["worker"], cpu_capacity=cpu, mem_capacity=mem,
                cpu_allocatable=a_cpu, mem_allocatable=a_mem,
                cpu_requested=min(a_cpu, total["cpu_request"] * share),
                mem_requested=min(a_mem, total["mem_request"] * share),
                cpu_usage_avg=total["cpu_usage_avg"] * ushare + 0.4,
                cpu_usage_max=total["cpu_usage_max"] * ushare * 0.6 + 0.8,
                mem_usage_avg=total["mem_usage_avg"] * ushare + 4 * GIB,
                mem_usage_max=total["mem_usage_max"] * ushare + 4.5 * GIB,
            )
        )
    for name, role, cpu, mem in others:
        nodes.append(
            NodeSample(
                name=name, roles=[role], cpu_capacity=cpu, mem_capacity=mem,
                cpu_allocatable=cpu - 1, mem_allocatable=mem - 4 * GIB,
                cpu_requested=cpu * 0.5, mem_requested=mem * 0.5,
                cpu_usage_avg=cpu * 0.3, cpu_usage_max=cpu * 0.5,
                mem_usage_avg=mem * 0.45, mem_usage_max=mem * 0.5,
            )
        )
    return HourSnapshot(cluster=spec.name, hour=hour, namespaces=namespaces, nodes=nodes, containers=containers)
