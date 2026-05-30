"""
tests/test_operator_weight.py

Reproducible checks behind the "Weight sensitivity" section of ARCHITECTURE.md.

Two complementary facts are asserted:

  1. INVARIANCE — on the five supplied scenarios, sweeping any soft-rule weight
     leaves the schedule unchanged (the measured property documented in §2–§3).

  2. THE MECHANISM IS CORRECT — on a minimal scenario with genuine flexibility
     (a bus with two cost-optimal plans of different wait/trip), raising the
     operator weight DOES change the selected plan. This proves the weighting
     machinery is live; the supplied scenarios simply never present that case.

Run with pytest:   pytest tests/
or directly:       python tests/test_operator_weight.py
"""

from __future__ import annotations

import copy
import glob
import os
import sys

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scheduler import run_scheduler  # noqa: E402

SCENARIO_GLOB = os.path.join(os.path.dirname(__file__), "..", "scenarios", "scenario_*.yaml")


def _fingerprint(timelines):
    """Full schedule identity: plan, arrival and wait per bus."""
    return tuple(
        (
            tl.bus.id,
            "+".join(s.station for s in tl.charging_stops),
            round(tl.arrival_min, 3),
            round(tl.total_wait_min, 3),
        )
        for tl in sorted(timelines, key=lambda t: t.bus.id)
    )


def _plan_of(timelines, bus_id):
    tl = next(t for t in timelines if t.bus.id == bus_id)
    return "+".join(s.station for s in tl.charging_stops)


# A minimal scenario engineered to give one bus a real choice:
#   Route O-90-P-90-M-90-Q-90-Z (360 km total), 240 km range.
#   - 1-charge plan "M"   : O->M=180, M->Z=180 (both <=240) -> least charging.
#   - 2-charge plan "P+M" : avoids no station but adds 25 min of charging.
#   A blocker (operator X) occupies station M's charger so the 1-charge plan
#   incurs ~10 min wait, while the 2-charge plan waits 0 but costs more trip.
#   At low operator weight the cheaper-trip 1-charge plan wins; raising the
#   operator weight (a wait penalty) tips the choice to the zero-wait plan.
FLEX_SCENARIO = {
    "meta": {"id": 99, "name": "flex-boundary", "description": "boundary test"},
    "route": {
        "origin": "O",
        "destination": "Z",
        "segments": [
            {"from": "O", "to": "P", "distance_km": 90},
            {"from": "P", "to": "M", "distance_km": 90},
            {"from": "M", "to": "Q", "distance_km": 90},
            {"from": "Q", "to": "Z", "distance_km": 90},
        ],
    },
    "stations": [{"id": "P", "chargers": 1}, {"id": "M", "chargers": 1}, {"id": "Q", "chargers": 1}],
    "physics": {"battery_range_km": 240, "charge_duration_min": 25, "speed_kmh": 60},
    "weights": {"individual": 1.0, "operator": 1.0, "overall": 1.0},
    "buses": [
        {"id": "blk", "operator": "X", "direction": "BK", "departure": "07:45"},
        {"id": "bus2", "operator": "Y", "direction": "BK", "departure": "08:00"},
    ],
}


def test_supplied_scenarios_are_weight_invariant():
    """Sweeping any soft-rule weight produces exactly one schedule per scenario."""
    sweeps = {
        "individual": [0.0, 0.5, 1.0, 2.0, 5.0, 10.0],
        "operator": [0.0, 0.5, 1.0, 2.0, 5.0, 10.0, 100.0],
        "overall": [0.0, 0.5, 1.0, 2.0, 5.0, 10.0],
    }
    files = sorted(glob.glob(SCENARIO_GLOB))
    assert files, "no scenario files found"
    for f in files:
        base = yaml.safe_load(open(f))
        for wname, values in sweeps.items():
            seen = set()
            for w in values:
                sc = copy.deepcopy(base)
                sc["weights"] = {"individual": 1.0, "operator": 1.0, "overall": 1.0}
                sc["weights"][wname] = w
                seen.add(_fingerprint(run_scheduler(sc)))
            assert len(seen) == 1, (
                f"{base['meta']['name']}: varying '{wname}' produced "
                f"{len(seen)} distinct schedules (expected 1)"
            )


def test_operator_weight_changes_plan_under_flexibility():
    """With genuine flexibility, raising the operator weight changes the plan."""
    low = copy.deepcopy(FLEX_SCENARIO)
    low["weights"]["operator"] = 0.0
    high = copy.deepcopy(FLEX_SCENARIO)
    high["weights"]["operator"] = 3.0

    plan_low = _plan_of(run_scheduler(low), "bus2")
    plan_high = _plan_of(run_scheduler(high), "bus2")

    assert plan_low != plan_high, (
        f"operator weight had no effect even with flexibility "
        f"(low={plan_low!r}, high={plan_high!r})"
    )
    # Concretely: cheaper-trip 1-charge plan at low weight, zero-wait plan at high.
    assert plan_low == "M", f"expected single-charge plan 'M' at op=0, got {plan_low!r}"
    assert plan_high == "P+M", f"expected zero-wait plan 'P+M' at op=3, got {plan_high!r}"


if __name__ == "__main__":
    test_supplied_scenarios_are_weight_invariant()
    print("PASS  supplied scenarios are invariant to all soft-rule weights")
    test_operator_weight_changes_plan_under_flexibility()
    print("PASS  operator weight changes the plan when flexibility exists")
