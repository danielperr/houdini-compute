"""PromQL used by the collector.

Every query is an *instant* query evaluated at the end of the collected hour
and summarises that hour with `[1h]` ranges or `[1h:5m]` subqueries.

Placeholders (plain text substitution, see `render`):
  $C             cluster matcher, e.g. cluster="prod-1"
  $CLUSTER       cluster label name (CLUSTER_LABEL)
  $TENANT        kube-state-metrics name of the tenant label, e.g. label_tenant
  $NODE          label that carries the node name on node-exporter metrics

Keep all PromQL here: the Phase 0 probe runs the same queries, and this is the
one place to adapt if a metric or label differs in your environment.
"""

import re

CTR = 'container!="",container!="POD"'
RUNNING = 'max by (namespace, pod) (kube_pod_status_phase{$C, phase="Running"}) == 1'
SCHEDULED = 'max by (namespace, pod) (kube_pod_status_phase{$C, phase=~"Running|Pending"}) == 1'
CPU_RATE_5M = f"sum by (namespace, pod, container) (rate(container_cpu_usage_seconds_total{{$C, {CTR}}}[5m]))"
NODE_CPU_5M = 'sum by ($NODE) (rate(node_cpu_seconds_total{$C, mode!~"idle|iowait|steal"}[5m]))'
NODE_MEM = "max by ($NODE) (node_memory_MemTotal_bytes{$C} - node_memory_MemAvailable_bytes{$C})"

CLUSTERS = "count by ($CLUSTER) (kube_node_info)"

NAMESPACE = {
    "labels": "max by (namespace, $TENANT) (max_over_time(kube_namespace_labels{$C}[1h]))",
    # Several quotas on one resource: the smallest hard limit is the effective one.
    "quota_hard": 'min by (namespace, resource) (max_over_time(kube_resourcequota{$C, type="hard"}[1h]))',
    "quota_used": 'max by (namespace, resource) (max_over_time(kube_resourcequota{$C, type="used"}[1h]))',
}

OWNERS = {
    "pod": "max by (namespace, pod, owner_kind, owner_name) (max_over_time(kube_pod_owner{$C}[1h]))",
    "replicaset": "max by (namespace, replicaset, owner_kind, owner_name) "
    "(max_over_time(kube_replicaset_owner{$C}[1h]))",
    "job": "max by (namespace, job_name, owner_kind, owner_name) (max_over_time(kube_job_owner{$C}[1h]))",
    # Time-weighted running pods per direct owner: 12 five-minute steps per hour.
    "replicas": "sum_over_time((count by (namespace, owner_kind, owner_name) "
    f"(kube_pod_owner{{$C}} * on (namespace, pod) group_left () ({RUNNING})))[1h:5m]) / 12",
}

CONTAINER = {
    "cpu_request": "max by (namespace, pod, container) "
    '(max_over_time(kube_pod_container_resource_requests{$C, resource="cpu"}[1h]))',
    "mem_request": "max by (namespace, pod, container) "
    '(max_over_time(kube_pod_container_resource_requests{$C, resource="memory"}[1h]))',
    "cpu_limit": "max by (namespace, pod, container) "
    '(max_over_time(kube_pod_container_resource_limits{$C, resource="cpu"}[1h]))',
    "mem_limit": "max by (namespace, pod, container) "
    '(max_over_time(kube_pod_container_resource_limits{$C, resource="memory"}[1h]))',
    "cpu_usage_avg": f"avg_over_time(({CPU_RATE_5M})[1h:5m])",
    "cpu_usage_p95": f"quantile_over_time(0.95, ({CPU_RATE_5M})[1h:5m])",
    "cpu_usage_max": f"max_over_time(({CPU_RATE_5M})[1h:5m])",
    "mem_usage_avg": "max by (namespace, pod, container) "
    f"(avg_over_time(container_memory_working_set_bytes{{$C, {CTR}}}[1h]))",
    "mem_usage_max": "max by (namespace, pod, container) "
    f"(max_over_time(container_memory_working_set_bytes{{$C, {CTR}}}[1h]))",
    "cpu_throttled_ratio": "sum by (namespace, pod, container) "
    f"(increase(container_cpu_cfs_throttled_periods_total{{$C, {CTR}}}[1h])) / "
    f"sum by (namespace, pod, container) (increase(container_cpu_cfs_periods_total{{$C, {CTR}}}[1h]))",
    "restarts": "max by (namespace, pod, container) (increase(kube_pod_container_status_restarts_total{$C}[1h]))",
    "oom_flag": "max by (namespace, pod, container) (max_over_time("
    'kube_pod_container_status_last_terminated_reason{$C, reason="OOMKilled"}[1h]))',
}

NODE = {
    "roles": "max by (node, role) (kube_node_role{$C})",
    "capacity": 'max by (node, resource) (kube_node_status_capacity{$C, resource=~"cpu|memory"})',
    "allocatable": "max by (node, resource) "
    '(max_over_time(kube_node_status_allocatable{$C, resource=~"cpu|memory"}[1h]))',
    "requested": "avg_over_time((sum by (node, resource) ("
    'kube_pod_container_resource_requests{$C, resource=~"cpu|memory"} '
    f"* on (namespace, pod) group_left () ({SCHEDULED})))[1h:5m])",
    "cpu_usage_avg": f"avg_over_time(({NODE_CPU_5M})[1h:5m])",
    "cpu_usage_max": f"max_over_time(({NODE_CPU_5M})[1h:5m])",
    "mem_usage_avg": f"avg_over_time(({NODE_MEM})[1h:5m])",
    "mem_usage_max": f"max_over_time(({NODE_MEM})[1h:5m])",
}


def ksm_label_name(label):
    """kube-state-metrics exposes namespace label `a.b/c-d` as `label_a_b_c_d`."""
    return "label_" + re.sub(r"[^a-zA-Z0-9_]", "_", label)


def cluster_matcher(cluster_label, cluster):
    escaped = cluster.replace("\\", "\\\\").replace('"', '\\"')
    return f'{cluster_label}="{escaped}"'


def render(template, cluster_matcher="", cluster_label="cluster", tenant_label="tenant", node_label="instance"):
    return (
        template.replace("$CLUSTER", cluster_label)
        .replace("$C", cluster_matcher)
        .replace("$TENANT", ksm_label_name(tenant_label))
        .replace("$NODE", node_label)
    )
