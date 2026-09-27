#!/usr/bin/env python3
"""Phase 0 probe: check that Thanos has everything Houdini Compute needs.

Runs the collector's own PromQL (compute/queries.py) against your Thanos for
one hour, and reports per query: time taken, series returned, and label keys.
It also checks tenant-label coverage, node-name matching and ownership
resolution. By default the report contains no namespace, pod or node names,
so it can be shared outside the network.

Standard library only. Usage:

  python3 tools/probe.py --url https://thanos-querier.example:9091 \
      --token-file /var/run/secrets/kubernetes.io/serviceaccount/token \
      --tenant-label tenant [--cluster prod-east] [--ca-file ca.crt | --insecure]
"""

import argparse
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from compute import queries as q  # noqa: E402
from compute.thanos import ThanosClient, ThanosError  # noqa: E402

REQUIRED_METRICS = [
    "kube_node_info", "kube_node_role", "kube_node_status_capacity", "kube_node_status_allocatable",
    "kube_namespace_labels", "kube_resourcequota", "kube_pod_owner", "kube_replicaset_owner",
    "kube_job_owner", "kube_pod_status_phase", "kube_pod_container_resource_requests",
    "kube_pod_container_resource_limits", "kube_pod_container_status_restarts_total",
    "kube_pod_container_status_last_terminated_reason", "container_cpu_usage_seconds_total",
    "container_memory_working_set_bytes", "container_cpu_cfs_throttled_periods_total",
    "container_cpu_cfs_periods_total", "node_cpu_seconds_total", "node_memory_MemTotal_bytes",
    "node_memory_MemAvailable_bytes",
]


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", required=True, help="Thanos Querier base URL")
    p.add_argument("--token-file", help="File containing a bearer token")
    p.add_argument("--ca-file", help="CA bundle for TLS verification")
    p.add_argument("--insecure", action="store_true", help="Skip TLS verification")
    p.add_argument("--cluster-label", default="cluster")
    p.add_argument("--tenant-label", default="tenant", help="Namespace label that holds the tenant")
    p.add_argument("--node-label", default="instance", help="Label with the node name on node-exporter metrics")
    p.add_argument("--cluster", action="append", help="Cluster(s) to probe (default: all found)")
    p.add_argument("--hours-ago", type=int, default=1, help="Which past hour to probe (default: last full hour)")
    p.add_argument("--show-names", action="store_true", help="Include real names in the report")
    args = p.parse_args()

    token = Path(args.token_file).read_text().strip() if args.token_file else ""
    client = ThanosClient(args.url, token=token, ca_bundle=args.ca_file or "", verify_tls=not args.insecure)
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    hour = now - timedelta(hours=args.hours_ago)
    end = hour + timedelta(hours=1)

    def render(template, cluster=""):
        matcher = q.cluster_matcher(args.cluster_label, cluster) if cluster else ""
        return q.render(template, matcher, args.cluster_label, args.tenant_label, args.node_label)

    def timed(promql):
        t0 = time.monotonic()
        try:
            rows = client.query(promql, end)
            return rows, time.monotonic() - t0, None
        except ThanosError as e:
            return [], time.monotonic() - t0, str(e)

    out = [f"# Houdini Compute probe\n\nThanos: `{args.url}` · hour probed: {hour:%Y-%m-%d %H:00} UTC\n"]

    rows, secs, err = timed(render(q.CLUSTERS))
    if err:
        print(f"Cannot list clusters: {err}", file=sys.stderr)
        return 2
    clusters = sorted(r[0].get(args.cluster_label, "") for r in rows)
    out.append(f"## Clusters\n\n{len(clusters)} found by `{args.cluster_label}` label ({secs:.1f}s)")
    if args.show_names:
        out.append(": " + ", ".join(clusters))
    out.append("\n")

    names = "|".join(REQUIRED_METRICS)
    rows, _, err = timed(f'count by (__name__) ({{__name__=~"{names}"}})')
    present = {r[0].get("__name__"): int(r[1]) for r in rows}
    out.append("## Metrics present (all clusters)\n\n| Metric | Series |\n|---|---|")
    for m in REQUIRED_METRICS:
        out.append(f"| `{m}` | {present.get(m, '**MISSING**')} |")
    if err:
        out.append(f"\n(query failed: {err})")
    out.append("")

    targets = args.cluster or clusters
    for i, cluster in enumerate(targets):
        label = cluster if args.show_names else f"cluster #{i + 1}"
        out.append(f"## {label}\n\n| Query | Seconds | Series | Label keys |\n|---|---|---|---|")
        results = {}
        total = 0.0
        for group_name, group in (("namespace", q.NAMESPACE), ("owners", q.OWNERS),
                                  ("container", q.CONTAINER), ("node", q.NODE)):
            for key, template in group.items():
                rows, secs, err = timed(render(template, cluster))
                total += secs
                results[f"{group_name}.{key}"] = rows
                keys = sorted({k for labels, _ in rows for k in labels})
                status = f"ERROR: {err[:120]}" if err else ", ".join(keys)
                out.append(f"| {group_name}.{key} | {secs:.1f} | {len(rows)} | {status} |")
        out.append(f"\nTotal query time for one hour: **{total:.0f}s**\n")
        out.extend(checks(results, args))
    print("\n".join(out))
    return 0


def checks(results, args):
    lines = ["### Checks\n"]
    tenant_key = q.ksm_label_name(args.tenant_label)
    ns_rows = results["namespace.labels"]
    with_tenant = sum(1 for labels, _ in ns_rows if labels.get(tenant_key))
    lines.append(f"- Namespaces with a `{args.tenant_label}` label: {with_tenant} of {len(ns_rows)}")
    values = Counter(labels.get(tenant_key) for labels, _ in ns_rows if labels.get(tenant_key))
    lines.append(f"- Distinct tenant label values: {len(values)}")

    kube_nodes = {labels.get("node") for labels, _ in results["node.capacity"]}
    exporter_nodes = {labels.get(args.node_label) for labels, _ in results["node.cpu_usage_avg"]}
    matched = len(kube_nodes & exporter_nodes)
    lines.append(
        f"- Node-exporter `{args.node_label}` matches kube node names: {matched} of {len(kube_nodes)}"
        + ("" if matched == len(kube_nodes) else "  **← set --node-label / NODE_EXPORTER_NODE_LABEL**")
    )

    kinds = Counter(labels.get("owner_kind") for labels, _ in results["owners.pod"])
    lines.append("- Pod owner kinds: " + ", ".join(f"{k}={v}" for k, v in kinds.most_common()))
    rs = len(results["owners.replicaset"])
    lines.append(f"- ReplicaSet owner series: {rs}; Job owner series: {len(results['owners.job'])}")

    req = {(l.get("namespace"), l.get("pod"), l.get("container")) for l, _ in results["container.cpu_request"]}
    use = {(l.get("namespace"), l.get("pod"), l.get("container")) for l, _ in results["container.cpu_usage_avg"]}
    lines.append(f"- Containers with a CPU request: {len(req)}; with CPU usage: {len(use)}; both: {len(req & use)}")
    no_req = len(use - req)
    if no_req:
        lines.append(f"  - {no_req} containers use CPU but have no request series (init containers or missing KSM data)")
    thr = len(results["container.cpu_throttled_ratio"])
    lines.append(f"- Containers with throttling data: {thr}")
    lines.append("")
    return lines


if __name__ == "__main__":
    sys.exit(main())
