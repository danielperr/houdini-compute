"""Batch jobs that derive data from the collected facts."""

from datetime import timedelta
from itertools import groupby

from django.db import transaction
from django.utils import timezone

from .models import Cluster, ContainerHourly, NodeHourly, Recommendation
from .recommend import PROFILES, WINDOWS, HourStat, recommend

STAT_FIELDS = [
    "replicas",
    "cpu_request",
    "cpu_limit",
    "cpu_usage_avg",
    "cpu_usage_p95",
    "cpu_usage_max",
    "mem_request",
    "mem_limit",
    "mem_usage_avg",
    "mem_usage_max",
]


def collected_hours(cluster, since, until):
    """How many hours of the window actually have data for this cluster."""
    return (
        NodeHourly.objects.filter(node__cluster=cluster, ts__gte=since, ts__lt=until)
        .values("ts")
        .distinct()
        .count()
    )


def compute_recommendations(now=None):
    """Recompute every container's recommendation for every profile and window."""
    now = now or timezone.now()
    until = now.replace(minute=0, second=0, microsecond=0)
    total = 0
    for cluster in Cluster.objects.all():
        window_hours = {w: collected_hours(cluster, until - timedelta(days=w), until) for w in WINDOWS}
        oldest = until - timedelta(days=max(WINDOWS))
        rows = (
            ContainerHourly.objects.filter(workload__namespace__cluster=cluster, ts__gte=oldest, ts__lt=until)
            .order_by("workload_id", "container", "ts")
            .values_list("workload_id", "container", "ts", *STAT_FIELDS)
            .iterator(chunk_size=5000)
        )
        recs = []
        for (workload_id, container), group in groupby(rows, key=lambda r: (r[0], r[1])):
            hours = [(r[2], HourStat(*r[3:])) for r in group]
            for window in WINDOWS:
                since = until - timedelta(days=window)
                stats = [s for ts, s in hours if ts >= since]
                for profile in PROFILES.values():
                    result = recommend(stats, profile, window_hours[window])
                    if result is None:
                        continue
                    recs.append(
                        Recommendation(
                            workload_id=workload_id,
                            container=container,
                            profile=profile.name,
                            window_days=window,
                            computed_at=now,
                            **result.__dict__,
                        )
                    )
        with transaction.atomic():
            Recommendation.objects.filter(workload__namespace__cluster=cluster).delete()
            Recommendation.objects.bulk_create(recs, batch_size=2000)
        total += len(recs)
    return total
