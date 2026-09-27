from django.test import SimpleTestCase

from compute.recommend import MIB, MIN_CPU, MIN_MEM, PROFILES, HourStat, quantile, recommend


def stat(cpu_p95=0.1, cpu_max=0.2, mem_max=100 * MIB, replicas=1.0, cpu_request=1.0, mem_request=512 * MIB):
    return HourStat(
        replicas=replicas, cpu_request=cpu_request, cpu_limit=cpu_request, cpu_usage_avg=cpu_p95 / 2,
        cpu_usage_p95=cpu_p95, cpu_usage_max=cpu_max, mem_request=mem_request, mem_limit=mem_request,
        mem_usage_avg=mem_max * 0.9, mem_usage_max=mem_max,
    )


class QuantileTests(SimpleTestCase):
    def test_matches_linear_interpolation(self):
        self.assertEqual(quantile([1, 2, 3, 4, 5], 0.5), 3)
        self.assertAlmostEqual(quantile([0, 10], 0.95), 9.5)
        self.assertEqual(quantile([], 0.9), 0.0)


class RecommendTests(SimpleTestCase):
    def test_padded_container_is_reclaimable(self):
        stats = [stat(cpu_p95=0.2, replicas=3) for _ in range(24)]
        r = recommend(stats, PROFILES["balanced"], window_hours=24)
        self.assertAlmostEqual(r.cpu_request_recommended, 0.23)  # 0.2 * 1.15
        self.assertAlmostEqual(r.cpu_reclaimable, (1.0 - 0.23) * 3)
        self.assertGreaterEqual(r.cpu_limit_recommended, r.cpu_request_recommended)
        self.assertEqual(r.mem_limit_recommended, r.mem_request_recommended)

    def test_under_requested_container_never_offsets_waste(self):
        stats = [stat(cpu_p95=2.0, cpu_max=3.0, mem_max=900 * MIB) for _ in range(24)]
        r = recommend(stats, PROFILES["balanced"], window_hours=24)
        self.assertGreater(r.cpu_request_recommended, r.cpu_request_current)
        self.assertEqual(r.cpu_reclaimable, 0)
        self.assertEqual(r.mem_reclaimable, 0)

    def test_floors_apply_to_idle_containers(self):
        stats = [stat(cpu_p95=0.0001, cpu_max=0.0001, mem_max=1 * MIB) for _ in range(24)]
        r = recommend(stats, PROFILES["aggressive"], window_hours=24)
        self.assertEqual(r.cpu_request_recommended, MIN_CPU)
        self.assertEqual(r.mem_request_recommended, MIN_MEM)

    def test_short_lived_jobs_are_time_weighted(self):
        # Runs half of one hour per day for 7 days: holds its request ~0.3% of the time.
        stats = [stat(replicas=0.5) for _ in range(7)]
        r = recommend(stats, PROFILES["balanced"], window_hours=7 * 24)
        self.assertAlmostEqual(r.replicas_avg, 3.5 / 168)
        self.assertLess(r.cpu_reclaimable, 0.02)

    def test_profiles_are_ordered_by_risk(self):
        stats = [stat(cpu_p95=0.1 + i * 0.01, mem_max=(100 + i) * MIB) for i in range(48)]
        recs = {name: recommend(stats, p, 48) for name, p in PROFILES.items()}
        self.assertGreater(recs["conservative"].cpu_request_recommended, recs["balanced"].cpu_request_recommended)
        self.assertGreater(recs["balanced"].cpu_request_recommended, recs["aggressive"].cpu_request_recommended)
        self.assertGreaterEqual(recs["conservative"].mem_request_recommended, recs["aggressive"].mem_request_recommended)

    def test_no_data_means_no_recommendation(self):
        self.assertIsNone(recommend([], PROFILES["balanced"], 24))
        self.assertIsNone(recommend([stat(replicas=0)], PROFILES["balanced"], 24))
