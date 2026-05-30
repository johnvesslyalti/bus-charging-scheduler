# Bus Charging Scheduler

A Python + Streamlit app that schedules electric bus charging across 4 intermediate stations on the Bengaluru → Kochi route. Five pre-built scenarios demonstrate how the scheduler handles different traffic patterns and operator compositions.

## Live app

**https://bus-charging-scheduler-gxdtsbk3d92yqpeqchkcqk.streamlit.app**

Hosted on Streamlit Community Cloud — pick any of the 5 scenarios from the dropdown.

## Run locally

```bash
git clone <your-repo-url>
cd bus_scheduler
pip install -r requirements.txt
streamlit run app.py
```

The app starts on `http://localhost:8501`. Use the dropdown to pick any of the 5 scenarios.

---

## How to change a weight

Weights live in each scenario's YAML file — one obvious place, nothing else to touch.

Open `scenarios/scenario_4.yaml` (or any scenario) and edit the `weights` block:

```yaml
weights:
  individual: 1.0   # penalty for per-bus wait time
  operator: 2.0     # penalty for imbalance across an operator's fleet
  overall: 1.0      # penalty for total trip duration
```

Save the file. Refresh the Streamlit app. The scheduler re-runs with the new weights automatically.

---

## How to add a new rule

### Adding a soft rule (affects priority when there's flexibility)

1. Open `scheduler.py` and write a scoring function:

```python
def score_priority_bus(bus: Bus, timeline: BusTimeline) -> float:
    """Penalise waits more for priority-flagged buses."""
    return timeline.total_wait_min * 2 if bus.priority else timeline.total_wait_min
```

2. Add it to `compute_cost()` with its weight:

```python
w_priority = weights.get("priority_bus", 0.0)
# ... existing lines ...
return (
    w_ind * score_individual
    + w_op  * score_operator
    + w_all * score_overall
    + w_priority * score_priority_bus(timeline.bus, timeline)   # ← new line
)
```

3. Add `priority_bus: 1.5` to the `weights` block in the relevant scenario YAML files.

4. Add `priority: true/false` to buses in the YAML that need the flag.

That's it — no other code changes.

### Adding a hard rule (must always hold)

Hard rules go in `enumerate_valid_plans()` (to prune invalid station choices) or in `allocate_slots()` (to reject infeasible time assignments). Example: block charging between 02:00–04:00 for maintenance:

```python
# In allocate_slots(), after computing start_charge:
BLOCK_START, BLOCK_END = 2 * 60, 4 * 60  # minutes since midnight
if BLOCK_START <= (start_charge % (24 * 60)) < BLOCK_END:
    start_charge = BLOCK_END  # push past the window
```

---

## Adding a new scenario

Create `scenarios/scenario_6.yaml` following the existing format. Add as many buses, stations, or operators as needed. The scheduler and UI pick it up on next load — no code changes.

---

## Project structure

```
bus_scheduler/
├── app.py              # Streamlit UI
├── scheduler.py        # Scheduling engine
├── requirements.txt
├── README.md
├── ARCHITECTURE.md
└── scenarios/
    ├── scenario_1.yaml   # Even spacing (baseline)
    ├── scenario_2.yaml   # Bunched start
    ├── scenario_3.yaml   # Asymmetric load
    ├── scenario_4.yaml   # Operator-heavy (operator weight = 2.0)
    └── scenario_5.yaml   # Worst-case convergence
```
