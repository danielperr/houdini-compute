# Houdini Compute

Compute observability and right-sizing for multi-tenant OpenShift. It shows
where CPU and memory are requested but not used, ranks teams by how much they
could give back, and keeps an eye on the reserve.

See [docs/SPEC.md](docs/SPEC.md) for the product and technical spec.

## Run locally with demo data

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python manage.py migrate
.venv/bin/python manage.py generate_demo_data --days 14   # ~2 minutes
.venv/bin/python manage.py createsuperuser                # for /admin
.venv/bin/python manage.py runserver
```

Open http://127.0.0.1:8000/. The demo contains three clusters (`prod-east`
is the stressed one), 24 teams in 4 groups, one unmapped tenant and a mix of
padded, idle, bursty, under-requested, CronJob and Postgres workloads.

Run the tests with `.venv/bin/python manage.py test`.

## Against a real Thanos

1. **Probe first (Phase 0).** Run this inside the network; the report has no
   names unless you pass `--show-names`:

   ```bash
   python3 tools/probe.py --url https://<thanos-querier> --token-file token \
       --tenant-label <your-namespace-label> --cluster <stressed-cluster> > probe.md
   ```

   The probe only needs the Python standard library.

2. **Configure** through environment variables:

   | Variable | Meaning | Default |
   |---|---|---|
   | `THANOS_URL` | Thanos Querier base URL | `http://localhost:9090` |
   | `THANOS_TOKEN` | Bearer token (read-only) | – |
   | `THANOS_CA_BUNDLE` / `THANOS_VERIFY_TLS` | TLS settings | system CAs / `true` |
   | `CLUSTER_LABEL` | Label that tells clusters apart | `cluster` |
   | `TENANT_LABEL` | Namespace label holding the tenant | `tenant` |
   | `NODE_EXPORTER_NODE_LABEL` | Label with the node name on node-exporter metrics | `instance` |
   | `PLATFORM_NAMESPACE_REGEX` | Namespaces shown as platform | `^(openshift\|kube)(-.*)?$\|^default$` |
   | `TARGET_RESERVE` | Share of worker allocatable to keep unrequested | `0.20` |
   | `POSTGRES_HOST` / `_PORT` / `_DB` / `_USER` / `_PASSWORD` | Postgres (SQLite when unset) | – |
   | `DJANGO_SECRET_KEY`, `DJANGO_DEBUG`, `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS` | Django | dev values |

3. **Load the team mapping**: `python manage.py import_teams teams.csv`
   (columns `tenant_label,team,group`, see `examples/teams.csv`). After that,
   edit it in `/admin`.

4. **Schedule**, using the same image for everything:

   | Command | When |
   |---|---|
   | `python manage.py collect --backfill-hours 6` | Hourly, a few minutes past the hour |
   | `python manage.py compute_recommendations` | Daily |

   `collect` skips hours that already succeeded, so the backfill self-heals
   short outages. Thanos keeps about 7 days: longer gaps are lost.

## Layout

| Path | What |
|---|---|
| `compute/queries.py` | All PromQL, in one place to adapt to your environment |
| `compute/collector.py` | Thanos → `HourSnapshot` (pods resolved to workloads) |
| `compute/ingest.py` | `HourSnapshot` → database (idempotent) |
| `compute/recommend.py` | Right-sizing math and aggressiveness profiles |
| `compute/jobs.py` | Daily recommendation job |
| `compute/overview.py` | Numbers behind the Overview page |
| `compute/demo.py` | Synthetic data generator |
| `tools/probe.py` | Phase 0 probe |

## Not done yet

- Keycloak login: every page is currently open.
- OpenShift manifests (Deployment, Route, CronJobs).
- Retention pruning and daily rollups.
- The Explorer, team, workload and nodes pages, risk rules, reports.

See the phases in the spec.
