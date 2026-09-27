"""Data model.

Dimensions describe *what* exists (clusters, teams, namespaces, workloads,
nodes). Facts are hourly measurements collected from Thanos. Units are always
CPU cores and memory bytes; conversion to display units happens in views.

Containers are stored per workload, not per pod: pods churn, workloads don't.
Per-replica values (request, usage) plus an average replica count per hour are
enough to answer both "how big is one replica" and "how much does the workload
hold in total".
"""

from django.db import models


class Cluster(models.Model):
    name = models.CharField(max_length=128, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Group(models.Model):
    """Organisational level above teams."""

    name = models.CharField(max_length=128, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Team(models.Model):
    """A tenant. Namespaces map to a team through their tenant label value."""

    name = models.CharField(max_length=128, unique=True)
    group = models.ForeignKey(Group, null=True, blank=True, on_delete=models.SET_NULL, related_name="teams")

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class TenantLabel(models.Model):
    """Maps a namespace tenant-label value to a team (many values -> one team)."""

    value = models.CharField(max_length=253, unique=True)
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="tenant_labels")

    class Meta:
        ordering = ["value"]

    def __str__(self):
        return f"{self.value} -> {self.team}"


class Namespace(models.Model):
    cluster = models.ForeignKey(Cluster, on_delete=models.CASCADE, related_name="namespaces")
    name = models.CharField(max_length=253)
    # Raw label value as seen on the namespace; resolved to a team at query time
    # so that editing the mapping never requires re-collecting data.
    tenant_label = models.CharField(max_length=253, blank=True, db_index=True)
    is_platform = models.BooleanField(default=False)
    last_seen = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["cluster__name", "name"]
        constraints = [models.UniqueConstraint(fields=["cluster", "name"], name="uniq_namespace")]

    def __str__(self):
        return f"{self.cluster}/{self.name}"


class Workload(models.Model):
    KINDS = ["Deployment", "StatefulSet", "DaemonSet", "CronJob", "Job", "ReplicaSet", "Pod", "Other"]

    namespace = models.ForeignKey(Namespace, on_delete=models.CASCADE, related_name="workloads")
    kind = models.CharField(max_length=64)
    name = models.CharField(max_length=253)

    class Meta:
        ordering = ["namespace__cluster__name", "namespace__name", "name"]
        constraints = [models.UniqueConstraint(fields=["namespace", "kind", "name"], name="uniq_workload")]

    def __str__(self):
        return f"{self.namespace}/{self.kind}/{self.name}"


class Node(models.Model):
    cluster = models.ForeignKey(Cluster, on_delete=models.CASCADE, related_name="nodes")
    name = models.CharField(max_length=253)
    roles = models.CharField(max_length=253, blank=True)
    # Worker nodes that tenant workloads can land on (not master / infra).
    is_worker = models.BooleanField(default=True)
    cpu_capacity = models.FloatField(default=0)
    mem_capacity = models.FloatField(default=0)
    # Shape bucket derived from capacity, e.g. "16 vCPU / 64 GiB". Nodes of one
    # MachineSet share a shape, so this approximates MachineSet categories
    # without needing machine API access.
    category = models.CharField(max_length=64, blank=True)
    last_seen = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["cluster__name", "name"]
        constraints = [models.UniqueConstraint(fields=["cluster", "name"], name="uniq_node")]

    def __str__(self):
        return f"{self.cluster}/{self.name}"


class ContainerHourly(models.Model):
    """One container of one workload, aggregated over one hour.

    Request/limit/usage values are per replica; `replicas` is the average number
    of running replicas during the hour.
    """

    ts = models.DateTimeField(help_text="Start of the hour (UTC)")
    workload = models.ForeignKey(Workload, on_delete=models.CASCADE, related_name="hourly")
    container = models.CharField(max_length=253)
    replicas = models.FloatField(default=0)

    cpu_request = models.FloatField(default=0)
    cpu_limit = models.FloatField(null=True, blank=True)
    cpu_usage_avg = models.FloatField(default=0)
    cpu_usage_p95 = models.FloatField(default=0)
    cpu_usage_max = models.FloatField(default=0)
    cpu_throttled_ratio = models.FloatField(null=True, blank=True)

    mem_request = models.FloatField(default=0)
    mem_limit = models.FloatField(null=True, blank=True)
    mem_usage_avg = models.FloatField(default=0)
    mem_usage_max = models.FloatField(default=0)

    restarts = models.FloatField(default=0)
    oom_kills = models.FloatField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["workload", "container", "ts"], name="uniq_container_hour")
        ]
        indexes = [models.Index(fields=["ts"])]


class NodeHourly(models.Model):
    ts = models.DateTimeField()
    node = models.ForeignKey(Node, on_delete=models.CASCADE, related_name="hourly")

    cpu_allocatable = models.FloatField(default=0)
    cpu_requested = models.FloatField(default=0)
    cpu_usage_avg = models.FloatField(default=0)
    cpu_usage_max = models.FloatField(default=0)

    mem_allocatable = models.FloatField(default=0)
    mem_requested = models.FloatField(default=0)
    mem_usage_avg = models.FloatField(default=0)
    mem_usage_max = models.FloatField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["node", "ts"], name="uniq_node_hour")]
        indexes = [models.Index(fields=["ts"])]


class NamespaceQuotaHourly(models.Model):
    """ResourceQuota hard/used per namespace. Null means "no quota on this resource"."""

    ts = models.DateTimeField()
    namespace = models.ForeignKey(Namespace, on_delete=models.CASCADE, related_name="quota_hourly")

    cpu_request_hard = models.FloatField(null=True, blank=True)
    cpu_request_used = models.FloatField(null=True, blank=True)
    cpu_limit_hard = models.FloatField(null=True, blank=True)
    cpu_limit_used = models.FloatField(null=True, blank=True)
    mem_request_hard = models.FloatField(null=True, blank=True)
    mem_request_used = models.FloatField(null=True, blank=True)
    mem_limit_hard = models.FloatField(null=True, blank=True)
    mem_limit_used = models.FloatField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["namespace", "ts"], name="uniq_quota_hour")]
        indexes = [models.Index(fields=["ts"])]


class CollectorRun(models.Model):
    """One collection attempt for one cluster-hour. Gaps in history show up here."""

    STATUS_OK = "ok"
    STATUS_ERROR = "error"

    cluster = models.ForeignKey(Cluster, null=True, blank=True, on_delete=models.CASCADE)
    hour = models.DateTimeField()
    source = models.CharField(max_length=32, default="thanos")
    started_at = models.DateTimeField(auto_now_add=True)
    duration_seconds = models.FloatField(default=0)
    status = models.CharField(max_length=16, default=STATUS_OK)
    message = models.TextField(blank=True)
    containers = models.IntegerField(default=0)
    nodes = models.IntegerField(default=0)
    namespaces = models.IntegerField(default=0)

    class Meta:
        ordering = ["-hour", "cluster__name"]


class Recommendation(models.Model):
    """Right-sizing result for one container, per aggressiveness profile and window.

    Recomputed daily; only the latest result is kept. `*_reclaimable` is the
    average amount of the resource (cores / bytes, all replicas together) that
    would be freed by applying the recommended request. Never negative: an
    under-requested container does not offset a padded one.
    """

    workload = models.ForeignKey(Workload, on_delete=models.CASCADE, related_name="recommendations")
    container = models.CharField(max_length=253)
    profile = models.CharField(max_length=32)
    window_days = models.IntegerField()
    computed_at = models.DateTimeField()
    hours_of_data = models.IntegerField(default=0)
    replicas_avg = models.FloatField(default=0)

    cpu_request_current = models.FloatField(default=0)
    cpu_limit_current = models.FloatField(null=True, blank=True)
    cpu_usage_avg = models.FloatField(default=0)
    cpu_request_recommended = models.FloatField(default=0)
    cpu_limit_recommended = models.FloatField(default=0)
    cpu_reclaimable = models.FloatField(default=0)

    mem_request_current = models.FloatField(default=0)
    mem_limit_current = models.FloatField(null=True, blank=True)
    mem_usage_avg = models.FloatField(default=0)
    mem_request_recommended = models.FloatField(default=0)
    mem_limit_recommended = models.FloatField(default=0)
    mem_reclaimable = models.FloatField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["workload", "container", "profile", "window_days"], name="uniq_recommendation"
            )
        ]
        indexes = [models.Index(fields=["profile", "window_days"])]
