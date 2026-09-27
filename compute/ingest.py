"""Write one cluster-hour of measurements into the database.

Both the Thanos collector and the synthetic demo generator produce a
`HourSnapshot` and hand it to `write_snapshot`, so the demo exercises the same
write path as production. Writing is idempotent: re-collecting an hour replaces
that hour's facts.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime

from django.conf import settings
from django.db import transaction

from .models import (
    Cluster,
    ContainerHourly,
    Namespace,
    NamespaceQuotaHourly,
    Node,
    NodeHourly,
    Workload,
)

GIB = 1024**3
NON_WORKER_ROLES = {"master", "control-plane", "infra"}

QUOTA_FIELDS = [
    "cpu_request_hard",
    "cpu_request_used",
    "cpu_limit_hard",
    "cpu_limit_used",
    "mem_request_hard",
    "mem_request_used",
    "mem_limit_hard",
    "mem_limit_used",
]


@dataclass
class NamespaceSample:
    name: str
    tenant_label: str = ""
    quota: dict = field(default_factory=dict)  # keys from QUOTA_FIELDS


@dataclass
class NodeSample:
    name: str
    roles: list = field(default_factory=list)
    cpu_capacity: float = 0.0
    mem_capacity: float = 0.0
    cpu_allocatable: float = 0.0
    cpu_requested: float = 0.0
    cpu_usage_avg: float = 0.0
    cpu_usage_max: float = 0.0
    mem_allocatable: float = 0.0
    mem_requested: float = 0.0
    mem_usage_avg: float = 0.0
    mem_usage_max: float = 0.0


@dataclass
class ContainerSample:
    namespace: str
    workload_kind: str
    workload_name: str
    container: str
    replicas: float = 0.0
    cpu_request: float = 0.0
    cpu_limit: float | None = None
    cpu_usage_avg: float = 0.0
    cpu_usage_p95: float = 0.0
    cpu_usage_max: float = 0.0
    cpu_throttled_ratio: float | None = None
    mem_request: float = 0.0
    mem_limit: float | None = None
    mem_usage_avg: float = 0.0
    mem_usage_max: float = 0.0
    restarts: float = 0.0
    oom_kills: float = 0.0


@dataclass
class HourSnapshot:
    cluster: str
    hour: datetime  # start of the hour, tz-aware UTC
    namespaces: list = field(default_factory=list)
    nodes: list = field(default_factory=list)
    containers: list = field(default_factory=list)


def is_platform_namespace(name):
    return re.search(settings.PLATFORM_NAMESPACE_REGEX, name) is not None


def is_worker(roles):
    roles = set(roles)
    return not roles or ("worker" in roles and not roles & NON_WORKER_ROLES)


def node_category(cpu_capacity, mem_capacity):
    return f"{round(cpu_capacity)} vCPU / {round(mem_capacity / GIB)} GiB"


@transaction.atomic
def write_snapshot(snap: HourSnapshot):
    cluster, _ = Cluster.objects.get_or_create(name=snap.cluster)

    namespaces = _upsert_namespaces(cluster, snap)
    workloads = _upsert_workloads(namespaces, snap.containers)
    nodes = _upsert_nodes(cluster, snap)

    ContainerHourly.objects.filter(workload__namespace__cluster=cluster, ts=snap.hour).delete()
    NodeHourly.objects.filter(node__cluster=cluster, ts=snap.hour).delete()
    NamespaceQuotaHourly.objects.filter(namespace__cluster=cluster, ts=snap.hour).delete()

    ContainerHourly.objects.bulk_create(
        [
            ContainerHourly(
                ts=snap.hour,
                workload=workloads[(c.namespace, c.workload_kind, c.workload_name)],
                container=c.container,
                replicas=c.replicas,
                cpu_request=c.cpu_request,
                cpu_limit=c.cpu_limit,
                cpu_usage_avg=c.cpu_usage_avg,
                cpu_usage_p95=c.cpu_usage_p95,
                cpu_usage_max=c.cpu_usage_max,
                cpu_throttled_ratio=c.cpu_throttled_ratio,
                mem_request=c.mem_request,
                mem_limit=c.mem_limit,
                mem_usage_avg=c.mem_usage_avg,
                mem_usage_max=c.mem_usage_max,
                restarts=c.restarts,
                oom_kills=c.oom_kills,
            )
            for c in snap.containers
        ],
        batch_size=2000,
    )
    NodeHourly.objects.bulk_create(
        [
            NodeHourly(
                ts=snap.hour,
                node=nodes[n.name],
                cpu_allocatable=n.cpu_allocatable,
                cpu_requested=n.cpu_requested,
                cpu_usage_avg=n.cpu_usage_avg,
                cpu_usage_max=n.cpu_usage_max,
                mem_allocatable=n.mem_allocatable,
                mem_requested=n.mem_requested,
                mem_usage_avg=n.mem_usage_avg,
                mem_usage_max=n.mem_usage_max,
            )
            for n in snap.nodes
        ],
        batch_size=2000,
    )
    NamespaceQuotaHourly.objects.bulk_create(
        [
            NamespaceQuotaHourly(
                ts=snap.hour,
                namespace=namespaces[ns.name],
                **{f: ns.quota.get(f) for f in QUOTA_FIELDS},
            )
            for ns in snap.namespaces
            if ns.quota
        ],
        batch_size=2000,
    )
    return {"containers": len(snap.containers), "nodes": len(snap.nodes), "namespaces": len(namespaces)}


def _upsert_namespaces(cluster, snap):
    labels = {ns.name: ns.tenant_label for ns in snap.namespaces}
    for c in snap.containers:
        labels.setdefault(c.namespace, "")

    existing = {ns.name: ns for ns in Namespace.objects.filter(cluster=cluster, name__in=labels)}
    to_create, to_update = [], []
    for name, label in labels.items():
        ns = existing.get(name)
        if ns is None:
            to_create.append(
                Namespace(
                    cluster=cluster,
                    name=name,
                    tenant_label=label,
                    is_platform=is_platform_namespace(name),
                    last_seen=snap.hour,
                )
            )
            continue
        # A container-only mention carries no label; don't wipe a known one.
        if label:
            ns.tenant_label = label
        if ns.last_seen is None or snap.hour > ns.last_seen:
            ns.last_seen = snap.hour
        to_update.append(ns)
    Namespace.objects.bulk_create(to_create)
    Namespace.objects.bulk_update(to_update, ["tenant_label", "last_seen"])
    return {ns.name: ns for ns in Namespace.objects.filter(cluster=cluster, name__in=labels)}


def _upsert_workloads(namespaces, containers):
    keys = {(c.namespace, c.workload_kind, c.workload_name) for c in containers}
    ns_ids = {namespaces[k[0]].id for k in keys}

    def load():
        return {
            (w.namespace.name, w.kind, w.name): w
            for w in Workload.objects.filter(namespace_id__in=ns_ids).select_related("namespace")
        }

    existing = load()
    missing = [
        Workload(namespace=namespaces[ns], kind=kind, name=name)
        for (ns, kind, name) in keys
        if (ns, kind, name) not in existing
    ]
    if missing:
        Workload.objects.bulk_create(missing)
        existing = load()
    return existing


def _upsert_nodes(cluster, snap):
    existing = {n.name: n for n in Node.objects.filter(cluster=cluster)}
    to_create, to_update = [], []
    for s in snap.nodes:
        node = existing.get(s.name) or Node(cluster=cluster, name=s.name)
        node.roles = ",".join(sorted(s.roles))
        node.is_worker = is_worker(s.roles)
        node.cpu_capacity = s.cpu_capacity
        node.mem_capacity = s.mem_capacity
        node.category = node_category(s.cpu_capacity, s.mem_capacity)
        if node.last_seen is None or snap.hour > node.last_seen:
            node.last_seen = snap.hour
        (to_update if node.pk else to_create).append(node)
    Node.objects.bulk_create(to_create)
    Node.objects.bulk_update(
        to_update, ["roles", "is_worker", "cpu_capacity", "mem_capacity", "category", "last_seen"]
    )
    return {n.name: n for n in Node.objects.filter(cluster=cluster)}
