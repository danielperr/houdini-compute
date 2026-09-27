"""Right-sizing math. Pure functions, no database access.

A profile says how much risk to accept:
  * CPU request   = quantile of the hourly p95 CPU usage over the window x headroom
  * CPU limit     = peak CPU usage over the window x limit headroom (never below the request)
  * Memory request = quantile of the hourly *peak* working set x headroom
                    (memory is not compressible, so we size from peaks)
  * Memory limit  = memory request (keeps the pod out of overcommit OOMs)

Reclaimable = max(0, current request - recommended request) x average replicas.
"""

import math
from dataclasses import dataclass

MIB = 1024**2
MIN_CPU = 0.01  # 10m
MIN_MEM = 32 * MIB
CPU_STEP = 0.005  # round up to 5m
MEM_STEP = 4 * MIB  # round up to 4Mi
# Below this share of the window covered by data, a recommendation is "low confidence".
LOW_CONFIDENCE_COVERAGE = 0.5


@dataclass(frozen=True)
class Profile:
    name: str
    label: str
    cpu_quantile: float
    cpu_headroom: float
    cpu_limit_headroom: float
    mem_quantile: float
    mem_headroom: float


PROFILES = {
    p.name: p
    for p in [
        Profile("conservative", "Conservative", 1.00, 1.30, 1.50, 1.00, 1.30),
        Profile("balanced", "Balanced", 0.95, 1.15, 1.30, 1.00, 1.15),
        Profile("aggressive", "Aggressive", 0.90, 1.05, 1.15, 0.99, 1.05),
    ]
}
DEFAULT_PROFILE = "balanced"
WINDOWS = (7, 30, 90)
DEFAULT_WINDOW = 7


@dataclass
class HourStat:
    replicas: float
    cpu_request: float
    cpu_limit: float | None
    cpu_usage_avg: float
    cpu_usage_p95: float
    cpu_usage_max: float
    mem_request: float
    mem_limit: float | None
    mem_usage_avg: float
    mem_usage_max: float


@dataclass
class Result:
    hours_of_data: int
    replicas_avg: float
    cpu_request_current: float
    cpu_limit_current: float | None
    cpu_usage_avg: float
    cpu_request_recommended: float
    cpu_limit_recommended: float
    cpu_reclaimable: float
    mem_request_current: float
    mem_limit_current: float | None
    mem_usage_avg: float
    mem_request_recommended: float
    mem_limit_recommended: float
    mem_reclaimable: float


def quantile(values, q):
    """Linear-interpolated quantile (same as numpy's default)."""
    if not values:
        return 0.0
    s = sorted(values)
    pos = (len(s) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def round_up(value, step, minimum):
    return max(minimum, math.ceil(value / step - 1e-9) * step)


def recommend(stats, profile: Profile, window_hours: int) -> Result | None:
    """`stats` are the hourly rows of one container, oldest first.

    `window_hours` is how many hours of the window the cluster was actually
    collected; hours without a row for this container count as zero replicas
    (a CronJob that runs 5 minutes a day holds its requests 5 minutes a day).
    """
    stats = [s for s in stats if s.replicas > 0]
    if not stats or window_hours <= 0:
        return None
    latest = stats[-1]
    replicas_avg = sum(s.replicas for s in stats) / window_hours

    cpu_target = quantile([s.cpu_usage_p95 for s in stats], profile.cpu_quantile)
    cpu_req = round_up(cpu_target * profile.cpu_headroom, CPU_STEP, MIN_CPU)
    cpu_peak = max(s.cpu_usage_max for s in stats)
    cpu_lim = max(cpu_req, round_up(cpu_peak * profile.cpu_limit_headroom, CPU_STEP, MIN_CPU))

    mem_target = quantile([s.mem_usage_max for s in stats], profile.mem_quantile)
    mem_req = round_up(mem_target * profile.mem_headroom, MEM_STEP, MIN_MEM)

    def weighted_avg(attr):
        total = sum(s.replicas for s in stats)
        return sum(getattr(s, attr) * s.replicas for s in stats) / total

    return Result(
        hours_of_data=len(stats),
        replicas_avg=replicas_avg,
        cpu_request_current=latest.cpu_request,
        cpu_limit_current=latest.cpu_limit,
        cpu_usage_avg=weighted_avg("cpu_usage_avg"),
        cpu_request_recommended=cpu_req,
        cpu_limit_recommended=cpu_lim,
        cpu_reclaimable=max(0.0, latest.cpu_request - cpu_req) * replicas_avg,
        mem_request_current=latest.mem_request,
        mem_limit_current=latest.mem_limit,
        mem_usage_avg=weighted_avg("mem_usage_avg"),
        mem_request_recommended=mem_req,
        mem_limit_recommended=mem_req,
        mem_reclaimable=max(0.0, latest.mem_request - mem_req) * replicas_avg,
    )
