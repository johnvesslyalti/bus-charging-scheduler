"""
experiments/weight_sensitivity.py

Reproducible evidence for the "Weight sensitivity" section of ARCHITECTURE.md.

Run from the repo root:

    python experiments/weight_sensitivity.py

It regenerates, from the actual scheduler and the shipped scenario files:

  1. WEIGHT SWEEP        — distinct schedules produced as each soft-rule weight
                           is varied (the all-weight invariance result).
  2. PER-DECISION AUDIT  — for every bus decision, whether its cost-optimal plan
                           is unique, tied-but-identical, or tied-and-differing.
  3. REJECTED ALTERNATIVES — the two prototyped reorderings and their measured
                           failure modes (non-monotone fairness / degraded
                           throughput).

The numbers printed here are the source of truth for ARCHITECTURE.md §2, §3, §5.
This script imports only the public scheduler API plus the building blocks it
already exposes; it never mutates the committed scheduler.
"""

from __future__ import annotations

import copy
import glob
import math
import os
import sys

import yaml

# Allow running as `python experiments/weight_sensitivity.py` from the repo root.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scheduler import (
    Bus,
    BusTimeline,
    Route,
    Segment,
    allocate_slots,
    audit_plan_flexibility,
    compute_arrival,
    enumerate_valid_plans,
    run_scheduler,
)

SCENARIO_GLOB = os.path.join(os.path.dirname(__file__), "..", "scenarios", "scenario_*.yaml")


def load_scenarios() -> list[dict]:
    return [yaml.safe_load(open(f)) for f in sorted(glob.glob(SCENARIO_GLOB))]


def fingerprint(timelines: list[BusTimeline]) -> tuple:
    """A schedule's full identity: plan, arrival, and wait per bus."""
    return tuple(
        (
            tl.bus.id,
            "+".join(s.station for s in tl.charging_stops),
            round(tl.arrival_min, 3),
            round(tl.total_wait_min, 3),
        )
        for tl in sorted(timelines, key=lambda t: t.bus.id)
    )


# ---------------------------------------------------------------------------
# 1. Weight sweep — distinct schedules per scenario as each weight varies
# ---------------------------------------------------------------------------

def weight_sweep() -> None:
    print("=" * 78)
    print("1. WEIGHT SWEEP — distinct schedules as each soft weight varies")
    print("   (sweeping one weight; the other two held at 1.0)")
    print("=" * 78)
    sweeps = {
        "individual": [0.0, 0.5, 1.0, 2.0, 5.0, 10.0],
        "operator": [0.0, 0.5, 1.0, 2.0, 5.0, 10.0, 100.0],
        "overall": [0.0, 0.5, 1.0, 2.0, 5.0, 10.0],
    }
    print(f"  {'Scenario':<38} individual  operator  overall")
    for base in load_scenarios():
        counts = {}
        for wname, values in sweeps.items():
            seen = set()
            for w in values:
                sc = copy.deepcopy(base)
                sc["weights"] = {"individual": 1.0, "operator": 1.0, "overall": 1.0}
                sc["weights"][wname] = w
                seen.add(fingerprint(run_scheduler(sc)))
            counts[wname] = len(seen)
        print(
            f"  {base['meta']['name']:<38} "
            f"{counts['individual']:^10} {counts['operator']:^9} {counts['overall']:^8}"
        )
    print()


# ---------------------------------------------------------------------------
# 2. Per-decision audit — replays the scheduler loop, classifying each bus
# ---------------------------------------------------------------------------

def _build_route(scenario: dict) -> Route:
    segs = [Segment(s["from"], s["to"], s["distance_km"]) for s in scenario["route"]["segments"]]
    return Route(
        origin=scenario["route"]["origin"],
        destination=scenario["route"]["destination"],
        segments=segs,
        speed_kmh=scenario["physics"]["speed_kmh"],
    )


def per_decision_audit() -> None:
    print("=" * 78)
    print("2. PER-DECISION AUDIT — classification of every bus's cost-optimal plan")
    print("=" * 78)
    print(f"  {'Scenario':<38} buses  single  tied-identical  tied-differing")
    tot = [0, 0, 0, 0]
    for base in load_scenarios():
        # shared source of truth with the scheduler and the UI panel
        c = audit_plan_flexibility(base)
        n, s, ti, td = c["buses"], c["single"], c["tied_identical"], c["tied_differing"]
        tot[0] += n
        tot[1] += s
        tot[2] += ti
        tot[3] += td
        print(f"  {base['meta']['name']:<38} {n:^5} {s:^7} {ti:^15} {td:^14}")
    print(f"  {'TOTAL':<38} {tot[0]:^5} {tot[1]:^7} {tot[2]:^15} {tot[3]:^14}")
    print()


# ---------------------------------------------------------------------------
# 3. Rejected alternatives — measured failure modes
# ---------------------------------------------------------------------------

def _best_throughput_timeline(bus, route, phys, stl):
    """Plan minimising (wait + trip) given current station state — the baseline rule."""
    brange = phys["battery_range_km"]
    best, best_cost = None, math.inf
    for plan in enumerate_valid_plans(bus, route, brange):
        merged = {st: sorted(s for slots in cs for s in slots) for st, cs in stl.items()}
        stops = allocate_slots(plan, bus, route, phys, merged)
        if stops is None:
            continue
        arr = compute_arrival(bus, stops, route)
        tl = BusTimeline(bus, stops, bus.departure_min, arr)
        cost = tl.total_wait_min + (arr - bus.departure_min)
        if cost < best_cost - 1e-9:
            best_cost, best = cost, tl
    return best


def _commit(tl, stl):
    for stop in tl.charging_stops:
        start = stop.start_charge_min
        end = start + stop.charge_min
        placed = False
        for slots in stl[stop.station]:
            latest = max((s[1] for s in slots), default=0.0)
            if start >= latest:
                slots.append((start, end))
                placed = True
                break
        if not placed:
            stl[stop.station][0].append((start, end))


def _fresh_state(scenario):
    chargers = {s["id"]: s.get("chargers", 1) for s in scenario["stations"]}
    stl = {s: [[] for _ in range(n)] for s, n in chargers.items()}
    buses = []
    for b in scenario["buses"]:
        h, m = b["departure"].split(":")
        buses.append(Bus(b["id"], b["operator"], b["direction"], float(int(h) * 60 + int(m))))
    return stl, sorted(buses, key=lambda b: b.departure_min)


def alt_operator_priority(scenario, wop):
    """Charger-queue reordering: within one charge cycle, promote the operator
    with the highest running average wait (catch-up). Measures fairness spread."""
    from collections import defaultdict

    phys = scenario["physics"]
    route = _build_route(scenario)
    window = phys["charge_duration_min"]
    stl, uns = _fresh_state(scenario)
    opstats = defaultdict(lambda: {"total": 0.0, "count": 0})
    tls = []
    while uns:
        frontier = uns[0].departure_min
        cand = [b for b in uns if b.departure_min <= frontier + window]

        def avg(o):
            d = opstats[o]
            return d["total"] / d["count"] if d["count"] else 0.0

        pick = min(cand, key=lambda b: b.departure_min - wop * avg(b.operator))
        tl = _best_throughput_timeline(pick, route, phys, stl)
        _commit(tl, stl)
        opstats[pick.operator]["total"] += tl.total_wait_min
        opstats[pick.operator]["count"] += 1
        tls.append(tl)
        uns.remove(pick)
    by_op = defaultdict(list)
    for tl in tls:
        by_op[tl.bus.operator].append(tl.total_wait_min)
    avgs = {o: sum(w) / len(w) for o, w in by_op.items()}
    spread = max(avgs.values()) - min(avgs.values())
    total = sum(tl.total_wait_min for tl in tls)
    return total, spread


def alt_dynamic_minwait(scenario):
    """Reorder commits within a window to the globally-minimum-wait candidate.
    Measures throughput (total wait) vs the baseline departure-order scheduler."""
    phys = scenario["physics"]
    route = _build_route(scenario)
    window = phys["charge_duration_min"]
    stl, uns = _fresh_state(scenario)
    tls = []
    while uns:
        frontier = uns[0].departure_min
        cand = [b for b in uns if b.departure_min <= frontier + window]
        evald = [(b, _best_throughput_timeline(b, route, phys, stl)) for b in cand]
        pick, tl = min(evald, key=lambda bt: bt[1].total_wait_min)
        _commit(tl, stl)
        tls.append(tl)
        uns.remove(pick)
    return sum(tl.total_wait_min for tl in tls)


def rejected_alternatives() -> None:
    print("=" * 78)
    print("3. REJECTED ALTERNATIVES — measured failure modes vs the stable scheduler")
    print("=" * 78)
    scenarios = {s["meta"]["name"]: s for s in load_scenarios()}
    s2 = next(s for n, s in scenarios.items() if n.startswith("Scenario 2"))
    s5 = next(s for n, s in scenarios.items() if n.startswith("Scenario 5"))

    base2 = sum(t.total_wait_min for t in run_scheduler(s2))
    base5 = sum(t.total_wait_min for t in run_scheduler(s5))
    print(f"  Baseline total wait — Scenario 2: {base2:.0f} min | Scenario 5: {base5:.0f} min")
    print()

    print("  (a) Operator-priority charger reordering (fairness spread, Scenario 2):")
    for wop in [0.0, 3.0, 10.0]:
        total, spread = alt_operator_priority(s2, wop)
        print(f"        operator={wop:<5}  total wait={total:.0f}  per-operator spread={spread:.1f} min")
    print()

    print("  (b) Dynamic minimum-wait selection (throughput):")
    for name, sc, base in [("Scenario 5", s5, base5), ("Scenario 2", s2, base2)]:
        total = alt_dynamic_minwait(sc)
        pct = 100.0 * (total - base) / base
        print(f"        {name}: total wait {base:.0f} -> {total:.0f} min ({pct:+.0f}%)")
    print()


if __name__ == "__main__":
    weight_sweep()
    per_decision_audit()
    rejected_alternatives()
