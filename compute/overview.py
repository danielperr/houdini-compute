"""Numbers behind the Overview page."""

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta

from django.conf import settings
from django.db.models import F, Max, Sum

from .models import Cluster, ContainerHourly, NamespaceQuotaHourly, Node, NodeHourly, Recommendation
from .teams import label_to_team

GIB = 1024**3
RESOURCES = {"cpu": ("CPU", "cores", 1.0), "mem": ("Memory", "GiB", GIB)}
PLATFORM = "Platform"
UNMAPPED = "Unmapped"


@dataclass
class ClusterSummary:
    name: str
    latest: object = None
    allocatable: float = 0.0
    quota: float = 0.0
    requested: float = 0.0
    requested_tenant: float = 0.0
    requested_platform: float = 0.0
    used: float = 0.0
    reserve: float = 0.0
    reserve_threshold: float = 0.0  # requested level at which the reserve target is breached
    runway_days: float | None = None  # None: not shrinking
    reclaimable: float = 0.0
    node_size: float = 0.0
    trend: dict = field(default_factory=dict)

    @property
    def reserve_ok(self):
        return self.reserve >= settings.TARGET_RESERVE

    @property
    def reclaimable_nodes(self):
        return self.reclaimable / self.node_size if self.node_size else 0.0


def _scale(value, resource):
    return value / RESOURCES[resource][2]


def cluster_summary(cluster, resource, profile, window_days):
    r = resource
    s = ClusterSummary(name=cluster.name)
    workers = NodeHourly.objects.filter(node__cluster=cluster, node__is_worker=True)
    s.latest = workers.aggregate(ts=Max("ts"))["ts"]
    if s.latest is None:
        return s
    since = s.latest - timedelta(days=window_days)

    hourly = (
        workers.filter(ts__gt=since)
        .values("ts")
        .annotate(
            alloc=Sum(f"{r}_allocatable"), req=Sum(f"{r}_requested"), used=Sum(f"{r}_usage_avg")
        )
        .order_by("ts")
    )
    points = [(h["ts"], _scale(h["alloc"], r), _scale(h["req"], r), _scale(h["used"], r)) for h in hourly]
    _, s.allocatable, s.requested, s.used = points[-1]

    split = (
        ContainerHourly.objects.filter(workload__namespace__cluster=cluster, ts=s.latest)
        .values("workload__namespace__is_platform")
        .annotate(total=Sum(F(f"{r}_request") * F("replicas")))
    )
    for row in split:
        value = _scale(row["total"] or 0, r)
        if row["workload__namespace__is_platform"]:
            s.requested_platform = value
        else:
            s.requested_tenant = value

    quota = NamespaceQuotaHourly.objects.filter(
        namespace__cluster=cluster, namespace__is_platform=False, ts=s.latest
    ).aggregate(total=Sum(f"{r}_request_hard"))["total"]
    s.quota = _scale(quota or 0, r)

    s.reserve = 1 - s.requested / s.allocatable if s.allocatable else 0
    s.reserve_threshold = s.allocatable * (1 - settings.TARGET_RESERVE)
    s.runway_days = _runway_days(points, s.reserve_threshold)

    s.reclaimable = _scale(
        Recommendation.objects.filter(
            workload__namespace__cluster=cluster, profile=profile, window_days=window_days
        ).aggregate(total=Sum(f"{r}_reclaimable"))["total"]
        or 0,
        r,
    )
    sizes = Node.objects.filter(cluster=cluster, is_worker=True).values_list(
        "cpu_capacity" if r == "cpu" else "mem_capacity", flat=True
    )
    s.node_size = _scale(statistics.median(sizes), r) if sizes else 0

    s.trend = _trend(points, s.reserve_threshold, daily=window_days > 7)
    return s


def _daily(points):
    days = defaultdict(list)
    for ts, *values in points:
        days[ts.date()].append(values)
    return [(day, *[sum(col) / len(col) for col in zip(*rows)]) for day, rows in sorted(days.items())]


def _runway_days(points, threshold):
    """Days until requests cross the reserve threshold, from a linear fit of daily averages.

    0 means already breached; None means requests are flat or shrinking (or too
    little history to tell).
    """
    daily = _daily(points)[-30:]
    if not daily:
        return None
    current = points[-1][2]
    if current >= threshold:
        return 0.0
    if len(daily) < 3:
        return None
    xs = list(range(len(daily)))
    ys = [d[2] for d in daily]
    slope = statistics.linear_regression(xs, ys).slope
    if slope <= 0:
        return None
    return (threshold - current) / slope


def _trend(points, threshold, daily):
    rows = _daily(points) if daily else [(ts, *v) for ts, *v in points]
    fmt = "%Y-%m-%d" if daily else "%Y-%m-%d %H:00"
    return {
        "x": [t.strftime(fmt) for t, *_ in rows],
        "allocatable": [round(v[0], 2) for _, *v in rows],
        "requested": [round(v[1], 2) for _, *v in rows],
        "used": [round(v[2], 2) for _, *v in rows],
        "threshold": round(threshold, 2),
    }


@dataclass
class TeamRow:
    team: str
    group: str
    kind: str = "team"  # team | platform | unmapped
    quota: float = 0.0
    requested: float = 0.0
    used: float = 0.0
    reclaimable: float = 0.0
    labels: set = field(default_factory=set)

    @property
    def efficiency(self):
        return min(1.0, self.used / self.requested) if self.requested else None


def team_rows(clusters, resource, profile, window_days):
    """Per-team totals across the given clusters, biggest reclaimable first."""
    r = resource
    mapping = label_to_team()
    rows = {}

    def row_for(label, is_platform):
        if is_platform:
            key, group, kind = PLATFORM, "", "platform"
        elif label in mapping:
            (key, group), kind = mapping[label], "team"
        else:
            key, group, kind = f"{UNMAPPED}: {label or '(no label)'}", "", "unmapped"
        row = rows.setdefault(key, TeamRow(team=key, group=group, kind=kind))
        if label:
            row.labels.add(label)
        return row

    recs = (
        Recommendation.objects.filter(
            workload__namespace__cluster__in=clusters, profile=profile, window_days=window_days
        )
        .values("workload__namespace__tenant_label", "workload__namespace__is_platform")
        .annotate(
            requested=Sum(F(f"{r}_request_current") * F("replicas_avg")),
            used=Sum(F(f"{r}_usage_avg") * F("replicas_avg")),
            reclaimable=Sum(f"{r}_reclaimable"),
        )
    )
    for rec in recs:
        row = row_for(rec["workload__namespace__tenant_label"], rec["workload__namespace__is_platform"])
        row.requested += _scale(rec["requested"] or 0, r)
        row.used += _scale(rec["used"] or 0, r)
        row.reclaimable += _scale(rec["reclaimable"] or 0, r)

    for cluster in clusters:
        latest = NamespaceQuotaHourly.objects.filter(namespace__cluster=cluster).aggregate(ts=Max("ts"))["ts"]
        quotas = (
            NamespaceQuotaHourly.objects.filter(namespace__cluster=cluster, ts=latest)
            .values("namespace__tenant_label", "namespace__is_platform")
            .annotate(total=Sum(f"{r}_request_hard"))
        )
        for q in quotas:
            row = row_for(q["namespace__tenant_label"], q["namespace__is_platform"])
            row.quota += _scale(q["total"] or 0, r)

    return sorted(rows.values(), key=lambda t: (t.kind == "platform", -t.reclaimable))


def overview(resource, profile, window_days, cluster_name=None):
    clusters = list(Cluster.objects.all())
    if cluster_name:
        clusters = [c for c in clusters if c.name == cluster_name]
    summaries = [cluster_summary(c, resource, profile, window_days) for c in clusters]
    teams = team_rows(clusters, resource, profile, window_days)
    tenant_teams = [t for t in teams if t.kind != "platform"]
    total_reclaimable = sum(t.reclaimable for t in tenant_teams)
    largest = max((t.reclaimable for t in tenant_teams), default=0)
    for t in teams:
        tenant = t.kind != "platform"
        t.share = t.reclaimable / total_reclaimable if tenant and total_reclaimable else 0
        t.relative = t.reclaimable / largest if tenant and largest else 0
    return {
        "all_clusters": [c.name for c in Cluster.objects.all()],
        "summaries": summaries,
        "teams": teams,
        "total_reclaimable": total_reclaimable,
        "unmapped": [t for t in teams if t.kind == "unmapped"],
    }
