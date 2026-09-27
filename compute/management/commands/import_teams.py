from django.core.management.base import BaseCommand, CommandError

from compute.teams import MappingError, import_csv


class Command(BaseCommand):
    help = "Import the tenant_label,team,group mapping from a CSV file."

    def add_arguments(self, parser):
        parser.add_argument("path")

    def handle(self, *args, **opts):
        try:
            created, updated = import_csv(opts["path"])
        except (MappingError, OSError) as e:
            raise CommandError(str(e)) from e
        self.stdout.write(f"{created} tenant labels created, {updated} updated")
