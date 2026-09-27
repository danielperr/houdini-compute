# Houdini Compute — Product & Technical Spec (v0)

Status: agreed direction, Phase 0–1 in progress. Owner: DevOps (Compute Resource Governor).

## 1. Purpose

Give the organisation a clear, shared picture of OpenShift compute (CPU and
memory) so that it can **keep a healthy reserve and act on future surprises**,
by finding and reclaiming padded requests before asking for more capacity.

There is no hard ceiling today: VMware has spare cores and RAM, and nodes are
added through MachineSets. The goal is a **reserve policy** (default: 20% of
worker allocatable stays unrequested) and evidence-based decisions, not
emergency hardware buys.

Order of priorities: **observability first**, then conclusions, then efficiency.

## 2. Environment (as of 2026-09)

| Item | Value |
|---|---|
| Platform | Self-managed OpenShift 4.21 on VMware, air-gapped |
| Clusters | ~6; one is the most stressed (~50 workers, ~200 namespaces, thousands of pods) |
| Metrics | Own central Thanos ingesting all clusters, ~7 days retention; ACM installed |
| Confirmed metrics | `kube_namespace_labels` (tenant label), `kube_pod_owner`, `kube_resourcequota`, CPU throttling |
| Scarce resource | CPU, **by requests** (pods fail to schedule), not by usage |
| Quotas | `ResourceQuota` per namespace via GitOps, covering requests *and* limits |
| Policy | Requests/limits mandatory (enforced by quota). No LimitRanges, no policy engine |
| Deployments | GitOps with Helm + Argo CD |
| Workloads | Stateless + stateful services, Jobs, Postgres operators; few JVM apps; HPA rare |
| Nodes | MachineSets; a few worker shapes; taints are rare |
| SSO | Keycloak (federated from AD) |
| Out of scope | Storage, GPUs, OpenShift Virtualization, VMware-level metrics |
| Build | Images built outside, imported inside. Internal Nexus (npm) and PyPI mirror |

## 3. Users and what they decide

| Role | Decisions | Delivery |
|---|---|---|
| **Compute Resource Governor** (DevOps) | Approve/deny quota requests within SLA; weekly review of flagged workloads; when to add nodes | Portal: evidence pages, explorer, flag queue |
| **Dev team leads** | Which workloads to right-size and to what values | Portal + monthly team report via Mattermost webhook |
| **Platform team** (VMware / MachineSets, separate from DevOps) | Node shapes and counts per MachineSet category | Portal: nodes page, sizing recommendations |
| **Senior management** | How much to invest in performance work vs hardware | Monthly PDF via Mattermost webhook, linking to the portal |

Visibility: every logged-in user sees every tenant (for now). Only the
Governor Keycloak group can change settings, mappings and flags.

## 4. Tenant model

- **Team = tenant.** A namespace belongs to a team through its tenant label
  value, which is *not* necessarily the team name.
- **Group** is the organisational level above teams.
- Mapping `tenant_label → team → group` is seeded from a CSV
  (`tenant_label,team,group`) and edited by the Governor in the admin screens.
  Several label values may map to one team.
- Namespaces whose label value is not mapped appear as **Unmapped: <value>**
  so gaps are visible, never silently dropped.
- Platform namespaces (`openshift-*`, `kube-*`, `default`; configurable regex)
  are shown separately and excluded from team rankings.
- A team may span several clusters; cluster is a dimension, not a boundary.

## 5. Core metric: absolute waste, not ratios

Rank by **reclaimable resources**, so the biggest fish come first:

```
reclaimable = Σ over containers  max(0, current_request − recommended_request) × avg_replicas
```

- Computed **per container, floored at zero, then summed.** An
  under-requested container never hides a padded one in the same team.
- `recommended_request` depends on the chosen **aggressiveness profile**, so
  the ranking and the recommendations always agree. Changing the profile
  changes both.
- `avg_replicas` is **time-weighted** over the window: a CronJob that runs 5
  minutes a day holds its request 5 minutes a day.
- Efficiency (`used / requested`, capped at 100%) is shown as secondary context.
- Using more than requested is not "efficient"; it is under-reserving and a risk.

The future tenant score (and leaderboard) rewards efficiency only, built on
this same number.

## 6. Recommendations

### 6.1 Container requests and limits (for dev teams)

Computed daily from hourly data, per container, for each profile and window
(7 / 30 / 90 days):

| Profile | CPU request | CPU limit | Memory request = limit |
|---|---|---|---|
| Conservative | max of hourly p95 × 1.30 | peak × 1.50 | max of hourly peaks × 1.30 |
| Balanced (default) | p95 of hourly p95 × 1.15 | peak × 1.30 | max of hourly peaks × 1.15 |
| Aggressive | p90 of hourly p95 × 1.05 | peak × 1.15 | p99 of hourly peaks × 1.05 |

Floors: 10m CPU, 32 MiB memory. Values are rounded up (5m / 4 MiB). CPU
limit is never below the CPU request.

- Memory is sized from peaks because it cannot be compressed.
- Memory limit = request avoids overcommit-driven OOMs.
- CPU limits are kept because quotas require them. Dropping `limits.cpu` from
  quotas is a later policy discussion.
- Each recommendation carries **hours of data**. Until the tool has its own
  history, windows are short (Thanos keeps ~7 days) and monthly peaks are
  unseen. The UI marks low-coverage recommendations as low confidence.

### 6.2 Node sizing (for the platform team), Phase 3

- **Stranded capacity** per MachineSet category (shape): when workloads
  request a different CPU:memory ratio than the node shape, the leftover
  resource cannot be scheduled. Because CPU runs out first, expect memory
  stranded on nodes and a recommendation for more vCPU per GB.
- Node count needed to hold the reserve target, current vs after reclaiming.
- Trade-offs of bigger vs smaller nodes: per-node overhead, bin-packing,
  blast radius.

## 7. Risk detection (Phase 3)

Each rule has a configurable threshold and links to the workload:

- OOMKills; restarts / crash loops
- CPU throttling ratio above threshold (likely with mandatory CPU limits)
- Pods pending for lack of CPU (the stressed cluster's actual symptom)
- Memory usage above request (first to be evicted under node pressure)
- Usage close to the limit
- Request = limit padding pattern
- Reserve below target; runway shorter than N weeks

## 8. Screens

| Screen | Contents |
|---|---|
| **Overview** (done, demo data) | Per cluster: reserve vs target, runway, reclaimable (≈ nodes), capacity → quota → requests → usage, trend. Team ranking by reclaimable |
| **Explorer** | One page, all combinations: level (cluster / node / group / team / namespace / workload) × resource × metric (usage / requests / limits / quota / efficiency / reclaimable) × window × ranked-or-trend × absolute-or-% |
| **Team page** | Also the evidence link pasted into Jira quota tickets: quota vs requests vs usage (7/30/90 days), top workloads by reclaimable, risks, recommendations, suggested quota |
| **Workload page** | Usage vs request and limit over time, recommendation per profile, risk events, a ready-to-paste Helm values snippet |
| **Nodes** | Balance heatmap, stranded capacity per category, sizing recommendation |
| **Flag queue** | Weekly Governor review: open → acknowledged → team notified → resolved / exception (reason + expiry). Flags close automatically when the condition clears |
| **Admin** | Team mapping, settings, thresholds, collector runs |

## 9. Reports

- **Management (monthly):** PDF rendered from the same HTML templates as the
  portal, stored in the portal. A Mattermost webhook posts a summary plus a
  link to the PDF (webhooks cannot carry files).
  Content: reserve and runway per cluster, reclaimable trend, top teams and
  groups, month-over-month change.
- **Teams (monthly):** per-team summary via Mattermost webhook, linking to the
  team page.

## 10. Architecture

```
 OpenShift clusters ──metrics──▶ central Thanos (~7d)
                                     │  PromQL, read-only
                                     ▼
 ┌─────────────── Houdini Compute (one namespace, central cluster) ───────────────┐
 │ CronJob hourly   collect --backfill-hours 6   → Postgres (hourly facts)        │
 │ CronJob daily    compute_recommendations      → Postgres (recommendations)     │
 │ CronJob monthly  reports (PDF + webhook)                                        │
 │ Deployment       Django web (HTMX + ECharts) ◀── Keycloak OIDC                  │
 └─────────────────────────────────────────────────────────────────────────────────┘
```

- **Stack:** Python 3.11, Django 5.2, Postgres (existing Postgres operator),
  HTMX + ECharts vendored as static files (no JS build step), gunicorn +
  whitenoise. Scheduled work runs as Kubernetes CronJobs calling Django
  management commands, so there is no broker and no Celery.
- **Single data source:** Thanos only. No cluster API access is needed.
- **Collector:** hourly, idempotent, one run per cluster-hour recorded in
  `CollectorRun`. `--backfill-hours` recovers from outages shorter than
  Thanos' retention; longer gaps are lost for good, so collector health is
  monitored.
- **Storage** (per cluster, stressed cluster ~3k containers):
  - container hourly facts kept 90 days (~6.5M rows)
  - node and quota hourly facts kept 13 months (small)
  - daily team rollups kept 13 months for year-long trends (Phase 2)
- **Why not React:** the npm mirror makes it possible, but Django + HTMX is
  less code for a 4–6 person DevOps team. React can be added to the explorer
  alone if it outgrows HTMX.
- **Why our own recommendation math, not VPA/Kruize:** no extra components to
  run air-gapped, adjustable aggressiveness on the fly, and every number is
  explainable in one line to management.

### 10.1 Data model

- Dimensions: `Cluster`, `Group`, `Team`, `TenantLabel`, `Namespace`,
  `Workload` (Deployment / StatefulSet / DaemonSet / CronJob / Job / Pod),
  `Node` (roles, capacity, shape category).
- Facts, hourly:
  - `ContainerHourly`, per workload container:
    - replicas (time-weighted)
    - requests and limits
    - CPU avg / p95 / max
    - memory avg / max
    - throttling, restarts, OOMKills
  - `NodeHourly`: allocatable / requested / used
  - `NamespaceQuotaHourly`: hard / used
- Derived: `Recommendation` (per container × profile × window, latest only).

Units are always cores and bytes. Per-replica values plus replicas answer both
"how big is one pod" and "how much does the workload hold".

### 10.2 Security footprint (minimum necessary)

1. Read-only access to the central Thanos query endpoint (bearer token).
2. A Keycloak OIDC client.
3. Its own Postgres database.

No write access to any cluster.

## 11. Phases

| Phase | Scope | Status |
|---|---|---|
| 0: Data check | `tools/probe.py` against real Thanos: metric presence, cardinality, query timing, tenant-label coverage, node-name matching | Script ready, run inside the network |
| 1: Collector | Models, hourly collector, demo generator, Overview page, team mapping CSV + admin | **Skeleton done**. Remaining: Keycloak OIDC, deploy manifests, retention/pruning, collector health alert |
| 2: Observability | Explorer, team evidence pages, nodes page, daily team rollups | |
| 3: Risk & recommendations | Risk rules, workload page with recommendations, node sizing | |
| 4: Reports & workflow | Flag queue, monthly PDF + Mattermost webhooks | |
| Later | Leaderboard, Jira integration, suggested LimitRange defaults, what-if scenarios, operator packaging, ClusterResourceQuota per team if data shows many "move quota between my namespaces" tickets | |

## 12. Open items

- Run the probe on the stressed cluster; tune `compute/queries.py` from its report.
- Confirm the Governor Keycloak group name and the Mattermost webhook URLs.
- Pick the reserve target per cluster (default 20%).
- Where CPU runs out first is believed, not measured: the first week of data
  will confirm it.
