# ARCHITECTURE.md — Bus Charging Scheduler

## Framework / approach

### What I chose: event-driven simulation with a weighted cost function

The scheduler processes buses in departure-time order (earliest first). For each bus it:

1. **Enumerates all valid charging plans** — every subset of intermediate stations whose consecutive gaps respect the 240 km battery range constraint.
2. **Scores each plan** using a weighted cost function — the sum of three soft-rule terms, each multiplied by a tunable weight.
3. **Picks the lowest-cost plan** and commits it: the chosen charging slots are locked into the station timelines, making the problem smaller for every subsequent bus.

This is a greedy simulation, not an exhaustive global optimizer. It runs in milliseconds for any realistic fleet size, and the cost function is the natural extension point for new rules.

### Why this approach and not alternatives

**Why not ILP / integer programming?**  
An exact optimizer would find the global optimum but it is expensive, requires a solver library (PuLP, OR-Tools), and becomes much harder to extend with new rules. The brief explicitly asks for something that can have new rules dropped in — an additive cost function is that. ILP would need constraint reformulation every time a rule changes.

**Why not a priority queue / earliest-deadline-first?**  
EDF does not accommodate multi-objective weighting. It picks one criterion. The brief says weights must be tunable and trade off against each other — that calls for a scoring approach.

**Why greedy (departure order) and not backtracking?**  
With 20 buses and 4 stations the search space is tiny. A greedy solution with a good cost function gives defensible results in O(n·2^s) time where s = number of stations per direction (at most 4). Backtracking buys marginal quality improvements at exponential cost, and re-scheduling a later bus never un-commits an earlier bus's slot anyway.

---

## Data structure design

### Scenario YAML schema

Each scenario is a self-contained YAML file with five top-level keys:

```
meta        — human-readable name/description
route       — ordered list of segments with from/to/distance_km
stations    — list of charging station IDs with charger counts
physics     — battery_range_km, charge_duration_min, speed_kmh
weights     — individual / operator / overall (extensible)
buses       — list of bus objects with id/operator/direction/departure
```

The route is a **list of segments**, not a hardcoded enum. Adding a new station between B and C means inserting one YAML entry. Changing the Bengaluru→A distance means editing one number. The scheduler reads the route at runtime and derives all distances and orderings from it.

Direction is stored as a string tag (`"BK"` or `"KB"`). The engine reverses the segment list to get the KB route — no direction-specific code anywhere.

Charger count per station is a field on the station object (`chargers: 1`). Supporting two chargers at station B means changing `chargers: 2` in the YAML.

---

## Anticipated future changes and how the design handles them

These are the changes I considered when designing the schema. Each is handled by a YAML edit, not a code change, unless noted.

### 1. New charging station added to the route

**Example:** Add station E between D and Kochi, 50 km from D.

**Change needed:** Insert one segment entry in `route.segments` and one entry in `stations`. The scheduler's `enumerate_valid_plans()` reads stations from the YAML at runtime — no code change.

---

### 2. Different segment distances (road changes, reroute)

**Example:** A→B distance changes from 120 km to 140 km.

**Change needed:** Edit `distance_km` in the relevant segment. The engine derives all travel times from this value — one number, done.

---

### 3. More chargers at a busy station

**Example:** Station B gets a second charger during peak hours.

**Change needed:** Set `chargers: 2` for station B in the YAML. The engine already stores slots per-charger and allocates to the first available charger. Zero code change.

---

### 4. New operator

**Example:** Add operator "volvobuses" to some buses.

**Change needed:** Set `operator: "volvobuses"` on those bus entries. Operators are free-form strings — the cost function groups buses by operator automatically. No code change.

---

### 5. More buses (scaling the fleet)

**Example:** 100 buses instead of 20.

**Change needed:** Add more entries to the `buses` list in YAML. The engine sorts by departure time and processes all of them — O(n·2^s) complexity. With 4 stations s=4, so 16 plans per bus regardless of fleet size. No code change.

---

### 6. More routes sharing the same stations

**Example:** A Bengaluru → Mysore route that also uses station A.

**Change needed:** New scenario YAML with a different route definition. The station slot tracker is keyed by station ID — buses from different routes compete for the same charger correctly. No code change.

---

### 7. A new soft rule (e.g. electricity cost by time of day)

**Example:** Charging between 22:00–06:00 costs 30% less; prefer night slots.

**Change needed:**
- Add a `score_off_peak_cost(stop)` function in `scheduler.py` (~5 lines).
- Add `+ weights.get("off_peak", 0.0) * score_off_peak_cost(stop)` to `compute_cost()`.
- Add `off_peak: 1.5` to the `weights` block in relevant scenario YAMLs.

Code changes: ~7 lines in `scheduler.py`. No changes to YAML schema, UI, or data loader.

---

### 8. A new hard rule (e.g. maintenance window at a station)

**Example:** Station C is unavailable 02:00–04:00 for maintenance.

**Change needed:**
- Add a `blocked_windows` field to the station object in YAML: `blocked_windows: [{start: "02:00", end: "04:00"}]`.
- Add a check in `allocate_slots()` that pushes `start_charge` past any blocked window.

Code changes: ~10 lines in `scheduler.py`. YAML schema gains one optional field — backward-compatible (stations without it work as before).

---

### 9. Priority buses (always go first at a charger)

**Example:** Emergency vehicles get no-wait priority.

**Change needed:**
- Add `priority: true` to relevant bus entries in YAML.
- In `compute_cost()`, multiply the individual wait penalty by a large factor for priority buses, or add a dedicated `score_priority` term.
- Alternatively, process priority buses first before sorting by departure time.

Code changes: ~5 lines. YAML: add optional `priority` field.

---

### 10. Variable charging time (partial charge)

**Example:** A bus only needs 15 minutes of charge because it still has 50 km of range.

**Change needed:**
- The `charge_duration_min` field in physics is already per-scenario. Making it per-bus or per-stop would require adding a `remaining_range_km` field to each bus and a formula in `allocate_slots()`.
- YAML schema: add `charge_duration_min` as an optional override on individual bus entries.

Code changes: ~10 lines in `allocate_slots()`. YAML: one optional field.

---

### 11. Driver shift constraints

**Example:** A driver can only work 6 hours; if the trip takes longer due to waits, a handover is needed at a station.

**Change needed:**
- Add `max_drive_hours` to the bus entry in YAML.
- Add a hard check after timeline construction that flags buses exceeding the limit.
- This is a constraint on the output, not the scheduling logic itself — add as a post-processing validation step.

---

### 12. Multiple routes sharing a global station pool

**Example:** Two routes (BK and Bengaluru–Chennai) both use a station near the highway junction.

**Change needed:**
- Station slot tracker is already keyed globally by station ID.
- Run `run_scheduler()` for each route but pass the same `station_timelines` dict as shared state.
- YAML: no change. Code: extract `station_timelines` from `run_scheduler()` and make it a parameter — ~5 lines.

---

## How to change a weight (with a code example)

```yaml
# scenarios/scenario_4.yaml
weights:
  individual: 1.0
  operator: 2.0   # ← change this value
  overall: 1.0
```

That is the only change. The scheduler reads weights from the YAML and passes them to `compute_cost()`. No other file is touched.

---

## How to add a new soft rule (with a code example)

**Goal:** Penalise buses that arrive at their destination after 06:00 (night curfew).

**Step 1 — Write the scoring function in `scheduler.py`:**

```python
def score_late_arrival(timeline: BusTimeline, curfew_min: float = 6 * 60) -> float:
    """Return minutes past curfew, or 0 if on time."""
    return max(0.0, timeline.arrival_min % (24 * 60) - curfew_min)
```

**Step 2 — Wire it into `compute_cost()`:**

```python
w_curfew = weights.get("curfew", 0.0)

return (
    w_ind    * score_individual
    + w_op   * score_operator
    + w_all  * score_overall
    + w_curfew * score_late_arrival(timeline)   # ← add this line
)
```

**Step 3 — Add the weight to the scenario YAML:**

```yaml
weights:
  individual: 1.0
  operator: 1.0
  overall: 1.0
  curfew: 3.0   # ← add this line
```

Done. Three targeted edits; nothing else changes.

---

## Assumptions made

1. **Speed is uniform and constant** — 60 km/h throughout. Traffic, stops, and acceleration are not modelled. Assumption documented in YAML as `speed_kmh`.

2. **Charging always fills to full** — no partial charges. This simplifies the range constraint to a single number (240 km from any full charge).

3. **Buses are processed in departure-time order** — a bus that departs earlier has first claim on charger slots. This is the most natural fairness baseline before the weighted cost function applies.

4. **Endpoints (Bengaluru and Kochi) have unlimited slow chargers** — the spec states every bus departs with a full charge. These endpoints are not part of the scheduling problem.

5. **"Operator fairness" penalty** is measured as the absolute deviation of a bus's wait from its operator's running average. This is a reasonable proxy for fleet balance without requiring global optimisation.

6. **Greedy commitment** — once a bus is scheduled its slots are locked. A later bus cannot displace an earlier one. This makes the schedule deterministic and explainable.

7. **All 5 scenario YAMLs use the same route definition** — segments, distances, and station IDs are duplicated across files for self-containedness. In production, a shared `route_config.yaml` referenced by each scenario would be cleaner, but adds indirection that isn't worth it for 5 scenarios.
