from django.core.management.base import BaseCommand

from compute import demo
from compute.jobs import compute_recommendations


class Command(BaseCommand):
    help = "Fill the database with synthetic clusters, teams and hourly data (development only)."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=14)
        parser.add_argument("--seed", type=int, default=7)
        parser.add_argument("--cluster", action="append", help="Only these demo clusters (repeatable)")

    def handle(self, *args, **opts):
        demo.generate(days=opts["days"], seed=opts["seed"], clusters=opts["cluster"], log=self.stdout.write)
        self.stdout.write(f"{compute_recommendations()} recommendations written")
