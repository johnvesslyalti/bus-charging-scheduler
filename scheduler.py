"""
scheduler.py — Bus Charging Scheduler Engine

Architecture:
  Event-driven simulation using a timeline of charging slots per station.
  Each bus is assigned charging stations (its "plan") then given a time slot
  at each station via a greedy weighted-cost insertion.

Extending the scheduler:
  - New soft rule: add a score_<rule>(bus, ...) function and include it in
    compute_cost() weighted by scenario['weights']['<rule>']
  - New hard rule: add a check in is_valid_plan() or during slot allocation
  - More stations/buses/operators: zero code changes — driven entirely by
    the scenario YAML
"""

from __future__ import annotations
import math
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Segment:
    from_stop: str
    to_stop: str
    distance_km: float

    @property
    def travel_min(self) -> float:
        return 0.0  # filled in by Route using speed


@dataclass
class Route:
    origin: str
    destination: str
    segments: list[Segment]
    speed_kmh: float

    def segment_travel_min(self, seg: Segment) -> float:
        return (seg.distance_km / self.speed_kmh) * 60.0

    def stops_in_order(self, direction: str) -> list[str]:
        """All stops including endpoints, in travel order for this direction."""
        stops = [self.origin] + [s.to_stop for s in self.segments]
        if direction == "KB":
            stops = list(reversed(stops))
        return stops

    def distance_between(self, a: str, b: str, direction: str) -> float:
        """Total km between two consecutive stops in travel direction."""
        stops = self.stops_in_order(direction)
        idx_a = stops.index(a)
        idx_b = stops.index(b)
        if idx_b <= idx_a:
            raise ValueError(f"b must come after a for direction {direction}")
        segs = self.segments if direction == "BK" else list(reversed(self.segments))
        total = 0.0
        for i in range(idx_a, idx_b):
            seg = segs[i]
            total += seg.distance_km
        return total

    def travel_time_min(self, a: str, b: str, direction: str) -> float:
        return (self.distance_between(a, b, direction) / self.speed_kmh) * 60.0

    def station_stops(self, direction: str) -> list[str]:
        """Charging-eligible stops (not the endpoints)."""
        all_stops = self.stops_in_order(direction)
        return all_stops[1:-1]


@dataclass
class Bus:
    id: str
    operator: str
    direction: str        # "BK" or "KB"
    departure_min: float  # minutes since midnight


@dataclass
class ChargingStop:
    station: str
    arrive_min: float   # when the bus arrives at the station
    wait_min: float     # how long it waits before charger is free
    charge_min: float   # always 25
    depart_min: float   # arrive + wait + charge

    @property
    def start_charge_min(self) -> float:
        return self.arrive_min + self.wait_min


@dataclass
class BusTimeline:
    bus: Bus
    charging_stops: list[ChargingStop]
    departure_min: float
    arrival_min: float   # at final destination

    @property
    def total_wait_min(self) -> float:
        return sum(s.wait_min for s in self.charging_stops)


# ---------------------------------------------------------------------------
# Plan generator — which stations does a bus visit?
# ---------------------------------------------------------------------------

def enumerate_valid_plans(bus: Bus, route: Route, battery_range: float) -> list[list[str]]:
    """
    Return all valid subsets of charging stations (in route order) that
    satisfy the range constraint for this bus.
    A plan is valid if no gap (origin→first, between consecutive, last→dest)
    exceeds battery_range km.
    """
    stations = route.station_stops(bus.direction)
    origin = route.stops_in_order(bus.direction)[0]
    dest = route.stops_in_order(bus.direction)[-1]
    n = len(stations)
    valid = []

    for mask in range(1 << n):
        chosen = [stations[i] for i in range(n) if mask & (1 << i)]
        if not chosen:
            continue
        # Check all consecutive gaps
        checkpoints = [origin] + chosen + [dest]
        ok = True
        for i in range(len(checkpoints) - 1):
            try:
                d = route.distance_between(checkpoints[i], checkpoints[i + 1], bus.direction)
            except ValueError:
                ok = False
                break
            if d > battery_range:
                ok = False
                break
        if ok:
            valid.append(chosen)
    return valid


# ---------------------------------------------------------------------------
# Cost function — weighted soft rules
# ---------------------------------------------------------------------------

def compute_cost(
    timeline: BusTimeline,
    operator_wait_totals: dict[str, float],
    weights: dict[str, float],
) -> float:
    """
    Lower score = better.

    Soft rules (all additive, each scaled by its weight):
      individual : total wait time for this bus
      operator   : std dev of wait totals across this operator's buses so far
                   (we use the marginal increase from adding this bus's wait)
      overall    : this bus's total trip duration beyond theoretical minimum

    To add a new soft rule:
      1. Write a score_<name>(bus, timeline, ...) -> float function
      2. Add   + weights.get('<name>', 0.0) * score_<name>(...)   here
      3. Add the weight key to the scenario YAML
    """
    w_ind = weights.get("individual", 1.0)
    w_op  = weights.get("operator", 1.0)
    w_all = weights.get("overall", 1.0)

    # Individual: penalise total wait
    score_individual = timeline.total_wait_min

    # Operator: penalise how much this bus's wait diverges from its peers
    op = timeline.bus.operator
    existing = operator_wait_totals.get(op, 0.0)
    # simple proxy: squared difference from running mean would require history;
    # use absolute deviation from current operator average instead
    count = sum(1 for k, v in operator_wait_totals.items() if k == op)
    avg = (existing / count) if count > 0 else 0.0
    score_operator = abs(timeline.total_wait_min - avg)

    # Overall: total trip time vs theoretical minimum (no waits)
    trip_duration = timeline.arrival_min - timeline.departure_min
    # theoretical: travel time only (no waits, minimum charges)
    score_overall = trip_duration

    return (
        w_ind * score_individual
        + w_op  * score_operator
        + w_all * score_overall
    )


# ---------------------------------------------------------------------------
# Slot allocator — assigns a time slot at each station
# ---------------------------------------------------------------------------

def allocate_slots(
    plan: list[str],
    bus: Bus,
    route: Route,
    physics: dict[str, float],
    station_timelines: dict[str, list[tuple[float, float]]],
) -> list[ChargingStop] | None:
    """
    Given a charging plan (ordered list of stations), compute actual
    arrival/wait/depart times respecting:
      - 1 charger per station (no overlap)
      - A bus must arrive before it can start charging
    Returns None if the plan is physically infeasible for some reason.
    """
    charge_dur = physics["charge_duration_min"]
    stops: list[ChargingStop] = []
    current_pos = route.stops_in_order(bus.direction)[0]
    current_time = bus.departure_min

    for station in plan:
        travel = route.travel_time_min(current_pos, station, bus.direction)
        arrive = current_time + travel

        # Find earliest free slot at this station
        occupied = station_timelines[station]  # list of (start, end) slots
        # Sort by start time
        occupied_sorted = sorted(occupied)

        # Earliest we can start charging: when we arrive (bus must be there)
        earliest_start = arrive
        # Find a gap in occupied slots
        start_charge = earliest_start
        for (slot_start, slot_end) in occupied_sorted:
            if start_charge + charge_dur <= slot_start:
                # fits before this slot
                break
            if start_charge < slot_end:
                # overlaps — push after
                start_charge = slot_end
        # start_charge is now the actual charge start time
        wait = start_charge - arrive
        depart = start_charge + charge_dur

        stops.append(ChargingStop(
            station=station,
            arrive_min=arrive,
            wait_min=max(0.0, wait),
            charge_min=charge_dur,
            depart_min=depart,
        ))

        current_pos = station
        current_time = depart

    return stops


def compute_arrival(
    bus: Bus,
    stops: list[ChargingStop],
    route: Route,
) -> float:
    """Time bus reaches final destination after last charge."""
    dest = route.stops_in_order(bus.direction)[-1]
    if stops:
        last_stop = stops[-1]
        travel = route.travel_time_min(last_stop.station, dest, bus.direction)
        return last_stop.depart_min + travel
    else:
        # No charging stops (should not be valid per range rules, but handle it)
        origin = route.stops_in_order(bus.direction)[0]
        travel = route.travel_time_min(origin, dest, bus.direction)
        return bus.departure_min + travel


# ---------------------------------------------------------------------------
# Main scheduler — processes all buses in the scenario
# ---------------------------------------------------------------------------

def run_scheduler(scenario: dict[str, Any]) -> list[BusTimeline]:
    """
    Main entry point.  Takes a loaded scenario dict (from YAML) and returns
    a list of BusTimeline objects — one per bus — with full scheduling details.

    To change a weight: edit the scenario YAML (weights.individual / .operator / .overall)
    To add a new rule: see compute_cost() docstring above
    To grow the world: add buses/stations/routes to YAML — no code changes needed
    """
    # --- Build route ---
    phys = scenario["physics"]
    speed = phys["speed_kmh"]
    battery_range = phys["battery_range_km"]

    raw_segs = scenario["route"]["segments"]
    segments = [Segment(s["from"], s["to"], s["distance_km"]) for s in raw_segs]
    route = Route(
        origin=scenario["route"]["origin"],
        destination=scenario["route"]["destination"],
        segments=segments,
        speed_kmh=speed,
    )

    # --- Build station slot tracker ---
    # Each station maps to a list of (start_min, end_min) occupied intervals
    # Support multiple chargers per station (scenario YAML: chargers: N)
    station_chargers: dict[str, int] = {
        s["id"]: s.get("chargers", 1) for s in scenario["stations"]
    }
    # For multi-charger stations we track slots per charger
    # station_timelines[station][charger_idx] = list of (start, end)
    station_timelines: dict[str, list[list[tuple[float, float]]]] = {
        s: [[] for _ in range(n)] for s, n in station_chargers.items()
    }

    weights = scenario.get("weights", {"individual": 1.0, "operator": 1.0, "overall": 1.0})

    # --- Build bus objects ---
    buses = []
    for b in scenario["buses"]:
        h, m = b["departure"].split(":")
        dep_min = int(h) * 60 + int(m)
        buses.append(Bus(
            id=b["id"],
            operator=b["operator"],
            direction=b["direction"],
            departure_min=float(dep_min),
        ))

    # --- Sort buses by departure time (process earliest first) ---
    buses.sort(key=lambda b: b.departure_min)

    # --- Schedule each bus ---
    timelines: list[BusTimeline] = []
    operator_wait_totals: dict[str, float] = {}

    for bus in buses:
        plans = enumerate_valid_plans(bus, route, battery_range)
        if not plans:
            raise ValueError(f"No valid charging plan for bus {bus.id}")

        best_timeline: BusTimeline | None = None
        best_cost = math.inf

        for plan in plans:
            # For multi-charger stations, flatten to earliest-available charger
            # Build a merged timeline view for slot allocation
            merged_station_timelines: dict[str, list[tuple[float, float]]] = {}
            for st, charger_slots in station_timelines.items():
                # Merge all charger slots into one sorted list for allocation check
                merged_station_timelines[st] = sorted(
                    [slot for slots in charger_slots for slot in slots]
                )

            stops = allocate_slots(plan, bus, route, phys, merged_station_timelines)
            if stops is None:
                continue

            arrival = compute_arrival(bus, stops, route)
            tl = BusTimeline(
                bus=bus,
                charging_stops=stops,
                departure_min=bus.departure_min,
                arrival_min=arrival,
            )
            cost = compute_cost(tl, operator_wait_totals, weights)
            if cost < best_cost:
                best_cost = cost
                best_timeline = tl

        if best_timeline is None:
            raise ValueError(f"Could not schedule bus {bus.id}")

        # Commit slots to station timelines
        for stop in best_timeline.charging_stops:
            st = stop.station
            start = stop.start_charge_min
            end = stop.start_charge_min + stop.charge_min
            # Assign to the charger with the latest end time that still fits
            charger_slots = station_timelines[st]
            # Find charger where this slot fits (earliest available)
            assigned = False
            for slots in charger_slots:
                occupied_ends = [s[1] for s in slots]
                latest_end = max(occupied_ends) if occupied_ends else 0.0
                if start >= latest_end:
                    slots.append((start, end))
                    assigned = True
                    break
            if not assigned:
                # All chargers busy — find one where we wait least (should not
                # happen since allocate_slots already found a free slot)
                charger_slots[0].append((start, end))

        # Update operator running totals
        op = bus.operator
        operator_wait_totals[op] = (
            operator_wait_totals.get(op, 0.0) + best_timeline.total_wait_min
        )

        timelines.append(best_timeline)

    # Restore original order (by bus ID)
    timelines.sort(key=lambda t: t.bus.id)
    return timelines


# ---------------------------------------------------------------------------
# Helpers for the UI layer
# ---------------------------------------------------------------------------

def min_to_hhmm(minutes: float) -> str:
    h = int(minutes) // 60
    m = int(minutes) % 60
    return f"{h:02d}:{m:02d}"


def station_view(timelines: list[BusTimeline]) -> dict[str, list[dict]]:
    """
    Returns per-station ordered list of charging events, sorted by charge start time.
    Used by the UI for the per-station view.
    """
    result: dict[str, list[dict]] = {}
    for tl in timelines:
        for stop in tl.charging_stops:
            st = stop.station
            if st not in result:
                result[st] = []
            result[st].append({
                "bus_id": tl.bus.id,
                "operator": tl.bus.operator,
                "direction": tl.bus.direction,
                "arrive": min_to_hhmm(stop.arrive_min),
                "wait_min": round(stop.wait_min, 1),
                "charge_start": min_to_hhmm(stop.start_charge_min),
                "charge_end": min_to_hhmm(stop.start_charge_min + stop.charge_min),
            })
    for st in result:
        result[st].sort(key=lambda e: e["charge_start"])
    return result
