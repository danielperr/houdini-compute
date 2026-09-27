from datetime import datetime, timedelta, timezone

from django.test import SimpleTestCase, TestCase

from compute import queries as q
from compute.collector import Collector
from compute.ingest import write_snapshot
from compute.models import ContainerHourly, Namespace, Node, NodeHourly, Workload

HOUR = datetime(2026, 9, 1, 10, tzinfo=timezone.utc)
GIB = 1024**3


class FakeThanos:
    """Answers queries by exact rendered PromQL; anything else returns no data."""

    def __init__(self, cluster, responses):
        self.calls = []
        m = q.cluster_matcher("cluster", cluster)
        self.responses = {
            q.render(t, m, tenant_label="company.io/tenant", node_label="instance"): v for t, v in responses.items()
        }

    def query(self, promql, at):
        self.calls.append((promql, at))
        return self.responses.get(promql, [])


def pc(pod, container, value, ns="shop"):
    return ({"namespace": ns, "pod": pod, "container": container}, value)


RESPONSES = {
    q.NAMESPACE["labels"]: [({"namespace": "shop", "label_company_io_tenant": "t-shop"}, 1)],
    q.NAMESPACE["quota_hard"]: [
        ({"namespace": "shop", "resource": "requests.cpu"}, 10),
        ({"namespace": "shop", "resource": "cpu"}, 8),  # tighter alias wins
        ({"namespace": "shop", "resource": "limits.memory"}, 20 * GIB),
    ],
    q.NAMESPACE["quota_used"]: [({"namespace": "shop", "resource": "requests.cpu"}, 3)],
    q.OWNERS["pod"]: [
        ({"namespace": "shop", "pod": "web-abc-1", "owner_kind": "ReplicaSet", "owner_name": "web-abc"}, 1),
        ({"namespace": "shop", "pod": "web-abc-2", "owner_kind": "ReplicaSet", "owner_name": "web-abc"}, 1),
        ({"namespace": "shop", "pod": "nightly-123-x", "owner_kind": "Job", "owner_name": "nightly-123"}, 1),
        ({"namespace": "shop", "pod": "debug", "owner_kind": "<none>", "owner_name": "<none>"}, 1),
    ],
    q.OWNERS["replicaset"]: [
        ({"namespace": "shop", "replicaset": "web-abc", "owner_kind": "Deployment", "owner_name": "web"}, 1)
    ],
    q.OWNERS["job"]: [
        ({"namespace": "shop", "job_name": "nightly-123", "owner_kind": "CronJob", "owner_name": "nightly"}, 1)
    ],
    q.OWNERS["replicas"]: [
        ({"namespace": "shop", "owner_kind": "ReplicaSet", "owner_name": "web-abc"}, 2.0),
        ({"namespace": "shop", "owner_kind": "Job", "owner_name": "nightly-123"}, 0.25),
    ],
    q.CONTAINER["cpu_request"]: [
        pc("web-abc-1", "app", 1.0), pc("web-abc-2", "app", 1.0),
        pc("nightly-123-x", "job", 2.0), pc("debug", "shell", 0.1),
    ],
    q.CONTAINER["mem_request"]: [pc("web-abc-1", "app", GIB), pc("web-abc-2", "app", GIB)],
    q.CONTAINER["cpu_usage_avg"]: [pc("web-abc-1", "app", 0.2), pc("web-abc-2", "app", 0.4)],
    q.CONTAINER["cpu_usage_p95"]: [pc("web-abc-1", "app", 0.3), pc("web-abc-2", "app", 0.6)],
    q.CONTAINER["cpu_throttled_ratio"]: [pc("web-abc-1", "app", float("nan"))],
    q.CONTAINER["restarts"]: [pc("web-abc-2", "app", 2.0), pc("nightly-123-x", "job", 1.0)],
    q.CONTAINER["oom_flag"]: [pc("web-abc-2", "app", 1.0)],
    # Usage for a container without a spec entry (e.g. an init container) is ignored.
    q.CONTAINER["cpu_usage_max"]: [pc("web-abc-1", "init", 5.0), pc("web-abc-1", "app", 0.9)],
    q.NODE["roles"]: [({"node": "w1", "role": "worker"}, 1), ({"node": "m1", "role": "master"}, 1)],
    q.NODE["capacity"]: [
        ({"node": "w1", "resource": "cpu"}, 16), ({"node": "w1", "resource": "memory"}, 64 * GIB),
        ({"node": "m1", "resource": "cpu"}, 8), ({"node": "m1", "resource": "memory"}, 32 * GIB),
    ],
    q.NODE["allocatable"]: [({"node": "w1", "resource": "cpu"}, 15.5)],
    q.NODE["requested"]: [({"node": "w1", "resource": "cpu"}, 12.0)],
    q.NODE["cpu_usage_avg"]: [({"instance": "w1"}, 4.0), ({"instance": "unknown"}, 1.0)],
}


def collect(cluster="prod-1"):
    client = FakeThanos(cluster, RESPONSES)
    collector = Collector(client, cluster_label="cluster", tenant_label="company.io/tenant", node_label="instance")
    return collector.collect(cluster, HOUR), client


class CollectorTests(SimpleTestCase):
    def test_queries_are_evaluated_at_end_of_hour_and_scoped_to_cluster(self):
        _, client = collect()
        self.assertTrue(all(at == HOUR + timedelta(hours=1) for _, at in client.calls))
        self.assertTrue(all('cluster="prod-1"' in promql for promql, _ in client.calls))

    def test_workloads_resolve_to_top_level_owner(self):
        snap, _ = collect()
        by_key = {(c.workload_kind, c.workload_name, c.container): c for c in snap.containers}
        self.assertEqual(
            set(by_key), {("Deployment", "web", "app"), ("CronJob", "nightly", "job"), ("Pod", "debug", "shell")}
        )
        web = by_key[("Deployment", "web", "app")]
        self.assertEqual(web.replicas, 2.0)
        self.assertAlmostEqual(web.cpu_usage_avg, 0.3)  # mean of pods
        self.assertEqual(web.cpu_usage_p95, 0.6)  # max of pods
        self.assertEqual(web.cpu_usage_max, 0.9)
        self.assertIsNone(web.cpu_throttled_ratio)  # NaN dropped
        self.assertEqual(web.restarts, 2.0)
        self.assertEqual(web.oom_kills, 2.0)
        self.assertEqual(by_key[("CronJob", "nightly", "job")].replicas, 0.25)
        self.assertEqual(by_key[("CronJob", "nightly", "job")].oom_kills, 0)
        self.assertEqual(by_key[("Pod", "debug", "shell")].replicas, 1.0)

    def test_namespace_tenant_and_quota(self):
        snap, _ = collect()
        (ns,) = snap.namespaces
        self.assertEqual(ns.tenant_label, "t-shop")
        self.assertEqual(ns.quota["cpu_request_hard"], 8)
        self.assertEqual(ns.quota["cpu_request_used"], 3)
        self.assertEqual(ns.quota["mem_limit_hard"], 20 * GIB)

    def test_nodes(self):
        snap, _ = collect()
        nodes = {n.name: n for n in snap.nodes}
        self.assertEqual(set(nodes), {"w1", "m1"})
        self.assertEqual(nodes["w1"].cpu_usage_avg, 4.0)
        self.assertEqual(nodes["w1"].cpu_requested, 12.0)


class IngestTests(TestCase):
    def test_write_is_idempotent_and_classifies(self):
        snap, _ = collect()
        write_snapshot(snap)
        write_snapshot(snap)
        self.assertEqual(ContainerHourly.objects.count(), 3)
        self.assertEqual(Workload.objects.count(), 3)
        self.assertEqual(NodeHourly.objects.count(), 2)
        self.assertEqual(Namespace.objects.get(name="shop").tenant_label, "t-shop")
        self.assertTrue(Node.objects.get(name="w1").is_worker)
        self.assertFalse(Node.objects.get(name="m1").is_worker)
        self.assertEqual(Node.objects.get(name="w1").category, "16 vCPU / 64 GiB")


class QueryRenderTests(SimpleTestCase):
    def test_placeholders(self):
        rendered = q.render(q.CLUSTERS, cluster_label="ocp_cluster")
        self.assertEqual(rendered, "count by (ocp_cluster) (kube_node_info)")
        self.assertEqual(q.ksm_label_name("company.io/tenant-id"), "label_company_io_tenant_id")
        self.assertEqual(q.cluster_matcher("cluster", 'a"b'), 'cluster="a\\"b"')

    def test_every_template_renders_without_leftover_placeholders(self):
        m = q.cluster_matcher("cluster", "x")
        for group in (q.NAMESPACE, q.OWNERS, q.CONTAINER, q.NODE):
            for template in group.values():
                self.assertNotIn("$", q.render(template, m))
