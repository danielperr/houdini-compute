from django.core.management.base import BaseCommand

from compute.jobs import compute_recommendations


class Command(BaseCommand):
    help = "Recompute right-sizing recommendations for every container (run daily)."

    def handle(self, *args, **opts):
        count = compute_recommendations()
        self.stdout.write(f"{count} recommendations written")
