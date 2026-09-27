import time
from datetime import datetime, timedelta, timezone

from django.core.management.base import BaseCommand, CommandError

from compute.collector import Collector
from compute.ingest import write_snapshot
from compute.models import Cluster, CollectorRun
from compute.thanos import ThanosClient, ThanosError


def last_complete_hour(now=None):
    now = now or datetime.now(timezone.utc)
    return now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)


class Command(BaseCommand):
    help = (
        "Collect hourly facts from Thanos. Run hourly (a few minutes past the hour). "
        "Thanos keeps ~7 days, so --backfill-hours up to ~160 recovers from outages."
    )

    def add_arguments(self, parser):
        parser.add_argument("--hour", help="Start of the hour to collect, ISO 8601 UTC (default: last full hour)")
        parser.add_argument(
            "--backfill-hours", type=int, default=0,
            help="Also collect the previous N hours that have no successful run yet",
        )
        parser.add_argument("--cluster", action="append", help="Only these clusters (repeatable)")
        parser.add_argument("--force", action="store_true", help="Re-collect hours that already succeeded")

    def handle(self, *args, **opts):
        if opts["hour"]:
            hour = datetime.fromisoformat(opts["hour"]).replace(minute=0, second=0, microsecond=0)
            if hour.tzinfo is None:
                hour = hour.replace(tzinfo=timezone.utc)
        else:
            hour = last_complete_hour()
        hours = [hour - timedelta(hours=i) for i in range(opts["backfill_hours"], -1, -1)]

        collector = Collector(ThanosClient.from_settings())
        try:
            clusters = opts["cluster"] or collector.clusters(hour)
        except ThanosError as e:
            raise CommandError(str(e)) from e
        if not clusters:
            raise CommandError("No clusters found in Thanos (check CLUSTER_LABEL)")

        failures = 0
        for name in clusters:
            cluster, _ = Cluster.objects.get_or_create(name=name)
            done = set(
                CollectorRun.objects.filter(
                    cluster=cluster, hour__in=hours, status=CollectorRun.STATUS_OK, source="thanos"
                ).values_list("hour", flat=True)
            )
            for h in hours:
                if h in done and not opts["force"]:
                    continue
                started = time.monotonic()
                run = CollectorRun(cluster=cluster, hour=h, source="thanos")
                try:
                    counts = write_snapshot(collector.collect(name, h))
                    run.containers, run.nodes, run.namespaces = (
                        counts["containers"], counts["nodes"], counts["namespaces"]
                    )
                    self.stdout.write(f"{name} {h:%Y-%m-%d %H:00}: {counts}")
                except ThanosError as e:
                    failures += 1
                    run.status, run.message = CollectorRun.STATUS_ERROR, str(e)[:2000]
                    self.stderr.write(f"{name} {h:%Y-%m-%d %H:00}: {e}")
                run.duration_seconds = time.monotonic() - started
                run.save()
        if failures:
            raise CommandError(f"{failures} cluster-hour(s) failed")
