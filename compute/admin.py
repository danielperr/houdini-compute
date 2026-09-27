from django.contrib import admin

from .models import (
    Cluster,
    CollectorRun,
    Group,
    Namespace,
    Node,
    Recommendation,
    Team,
    TenantLabel,
    Workload,
)

admin.site.site_header = "Houdini Compute administration"


class TenantLabelInline(admin.TabularInline):
    model = TenantLabel
    extra = 1


class TeamInline(admin.TabularInline):
    model = Team
    extra = 0
    fields = ["name"]
    show_change_link = True


@admin.register(Group)
class GroupAdmin(admin.ModelAdmin):
    search_fields = ["name"]
    inlines = [TeamInline]


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display = ["name", "group", "labels"]
    list_filter = ["group"]
    search_fields = ["name", "tenant_labels__value"]
    inlines = [TenantLabelInline]

    @admin.display(description="Tenant labels")
    def labels(self, obj):
        return ", ".join(tl.value for tl in obj.tenant_labels.all())

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("group").prefetch_related("tenant_labels")


@admin.register(TenantLabel)
class TenantLabelAdmin(admin.ModelAdmin):
    list_display = ["value", "team"]
    list_select_related = ["team"]
    search_fields = ["value", "team__name"]
    autocomplete_fields = ["team"]


@admin.register(Cluster)
class ClusterAdmin(admin.ModelAdmin):
    search_fields = ["name"]


@admin.register(Namespace)
class NamespaceAdmin(admin.ModelAdmin):
    list_display = ["name", "cluster", "tenant_label", "is_platform", "last_seen"]
    list_filter = ["cluster", "is_platform"]
    search_fields = ["name", "tenant_label"]
    readonly_fields = ["cluster", "name", "tenant_label", "last_seen"]


@admin.register(Workload)
class WorkloadAdmin(admin.ModelAdmin):
    list_display = ["name", "kind", "namespace"]
    list_filter = ["kind", "namespace__cluster"]
    search_fields = ["name", "namespace__name"]
    list_select_related = ["namespace__cluster"]


@admin.register(Node)
class NodeAdmin(admin.ModelAdmin):
    list_display = ["name", "cluster", "roles", "is_worker", "category", "last_seen"]
    list_filter = ["cluster", "is_worker", "category"]
    search_fields = ["name"]


@admin.register(CollectorRun)
class CollectorRunAdmin(admin.ModelAdmin):
    list_display = ["hour", "cluster", "source", "status", "duration_seconds", "containers", "nodes", "namespaces"]
    list_filter = ["status", "cluster", "source"]
    date_hierarchy = "hour"


@admin.register(Recommendation)
class RecommendationAdmin(admin.ModelAdmin):
    list_display = [
        "workload", "container", "profile", "window_days",
        "cpu_request_current", "cpu_request_recommended", "cpu_reclaimable",
    ]
    list_filter = ["profile", "window_days", "workload__namespace__cluster"]
    search_fields = ["workload__name", "workload__namespace__name"]
    list_select_related = ["workload__namespace__cluster"]
