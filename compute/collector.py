"""Turn one hour of Thanos data into `HourSnapshot`s (one per cluster)."""

import math
from collections import defaultdict
from datetime import timedelta

from django.conf import settings

from . import queries as q
from .ingest import ContainerSample, HourSnapshot, NamespaceSample, NodeSample

QUOTA_RESOURCES = {
    "requests.cpu": "cpu_request",
    "cpu": "cpu_request",
    "limits.cpu": "cpu_limit",
    "requests.memory": "mem_request",
    "memory": "mem_request",
    "limits.memory": "mem_limit",
}


class Collector:
    def __init__(self, client, cluster_label=None, tenant_label=None, node_label=None):
        self.client = client
        self.cluster_label = cluster_label or settings.CLUSTER_LABEL
        self.tenant_label = tenant_label or settings.TENANT_LABEL
        self.node_label = node_label or settings.NODE_EXPORTER_NODE_LABEL

    # -- plumbing ---------------------------------------------------------

    def _query(self, template, at, cluster=""):
        matcher = q.cluster_matcher(self.cluster_label, cluster) if cluster else ""
        promql = q.render(
            template,
            cluster_matcher=matcher,
            cluster_label=self.cluster_label,
            tenant_label=self.tenant_label,
            node_label=self.node_label,
        )
        return [(labels, v) for labels, v in self.client.query(promql, at) if math.isfinite(v)]

    def clusters(self, hour):
        end = hour + timedelta(hours=1)
        rows = self._query(q.CLUSTERS, end)
        return sorted(labels[self.cluster_label] for labels, _ in rows if labels.get(self.cluster_label))

    # -- collection -------------------------------------------------------

    def collect(self, cluster, hour):
        end = hour + timedelta(hours=1)
        return HourSnapshot(
            cluster=cluster,
            hour=hour,
            namespaces=self._namespaces(cluster, end),
            nodes=self._nodes(cluster, end),
            containers=self._containers(cluster, end),
        )

    def _namespaces(self, cluster, end):
        tenant_key = q.ksm_label_name(self.tenant_label)
        result = {}
        for labels, _ in self._query(q.NAMESPACE["labels"], end, cluster):
            ns = labels.get("namespace")
            if ns:
                result[ns] = NamespaceSample(name=ns, tenant_label=labels.get(tenant_key, ""))
        for kind in ("hard", "used"):
            for labels, v in self._query(q.NAMESPACE[f"quota_{kind}"], end, cluster):
                field = QUOTA_RESOURCES.get(labels.get("resource"))
                ns = labels.get("namespace")
                if not field or not ns:
                    continue
                sample = result.setdefault(ns, NamespaceSample(name=ns))
                key = f"{field}_{kind}"
                # "cpu" and "requests.cpu" can both be set; keep the tighter hard limit.
                if kind == "hard" and key in sample.quota:
                    v = min(v, sample.quota[key])
                sample.quota[key] = v
        return list(result.values())

    def _workload_resolver(self, cluster, end):
        """Map (namespace, pod) -> (kind, name) of the top-level workload."""
        rs_owner = {
            (l["namespace"], l["replicaset"]): (l["owner_kind"], l["owner_name"])
            for l, _ in self._query(q.OWNERS["replicaset"], end, cluster)
            if l.get("owner_kind") not in (None, "", "<none>")
        }
        job_owner = {
            (l["namespace"], l["job_name"]): (l["owner_kind"], l["owner_name"])
            for l, _ in self._query(q.OWNERS["job"], end, cluster)
            if l.get("owner_kind") not in (None, "", "<none>")
        }

        def top_level(ns, kind, name):
            if kind == "ReplicaSet":
                return rs_owner.get((ns, name), (kind, name))
            if kind == "Job":
                return job_owner.get((ns, name), (kind, name))
            return kind, name

        pods = {}
        for l, _ in self._query(q.OWNERS["pod"], end, cluster):
            ns, pod, kind, name = l.get("namespace"), l.get("pod"), l.get("owner_kind"), l.get("owner_name")
            if kind in (None, "", "<none>", "Node"):
                pods[(ns, pod)] = ("Pod", pod)
            else:
                pods[(ns, pod)] = top_level(ns, kind, name)

        replicas = defaultdict(float)
        for l, v in self._query(q.OWNERS["replicas"], end, cluster):
            ns, kind, name = l.get("namespace"), l.get("owner_kind"), l.get("owner_name")
            if kind in (None, "", "<none>", "Node"):
                continue
            replicas[(ns, *top_level(ns, kind, name))] += v
        return pods, replicas

    def _containers(self, cluster, end):
        pods, replicas = self._workload_resolver(cluster, end)
        per_pod = defaultdict(dict)  # (ns, pod, container) -> metric -> value
        for metric, template in q.CONTAINER.items():
            for l, v in self._query(template, end, cluster):
                key = (l.get("namespace"), l.get("pod"), l.get("container"))
                if all(key):
                    per_pod[key][metric] = v

        grouped = defaultdict(list)  # (ns, kind, name, container) -> [metrics per pod]
        for (ns, pod, container), m in per_pod.items():
            if "cpu_request" not in m and "mem_request" not in m:
                continue  # init/sidecar noise without a spec entry
            kind, name = pods.get((ns, pod), ("Pod", pod))
            grouped[(ns, kind, name, container)].append(m)

        samples = []
        for (ns, kind, name, container), rows in grouped.items():
            ooms = sum(r.get("restarts", 0) for r in rows if r.get("oom_flag", 0) >= 1)
            pod_count = len(rows)
            samples.append(
                ContainerSample(
                    namespace=ns,
                    workload_kind=kind,
                    workload_name=name,
                    container=container,
                    # Bare pods have no owner series; count them as present.
                    replicas=replicas.get((ns, kind, name), float(pod_count) if kind == "Pod" else 0.0),
                    cpu_request=_max(rows, "cpu_request", 0.0),
                    cpu_limit=_max(rows, "cpu_limit", None),
                    cpu_usage_avg=_mean(rows, "cpu_usage_avg"),
                    cpu_usage_p95=_max(rows, "cpu_usage_p95", 0.0),
                    cpu_usage_max=_max(rows, "cpu_usage_max", 0.0),
                    cpu_throttled_ratio=_mean(rows, "cpu_throttled_ratio", None),
                    mem_request=_max(rows, "mem_request", 0.0),
                    mem_limit=_max(rows, "mem_limit", None),
                    mem_usage_avg=_mean(rows, "mem_usage_avg"),
                    mem_usage_max=_max(rows, "mem_usage_max", 0.0),
                    restarts=sum(r.get("restarts", 0) for r in rows),
                    oom_kills=ooms,
                )
            )
        return samples

    def _nodes(self, cluster, end):
        nodes = {}

        def node(name):
            return nodes.setdefault(name, NodeSample(name=name))

        for l, _ in self._query(q.NODE["roles"], end, cluster):
            if l.get("node"):
                node(l["node"]).roles.append(l.get("role", ""))
        for metric in ("capacity", "allocatable", "requested"):
            for l, v in self._query(q.NODE[metric], end, cluster):
                name, res = l.get("node"), l.get("resource")
                if not name or res not in ("cpu", "memory"):
                    continue
                prefix = "cpu" if res == "cpu" else "mem"
                setattr(node(name), f"{prefix}_{metric}", v)
        for metric in ("cpu_usage_avg", "cpu_usage_max", "mem_usage_avg", "mem_usage_max"):
            for l, v in self._query(q.NODE[metric], end, cluster):
                name = l.get(self.node_label)
                if name in nodes:  # ignore exporters we can't tie to a known node
                    setattr(nodes[name], metric, v)
        # Nodes that only showed up in usage metrics or lack capacity are not real nodes.
        return [n for n in nodes.values() if n.cpu_capacity or n.cpu_allocatable]


def _max(rows, key, default):
    values = [r[key] for r in rows if key in r]
    return max(values) if values else default


def _mean(rows, key, default=0.0):
    values = [r[key] for r in rows if key in r]
    return sum(values) / len(values) if values else default
