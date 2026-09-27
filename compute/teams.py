"""Team mapping: tenant label value -> team -> group, seeded from a CSV.

CSV columns (header required): tenant_label,team,group
Several tenant labels may point to the same team. After the import, the
Governor keeps the mapping up to date in the admin screens.
"""

import csv

from django.db import transaction

from .models import Group, Team, TenantLabel

REQUIRED_COLUMNS = {"tenant_label", "team", "group"}


class MappingError(ValueError):
    pass


@transaction.atomic
def import_mapping(rows):
    """`rows` is an iterable of dicts. Returns (created, updated) label counts."""
    created = updated = 0
    for i, row in enumerate(rows, start=2):  # row 1 is the header
        label = (row.get("tenant_label") or "").strip()
        team_name = (row.get("team") or "").strip()
        group_name = (row.get("group") or "").strip()
        if not label or not team_name:
            raise MappingError(f"line {i}: tenant_label and team are required")
        group = Group.objects.get_or_create(name=group_name)[0] if group_name else None
        team, _ = Team.objects.get_or_create(name=team_name)
        if group and team.group_id != group.id:
            team.group = group
            team.save(update_fields=["group"])
        _, was_created = TenantLabel.objects.update_or_create(value=label, defaults={"team": team})
        created += was_created
        updated += not was_created
    return created, updated


def import_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise MappingError(f"missing columns: {', '.join(sorted(missing))}")
        return import_mapping(reader)


def label_to_team():
    """{tenant_label: (team_name, group_name)} for fast lookups in views."""
    return {
        tl.value: (tl.team.name, tl.team.group.name if tl.team.group else "")
        for tl in TenantLabel.objects.select_related("team__group")
    }
