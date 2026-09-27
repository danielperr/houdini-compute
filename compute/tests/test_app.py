import tempfile
from datetime import datetime, timezone

from django.test import TestCase

from compute import demo
from compute.jobs import compute_recommendations
from compute.models import Recommendation, Team, TenantLabel
from compute.overview import overview
from compute.teams import MappingError, import_csv

NOW = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)


class TeamImportTests(TestCase):
    def write(self, text):
        f = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False)
        f.write(text)
        f.close()
        return f.name

    def test_import_and_reimport(self):
        path = self.write("tenant_label,team,group\nt-a,Alpha,G1\nt-a2,Alpha,G1\nt-b,Beta,\n")
        self.assertEqual(import_csv(path), (3, 0))
        self.assertEqual(Team.objects.get(name="Alpha").tenant_labels.count(), 2)
        self.assertIsNone(Team.objects.get(name="Beta").group)
        path = self.write("tenant_label,team,group\nt-b,Alpha,G2\n")
        self.assertEqual(import_csv(path), (0, 1))
        self.assertEqual(TenantLabel.objects.get(value="t-b").team.name, "Alpha")
        self.assertEqual(Team.objects.get(name="Alpha").group.name, "G2")

    def test_rejects_missing_columns(self):
        with self.assertRaises(MappingError):
            import_csv(self.write("label,team\nx,y\n"))


class DemoAndOverviewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        demo.generate(days=3, seed=1, now=NOW, clusters=["prod-west"], log=lambda *_: None)
        compute_recommendations(now=NOW)

    def test_recommendations_exist_for_all_profiles(self):
        self.assertEqual(
            set(Recommendation.objects.values_list("profile", flat=True)),
            {"conservative", "balanced", "aggressive"},
        )

    def test_overview_numbers_are_consistent(self):
        data = overview("cpu", "balanced", 7)
        (s,) = data["summaries"]
        self.assertAlmostEqual(s.requested, s.requested_tenant + s.requested_platform, places=1)
        self.assertGreater(s.allocatable, s.requested)
        self.assertGreater(s.reclaimable, 0)
        teams = [t for t in data["teams"] if t.kind != "platform"]
        self.assertEqual([t.reclaimable for t in teams], sorted((t.reclaimable for t in teams), reverse=True))
        self.assertTrue(any(t.kind == "unmapped" for t in teams))
        self.assertEqual(data["teams"][-1].kind, "platform")

    def test_aggressive_reclaims_more_than_conservative(self):
        agg = overview("cpu", "aggressive", 7)["summaries"][0].reclaimable
        con = overview("cpu", "conservative", 7)["summaries"][0].reclaimable
        self.assertGreater(agg, con)

    def test_page_renders_full_and_partial(self):
        r = self.client.get("/", {"resource": "mem", "window": "30", "profile": "aggressive"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "prod-west")
        self.assertContains(r, "<html")
        r = self.client.get("/", {"resource": "bogus", "window": "x"}, HTTP_HX_REQUEST="true")
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "<html")
        self.assertContains(r, "Teams by reclaimable CPU")

    def test_empty_database_renders(self):
        Recommendation.objects.all().delete()
        from compute.models import Cluster

        Cluster.objects.all().delete()
        r = self.client.get("/")
        self.assertContains(r, "No data yet")

    def test_healthz(self):
        self.assertEqual(self.client.get("/healthz").content, b"ok")
