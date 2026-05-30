"""
app.py — Bus Charging Scheduler — Streamlit UI

Run locally:  streamlit run app.py
"""

import os
import glob
import yaml
import streamlit as st
import pandas as pd
from scheduler import run_scheduler, min_to_hhmm, station_view

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Bus Charging Scheduler",
    page_icon="⚡",
    layout="wide",
)

# ---------------------------------------------------------------------------
# Load scenarios
# ---------------------------------------------------------------------------

SCENARIO_DIR = os.path.join(os.path.dirname(__file__), "scenarios")


@st.cache_data
def load_scenarios() -> dict[str, dict]:
    files = sorted(glob.glob(os.path.join(SCENARIO_DIR, "scenario_*.yaml")))
    scenarios = {}
    for f in files:
        with open(f) as fh:
            data = yaml.safe_load(fh)
        label = data["meta"]["name"]
        scenarios[label] = data
    return scenarios


scenarios = load_scenarios()

# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
st.title("⚡ Bus Charging Scheduler")
st.caption("Bengaluru ↔ Kochi | 4 charging stations | weighted soft-rule scheduler")

# ---------------------------------------------------------------------------
# Scenario selector
# ---------------------------------------------------------------------------
scenario_name = st.selectbox("Select scenario", list(scenarios.keys()))
scenario = scenarios[scenario_name]

meta = scenario["meta"]
st.markdown(f"**{meta['name']}** — {meta['description']}")

# ---------------------------------------------------------------------------
# Run scheduler
# ---------------------------------------------------------------------------
with st.spinner("Running scheduler…"):
    try:
        timelines = run_scheduler(scenario)
        st_view = station_view(timelines)
        error = None
    except Exception as e:
        error = str(e)
        timelines = []
        st_view = {}

if error:
    st.error(f"Scheduler error: {error}")
    st.stop()

# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------
tab_input, tab_buses, tab_stations = st.tabs(
    ["📋 Scenario Input", "🚌 Per-Bus Timetable", "🔌 Per-Station View"]
)

# ── TAB 1: Scenario input ──────────────────────────────────────────────────
with tab_input:
    col1, col2 = st.columns(2)

    with col1:
        st.subheader("Route")
        route = scenario["route"]
        segs = route["segments"]
        seg_rows = [
            {"From": s["from"], "To": s["to"], "Distance (km)": s["distance_km"]}
            for s in segs
        ]
        st.dataframe(pd.DataFrame(seg_rows), use_container_width=True, hide_index=True)

        st.subheader("Physics")
        phys = scenario["physics"]
        st.markdown(
            f"- **Speed:** {phys['speed_kmh']} km/h  \n"
            f"- **Battery range:** {phys['battery_range_km']} km  \n"
            f"- **Charge duration:** {phys['charge_duration_min']} min  "
        )

        st.subheader("Weights")
        w = scenario.get("weights", {})
        st.markdown(
            f"- **individual** (per-bus wait penalty): `{w.get('individual', 1.0)}`  \n"
            f"- **operator** (fleet fairness penalty): `{w.get('operator', 1.0)}`  \n"
            f"- **overall** (total trip time penalty): `{w.get('overall', 1.0)}`  "
        )

        st.subheader("Stations")
        st_rows = [
            {"Station": s["id"], "Chargers": s.get("chargers", 1)}
            for s in scenario["stations"]
        ]
        st.dataframe(pd.DataFrame(st_rows), use_container_width=True, hide_index=True)

    with col2:
        st.subheader("Bus Fleet")
        bus_rows = [
            {
                "Bus ID": b["id"],
                "Operator": b["operator"],
                "Direction": "Bengaluru→Kochi" if b["direction"] == "BK" else "Kochi→Bengaluru",
                "Departure": b["departure"],
            }
            for b in scenario["buses"]
        ]
        st.dataframe(
            pd.DataFrame(bus_rows),
            use_container_width=True,
            hide_index=True,
            height=600,
        )

# ── TAB 2: Per-bus timetable ───────────────────────────────────────────────
with tab_buses:
    st.subheader("Per-Bus Timetable")
    st.caption(
        "Each row shows one charging stop. Columns: when the bus arrives, "
        "how long it waits for the charger, when charging starts/ends, and final arrival."
    )

    # Summary row per bus
    summary_rows = []
    for tl in timelines:
        depart_str = min_to_hhmm(tl.departure_min)
        arrive_str = min_to_hhmm(tl.arrival_min)
        trip_min = tl.arrival_min - tl.departure_min
        total_wait = tl.total_wait_min
        stations_used = " → ".join(s.station for s in tl.charging_stops)
        summary_rows.append({
            "Bus": tl.bus.id,
            "Operator": tl.bus.operator,
            "Direction": "BK" if tl.bus.direction == "BK" else "KB",
            "Departs": depart_str,
            "Arrives": arrive_str,
            "Trip (min)": round(trip_min),
            "Total Wait (min)": round(total_wait, 1),
            "Charges at": stations_used,
        })

    summary_df = pd.DataFrame(summary_rows)
    st.dataframe(summary_df, use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("Detailed Stop Breakdown")

    # Detailed per-bus breakdown
    detail_rows = []
    for tl in timelines:
        origin = "Bengaluru" if tl.bus.direction == "BK" else "Kochi"
        dest   = "Kochi"     if tl.bus.direction == "BK" else "Bengaluru"

        # Departure row
        detail_rows.append({
            "Bus": tl.bus.id,
            "Operator": tl.bus.operator,
            "Stop": f"🚌 {origin} (depart)",
            "Arrive": "—",
            "Wait (min)": "—",
            "Charge Start": "—",
            "Charge End": "—",
            "Depart": min_to_hhmm(tl.departure_min),
        })

        for stop in tl.charging_stops:
            detail_rows.append({
                "Bus": tl.bus.id,
                "Operator": tl.bus.operator,
                "Stop": f"⚡ {stop.station}",
                "Arrive": min_to_hhmm(stop.arrive_min),
                "Wait (min)": round(stop.wait_min, 1),
                "Charge Start": min_to_hhmm(stop.start_charge_min),
                "Charge End": min_to_hhmm(stop.start_charge_min + stop.charge_min),
                "Depart": min_to_hhmm(stop.depart_min),
            })

        # Arrival row
        detail_rows.append({
            "Bus": tl.bus.id,
            "Operator": tl.bus.operator,
            "Stop": f"🏁 {dest} (arrive)",
            "Arrive": min_to_hhmm(tl.arrival_min),
            "Wait (min)": "—",
            "Charge Start": "—",
            "Charge End": "—",
            "Depart": "—",
        })

    detail_df = pd.DataFrame(detail_rows)

    # Colour buses alternately for readability
    bus_ids = detail_df["Bus"].unique().tolist()

    def highlight_bus(row):
        idx = bus_ids.index(row["Bus"]) if row["Bus"] in bus_ids else 0
        color = "#1e2a3a" if idx % 2 == 0 else "#151e2b"
        return [f"background-color: {color}"] * len(row)

    styled = detail_df.style.apply(highlight_bus, axis=1)
    st.dataframe(styled, use_container_width=True, hide_index=True, height=700)

# ── TAB 3: Per-station view ────────────────────────────────────────────────
with tab_stations:
    st.subheader("Per-Station Charging Order")
    st.caption(
        "The order in which buses use each charger, sorted by charge-start time. "
        "Direction shown as BK (Bengaluru→Kochi) or KB (Kochi→Bengaluru)."
    )

    station_ids = [s["id"] for s in scenario["stations"]]
    cols = st.columns(len(station_ids))

    for col, st_id in zip(cols, station_ids):
        with col:
            st.markdown(f"### Station {st_id}")
            events = st_view.get(st_id, [])
            if not events:
                st.info("No buses charge here")
                continue
            for i, ev in enumerate(events, start=1):
                dir_arrow = "→ Kochi" if ev["direction"] == "BK" else "→ Bengaluru"
                wait_str = f" *(wait {ev['wait_min']:.0f} min)*" if ev["wait_min"] > 0 else ""
                st.markdown(
                    f"**{i}.** `{ev['bus_id']}`  \n"
                    f"{ev['operator']} · {dir_arrow}  \n"
                    f"Arrive {ev['arrive']}{wait_str}  \n"
                    f"Charge {ev['charge_start']} → {ev['charge_end']}"
                )
                st.divider()

# ---------------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------------
st.markdown(
    "---\n"
    "<small>Scheduler: weighted cost-function + event-driven simulation | "
    "Data: YAML scenario files | Stack: Python + Streamlit</small>",
    unsafe_allow_html=True,
)
