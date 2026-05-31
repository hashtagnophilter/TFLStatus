#!/usr/bin/env python3
"""
TFL Status Data Visualisation Generator
Downloads data from Azure Table Storage and creates an interactive HTML dashboard.
"""

import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from azure.data.tables import TableServiceClient
try:
    from azure.core.exceptions import ResourceExistsError
    from azure.storage.blob import BlobServiceClient, ContentSettings
except ImportError:  # pragma: no cover - depends on optional publishing package availability
    ResourceExistsError = None
    BlobServiceClient = None
    ContentSettings = None


# ── Azure connection ────────────────────────────────────────────────────────
def get_connection_string():
    """Resolve connection string from env or local settings."""
    cs = os.environ.get("STORAGE_CONNECTION_STRING", "")
    if cs:
        return cs
    # Try local.settings.json
    try:
        with open("local.settings.json") as f:
            settings = json.load(f)
            return settings.get("Values", {}).get("STORAGE_CONNECTION_STRING", "")
    except FileNotFoundError:
        pass
    raise ValueError("Set STORAGE_CONNECTION_STRING env var or provide local.settings.json")


# ── Data download ───────────────────────────────────────────────────────────
def download_all_data(conn_str):
    service = TableServiceClient.from_connection_string(conn_str)

    print("Downloading TFLState...")
    state_client = service.get_table_client("TFLState")
    state_data = [dict(e) for e in state_client.list_entities()]
    print(f"  {len(state_data)} state records")

    delay_tables = {"MetLineDelays": "metropolitan", "BakLineDelays": "bakerloo", "CircleDelays": "circle"}
    delay_data = {}
    for table_name, line_name in delay_tables.items():
        print(f"Downloading {table_name}...")
        client = service.get_table_client(table_name)
        entities = [dict(e) for e in client.list_entities()]
        delay_data[line_name] = entities
        print(f"  {len(entities)} records")

    return state_data, delay_data


# ── Analysis functions ──────────────────────────────────────────────────────
LINE_COLORS = {
    "metropolitan": "#9B0056",
    "bakerloo": "#B36305",
    "circle": "#FFD300",
    "hammersmith-city": "#F3A9BB",
}
LINE_LABELS = {
    "metropolitan": "Metropolitan",
    "bakerloo": "Bakerloo",
    "circle": "Circle",
    "hammersmith-city": "Hammersmith & City",
}
DISRUPTION_STATUSES = {"Minor Delays", "Severe Delays", "Part Suspended", "Suspended", "Part Closure"}
DASHBOARD_DATA_FILE = "latest_dashboard_data.json"
DASHBOARD_HTML_FILE = "tfl_status_dashboard_v2.html"


def analyse_reliability(state_data):
    """Per-line % time in each status (excluding Service Closed for fairness)."""
    lines = defaultdict(list)
    for r in state_data:
        lines[r["PartitionKey"]].append(r)

    result = {}
    for line, records in lines.items():
        operating = [r for r in records if r["previous_status"] != "Service Closed"]
        total = len(operating) or 1
        statuses = Counter(r["previous_status"] for r in operating)
        good = statuses.get("Good Service", 0)
        result[line] = {
            "good_pct": round(good / total * 100, 1),
            "disrupted_pct": round((total - good) / total * 100, 1),
            "breakdown": {s: round(c / total * 100, 1) for s, c in statuses.most_common()},
            "total_checks": total,
        }
    return result


def analyse_hourly_heatmap(state_data):
    """Disruption probability per hour-of-day × day-of-week, per line."""
    # {line: {(dow, hour): [is_disrupted, ...]}}
    buckets = defaultdict(lambda: defaultdict(list))
    for r in state_data:
        if r["previous_status"] == "Service Closed":
            continue
        try:
            ts = datetime.fromisoformat(r["timestamp"])
        except (ValueError, KeyError):
            continue
        dow = ts.weekday()  # 0=Mon
        hour = ts.hour
        is_disrupted = 1 if r["previous_status"] in DISRUPTION_STATUSES else 0
        buckets[r["PartitionKey"]][(dow, hour)].append(is_disrupted)

    result = {}
    for line, cells in buckets.items():
        matrix = []
        for dow in range(5):  # Mon-Fri only
            for hour in range(5, 24):  # 5am-11pm
                samples = cells.get((dow, hour), [])
                if samples:
                    pct = round(sum(samples) / len(samples) * 100, 1)
                else:
                    pct = 0
                matrix.append({"x": hour, "y": dow, "v": pct})
        result[line] = matrix
    return result


def analyse_cross_line_correlation(state_data):
    """How often are multiple lines disrupted simultaneously? Binned by 2-min intervals."""
    time_bins = defaultdict(dict)
    for r in state_data:
        if r["previous_status"] == "Service Closed":
            continue
        try:
            ts = datetime.fromisoformat(r["timestamp"])
        except (ValueError, KeyError):
            continue
        bin_key = ts.strftime("%Y-%m-%d %H:%M")[:15]  # ~2 min bins
        is_disrupted = r["previous_status"] in DISRUPTION_STATUSES
        time_bins[bin_key][r["PartitionKey"]] = is_disrupted

    # Count co-disruption pairs
    lines = ["metropolitan", "bakerloo", "circle", "hammersmith-city"]
    co_matrix = {a: {b: 0 for b in lines} for a in lines}
    total_bins = 0
    for bin_key, line_status in time_bins.items():
        if len(line_status) < 2:
            continue
        total_bins += 1
        disrupted = [l for l, d in line_status.items() if d]
        for i, a in enumerate(disrupted):
            for b in disrupted[i + 1 :]:
                co_matrix[a][b] += 1
                co_matrix[b][a] += 1
            co_matrix[a][a] += 1

    # Normalise to percentages
    for a in lines:
        own = co_matrix[a][a] or 1
        for b in lines:
            if a != b:
                co_matrix[a][b] = round(co_matrix[a][b] / own * 100, 1) if own else 0
    return co_matrix


def analyse_delay_causes(delay_data):
    """Categorise delay reasons using keyword extraction."""
    cause_patterns = [
        ("Signal Failure", r"signal\s+fail"),
        ("Faulty Train", r"faulty\s+train"),
        ("Points Failure", r"points\s+fail"),
        ("Person Ill on Train", r"person\s+ill|ill\s+on\s+a?\s*train"),
        ("Customer Incident", r"customer\s+incident"),
        ("Trespass on Tracks", r"trespass"),
        ("Fire Alert", r"fire"),
        ("Casualty on Track", r"casualty\s+on\s+the\s+track|casualty\s+on\s+track"),
        ("Defective Train", r"defective\s+train"),
        ("Late Finish Engineering", r"late\s+finish|over.?running\s+engineer"),
        ("Staff Shortage", r"staff\s+short|shortage\s+of\s+staff"),
        ("Power Supply Issue", r"power\s+supply"),
        ("Security Alert", r"security\s+alert"),
        ("Police Investigation", r"police"),
        ("Weather", r"weather|flood"),
        ("Earlier Incident", r"earlier"),
    ]
    causes_by_line = defaultdict(lambda: Counter())
    all_causes = Counter()

    for line, events in delay_data.items():
        for e in events:
            reason = e.get("reason", "").lower()
            if not reason:
                continue
            matched = False
            for cause_name, pattern in cause_patterns:
                if re.search(pattern, reason):
                    causes_by_line[line][cause_name] += 1
                    all_causes[cause_name] += 1
                    matched = True
                    break
            if not matched:
                causes_by_line[line]["Other"] += 1
                all_causes["Other"] += 1

    return dict(all_causes), {k: dict(v) for k, v in causes_by_line.items()}


def analyse_daily_disruption(state_data):
    """Total disruption minutes per day across all lines (calendar heatmap data)."""
    day_minutes = defaultdict(float)
    for r in state_data:
        if r["previous_status"] == "Service Closed":
            continue
        try:
            ts = datetime.fromisoformat(r["timestamp"])
        except (ValueError, KeyError):
            continue
        day = ts.strftime("%Y-%m-%d")
        if r["previous_status"] in DISRUPTION_STATUSES:
            day_minutes[day] += 2  # Each check is ~2 minutes

    # Per line per day
    line_day = defaultdict(lambda: defaultdict(float))
    for r in state_data:
        if r["previous_status"] == "Service Closed":
            continue
        try:
            ts = datetime.fromisoformat(r["timestamp"])
        except (ValueError, KeyError):
            continue
        day = ts.strftime("%Y-%m-%d")
        if r["previous_status"] in DISRUPTION_STATUSES:
            line_day[r["PartitionKey"]][day] += 2

    return dict(day_minutes), {k: dict(v) for k, v in line_day.items()}


def analyse_peak_vs_offpeak(state_data):
    """Disruption rates during AM peak (7-9), PM peak (17-19), and off-peak."""
    periods = {"AM Peak (07-09)": (7, 9), "PM Peak (17-19)": (17, 19)}
    results = {}

    for line in ["metropolitan", "bakerloo", "circle", "hammersmith-city"]:
        line_records = [r for r in state_data if r["PartitionKey"] == line and r["previous_status"] != "Service Closed"]
        period_rates = {}
        for period_name, (start, end) in periods.items():
            in_period = [r for r in line_records if start <= datetime.fromisoformat(r["timestamp"]).hour < end]
            disrupted = sum(1 for r in in_period if r["previous_status"] in DISRUPTION_STATUSES)
            total = len(in_period) or 1
            period_rates[period_name] = round(disrupted / total * 100, 1)

        offpeak = [r for r in line_records if not (7 <= datetime.fromisoformat(r["timestamp"]).hour < 9 or 17 <= datetime.fromisoformat(r["timestamp"]).hour < 19)]
        disrupted_off = sum(1 for r in offpeak if r["previous_status"] in DISRUPTION_STATUSES)
        period_rates["Off-Peak"] = round(disrupted_off / (len(offpeak) or 1) * 100, 1)
        results[line] = period_rates

    return results


def analyse_severity_escalation(delay_data):
    """Track how delays escalate: start as minor, become severe, etc."""
    escalations_by_line = {}
    for line, events in delay_data.items():
        severities = [e.get("severity", "Unknown") for e in sorted(events, key=lambda x: x.get("timestamp", ""))]
        transitions = Counter()
        for i in range(len(severities) - 1):
            if severities[i] != severities[i + 1]:
                transitions[f"{severities[i]} → {severities[i+1]}"] += 1
        escalations_by_line[line] = dict(transitions)
    return escalations_by_line


def analyse_disruption_streaks(state_data):
    """Longest consecutive disruption streaks per line (in minutes)."""
    lines = defaultdict(list)
    for r in state_data:
        lines[r["PartitionKey"]].append(r)

    streak_data = {}
    for line, records in lines.items():
        sorted_records = sorted(records, key=lambda x: x.get("timestamp", ""))
        max_streak = 0
        current_streak = 0
        streaks = []
        for r in sorted_records:
            if r["previous_status"] in DISRUPTION_STATUSES:
                current_streak += 2
            else:
                if current_streak > 0:
                    streaks.append(current_streak)
                current_streak = 0
        if current_streak > 0:
            streaks.append(current_streak)
        streak_data[line] = {
            "max_minutes": max(streaks) if streaks else 0,
            "avg_minutes": round(sum(streaks) / len(streaks), 1) if streaks else 0,
            "count": len(streaks),
            "distribution": Counter(min(s // 10 * 10, 120) for s in streaks),
        }
    return streak_data


def analyse_weekly_trend(state_data):
    """Weekly disruption % over time for trend analysis."""
    lines = defaultdict(lambda: defaultdict(lambda: {"total": 0, "disrupted": 0}))
    for r in state_data:
        if r["previous_status"] == "Service Closed":
            continue
        try:
            ts = datetime.fromisoformat(r["timestamp"])
        except (ValueError, KeyError):
            continue
        week = ts.strftime("%Y-W%W")
        lines[r["PartitionKey"]][week]["total"] += 1
        if r["previous_status"] in DISRUPTION_STATUSES:
            lines[r["PartitionKey"]][week]["disrupted"] += 1

    result = {}
    for line, weeks in lines.items():
        week_list = []
        for week in sorted(weeks.keys()):
            data = weeks[week]
            pct = round(data["disrupted"] / data["total"] * 100, 1) if data["total"] else 0
            week_list.append({"week": week, "pct": pct, "total": data["total"]})
        result[line] = week_list
    return result


# ── HTML Generation (v1 – dark theme) ───────────────────────────────────────
def generate_html(
    reliability, heatmap, correlation, causes, causes_by_line,
    daily_disruption, line_daily, peak_offpeak, escalations,
    streaks, weekly_trend
):
    # Prepare all data as JSON for embedding
    all_days = sorted(daily_disruption.keys())
    calendar_data = [{"date": d, "value": daily_disruption[d]} for d in all_days]

    # Causes sorted
    sorted_causes = sorted(causes.items(), key=lambda x: -x[1])
    cause_labels = [c[0] for c in sorted_causes]
    cause_values = [c[1] for c in sorted_causes]

    # Weekly trend lines
    all_weeks = sorted(set(w["week"] for line_weeks in weekly_trend.values() for w in line_weeks))

    # Streak distribution data
    streak_bins = ["0-10", "10-20", "20-30", "30-40", "40-50", "50-60", "60-90", "90-120", "120+"]
    streak_bin_ranges = [0, 10, 20, 30, 40, 50, 60, 90, 120]

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TFL Line Status Intelligence Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-chart-matrix@2.0.1/dist/chartjs-chart-matrix.min.js"></script>
<style>
  :root {{
    --bg: #0a0e17;
    --card: #131a2b;
    --border: #1e2a42;
    --text: #e0e6f0;
    --muted: #7b8ba8;
    --accent: #3b82f6;
    --met: #9B0056;
    --bak: #B36305;
    --cir: #FFD300;
    --hc: #F3A9BB;
  }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: 'Segoe UI', -apple-system, BlinkMacSystemFont, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.6;
    padding: 0;
  }}
  .header {{
    background: linear-gradient(135deg, #1a1f35 0%, #0d1117 100%);
    border-bottom: 1px solid var(--border);
    padding: 2rem 2rem 1.5rem;
  }}
  .header h1 {{
    font-size: 1.8rem;
    font-weight: 700;
    letter-spacing: -0.5px;
    margin-bottom: 0.3rem;
  }}
  .header h1 span {{ color: var(--accent); }}
  .header .subtitle {{
    color: var(--muted);
    font-size: 0.9rem;
  }}
  .dashboard {{
    max-width: 1400px;
    margin: 0 auto;
    padding: 1.5rem;
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 1.5rem;
  }}
  .card {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 1.5rem;
    position: relative;
  }}
  .card.full {{ grid-column: 1 / -1; }}
  .card h2 {{
    font-size: 1rem;
    font-weight: 600;
    margin-bottom: 0.3rem;
    color: var(--text);
  }}
  .card .card-desc {{
    font-size: 0.78rem;
    color: var(--muted);
    margin-bottom: 1rem;
  }}
  .card canvas {{ max-height: 400px; }}

  /* Reliability KPI row */
  .kpi-row {{
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 1rem;
    grid-column: 1 / -1;
  }}
  .kpi {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 1.2rem;
    text-align: center;
    position: relative;
    overflow: hidden;
  }}
  .kpi::before {{
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 3px;
  }}
  .kpi.met::before {{ background: var(--met); }}
  .kpi.bak::before {{ background: var(--bak); }}
  .kpi.cir::before {{ background: var(--cir); }}
  .kpi.hc::before {{ background: var(--hc); }}
  .kpi .line-name {{ font-size: 0.8rem; color: var(--muted); text-transform: uppercase; letter-spacing: 1px; }}
  .kpi .big-num {{ font-size: 2.4rem; font-weight: 700; margin: 0.3rem 0; }}
  .kpi .label {{ font-size: 0.75rem; color: var(--muted); }}
  .kpi.met .big-num {{ color: var(--met); }}
  .kpi.bak .big-num {{ color: var(--bak); }}
  .kpi.cir .big-num {{ color: #bfa200; }}
  .kpi.hc .big-num {{ color: var(--hc); }}

  /* Calendar heatmap */
  .calendar-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, 22px);
    gap: 3px;
    margin-top: 0.5rem;
  }}
  .cal-cell {{
    width: 20px; height: 20px;
    border-radius: 3px;
    position: relative;
    cursor: pointer;
  }}
  .cal-cell:hover::after {{
    content: attr(data-tip);
    position: absolute;
    bottom: 110%;
    left: 50%;
    transform: translateX(-50%);
    background: #1e2a42;
    color: #e0e6f0;
    padding: 4px 8px;
    border-radius: 4px;
    font-size: 0.7rem;
    white-space: nowrap;
    z-index: 10;
    pointer-events: none;
  }}
  .cal-label {{
    font-size: 0.65rem;
    color: var(--muted);
    text-align: center;
    line-height: 20px;
  }}
  .legend {{
    display: flex;
    align-items: center;
    gap: 4px;
    margin-top: 0.7rem;
    font-size: 0.7rem;
    color: var(--muted);
  }}
  .legend-swatch {{
    width: 14px; height: 14px;
    border-radius: 2px;
  }}

  /* Correlation matrix */
  .corr-table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 0.8rem;
    margin-top: 0.5rem;
  }}
  .corr-table th, .corr-table td {{
    padding: 0.6rem;
    text-align: center;
    border: 1px solid var(--border);
  }}
  .corr-table th {{
    color: var(--muted);
    font-weight: 500;
    font-size: 0.7rem;
  }}

  /* Insight boxes */
  .insight {{
    background: rgba(59,130,246,0.08);
    border-left: 3px solid var(--accent);
    padding: 0.8rem 1rem;
    margin-top: 1rem;
    border-radius: 0 6px 6px 0;
    font-size: 0.82rem;
  }}
  .insight strong {{ color: var(--accent); }}

  @media (max-width: 900px) {{
    .dashboard {{ grid-template-columns: 1fr; }}
    .kpi-row {{ grid-template-columns: repeat(2, 1fr); }}
  }}
</style>
</head>
<body>

<div class="header">
  <h1>TFL <span>Line Status Intelligence</span></h1>
  <div class="subtitle">Real-time monitoring data from {all_days[0] if all_days else '?'} to {all_days[-1] if all_days else '?'} &middot; {sum(r['total_checks'] for r in reliability.values()):,} status checks across 4 lines</div>
</div>

<div class="dashboard">

  <!-- KPI Row -->
  <div class="kpi-row">
    <div class="kpi met">
      <div class="line-name">Metropolitan</div>
      <div class="big-num">{reliability.get('metropolitan', {}).get('good_pct', 0)}%</div>
      <div class="label">Service reliability</div>
    </div>
    <div class="kpi bak">
      <div class="line-name">Bakerloo</div>
      <div class="big-num">{reliability.get('bakerloo', {}).get('good_pct', 0)}%</div>
      <div class="label">Service reliability</div>
    </div>
    <div class="kpi cir">
      <div class="line-name">Circle</div>
      <div class="big-num">{reliability.get('circle', {}).get('good_pct', 0)}%</div>
      <div class="label">Service reliability</div>
    </div>
    <div class="kpi hc">
      <div class="line-name">Hammersmith & City</div>
      <div class="big-num">{reliability.get('hammersmith-city', {}).get('good_pct', 0)}%</div>
      <div class="label">Service reliability</div>
    </div>
  </div>

  <!-- Weekly Trend -->
  <div class="card full">
    <h2>📈 Disruption Trend by Week</h2>
    <div class="card-desc">Percentage of operating time spent in disrupted status, week-over-week. Reveals improving or deteriorating patterns.</div>
    <canvas id="weeklyTrend"></canvas>
  </div>

  <!-- Hourly Heatmap -->
  <div class="card full">
    <h2>🔥 Disruption Risk Heatmap — Hour × Day of Week</h2>
    <div class="card-desc">Probability of disruption (%) at each hour on each weekday. Darker = more likely to experience delays.</div>
    <div style="display:grid; grid-template-columns: 1fr 1fr; gap: 1rem;">
      <canvas id="heatmapMet"></canvas>
      <canvas id="heatmapBak"></canvas>
      <canvas id="heatmapCir"></canvas>
      <canvas id="heatmapHC"></canvas>
    </div>
  </div>

  <!-- Delay Causes -->
  <div class="card">
    <h2>🔍 Root Cause Analysis</h2>
    <div class="card-desc">What's actually breaking? Delay reasons categorised from {sum(cause_values)} incident reports.</div>
    <canvas id="causes"></canvas>
  </div>

  <!-- Peak vs Off-Peak -->
  <div class="card">
    <h2>🚇 Rush Hour Impact</h2>
    <div class="card-desc">Are commuters disproportionately affected? Disruption rates during AM peak, PM peak, and off-peak.</div>
    <canvas id="peakOffpeak"></canvas>
  </div>

  <!-- Cross-Line Correlation -->
  <div class="card">
    <h2>🔗 Cross-Line Disruption Cascade</h2>
    <div class="card-desc">When Line A is disrupted, how often is Line B simultaneously disrupted? Reveals infrastructure dependencies.</div>
    <table class="corr-table" id="corrTable"></table>
  </div>

  <!-- Disruption Streak Duration -->
  <div class="card">
    <h2>⏱️ Disruption Duration Distribution</h2>
    <div class="card-desc">How long do disruptions last once they start? Shows the spread of continuous disruption episodes.</div>
    <canvas id="streakDist"></canvas>
  </div>

  <!-- Calendar Heatmap -->
  <div class="card full">
    <h2>📅 Daily Disruption Calendar</h2>
    <div class="card-desc">Each cell = one day. Colour intensity shows total disruption minutes across all lines. Hover for details.</div>
    <div id="calendarHeatmap"></div>
    <div class="legend">
      <span>Less</span>
      <div class="legend-swatch" style="background:#0e1726"></div>
      <div class="legend-swatch" style="background:#1a3a5c"></div>
      <div class="legend-swatch" style="background:#2563eb"></div>
      <div class="legend-swatch" style="background:#f59e0b"></div>
      <div class="legend-swatch" style="background:#ef4444"></div>
      <span>More</span>
    </div>
  </div>

  <!-- Severity Escalation -->
  <div class="card">
    <h2>⚡ Severity Escalation Patterns</h2>
    <div class="card-desc">How often do minor delays escalate to severe? Shows transitions between disruption levels.</div>
    <canvas id="escalation"></canvas>
  </div>

  <!-- Stacked area: disruption by type over time -->
  <div class="card">
    <h2>📊 Disruption Composition by Line</h2>
    <div class="card-desc">Breakdown of disruption types for each line, showing what fraction is minor vs severe vs suspension.</div>
    <canvas id="composition"></canvas>
  </div>

</div>

<script>
// ── Data ─────────────────────────────────────────────────────────────────
const LINE_COLORS = {json.dumps(LINE_COLORS)};
const LINE_LABELS = {json.dumps(LINE_LABELS)};
const weeklyTrend = {json.dumps(weekly_trend)};
const heatmapData = {json.dumps(heatmap)};
const causeLabels = {json.dumps(cause_labels)};
const causeValues = {json.dumps(cause_values)};
const causesByLine = {json.dumps(causes_by_line)};
const peakOffpeak = {json.dumps(peak_offpeak)};
const correlation = {json.dumps(correlation)};
const calendarData = {json.dumps(calendar_data)};
const lineDailyData = {json.dumps({k: dict(v) for k, v in line_daily.items()})};
const streakData = {json.dumps({k: {kk: vv for kk, vv in v.items() if kk != 'distribution'} for k, v in streaks.items()})};
const streakDist = {json.dumps({k: dict(v['distribution']) for k, v in streaks.items()})};
const escalations = {json.dumps(escalations)};
const reliability = {json.dumps(reliability)};
const allWeeks = {json.dumps(all_weeks)};

Chart.defaults.color = '#7b8ba8';
Chart.defaults.borderColor = '#1e2a42';
Chart.defaults.font.family = "'Segoe UI', sans-serif";

// ── Weekly Trend Chart ─────────────────────────────────────────────────
new Chart(document.getElementById('weeklyTrend'), {{
  type: 'line',
  data: {{
    labels: allWeeks.map(w => w.replace('2026-', '')),
    datasets: Object.entries(weeklyTrend).map(([line, data]) => {{
      const weekMap = Object.fromEntries(data.map(d => [d.week, d.pct]));
      return {{
        label: LINE_LABELS[line],
        data: allWeeks.map(w => weekMap[w] || 0),
        borderColor: LINE_COLORS[line],
        backgroundColor: LINE_COLORS[line] + '20',
        fill: false,
        tension: 0.3,
        pointRadius: 3,
        borderWidth: 2
      }};
    }})
  }},
  options: {{
    responsive: true,
    plugins: {{
      legend: {{ position: 'top', labels: {{ usePointStyle: true, padding: 15 }} }},
      tooltip: {{ callbacks: {{ label: ctx => ctx.dataset.label + ': ' + ctx.raw + '% disrupted' }} }}
    }},
    scales: {{
      y: {{ title: {{ display: true, text: 'Disruption %' }}, beginAtZero: true }},
      x: {{ title: {{ display: true, text: 'Week' }} }}
    }}
  }}
}});

// ── Heatmap Charts ─────────────────────────────────────────────────────
const dayLabels = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri'];
const hourLabels = Array.from({{length: 19}}, (_, i) => (i + 5).toString().padStart(2, '0') + ':00');

function createHeatmap(canvasId, line) {{
  const data = heatmapData[line] || [];
  const maxVal = Math.max(...data.map(d => d.v), 1);
  const ctx = document.getElementById(canvasId);
  new Chart(ctx, {{
    type: 'matrix',
    data: {{
      datasets: [{{
        label: LINE_LABELS[line],
        data: data.map(d => ({{ x: d.x - 4, y: d.y + 1, v: d.v }})),
        backgroundColor: (ctx) => {{
          const v = ctx.raw?.v || 0;
          if (v === 0) return '#0e1726';
          const t = Math.min(v / Math.max(maxVal * 0.7, 20), 1);
          if (t < 0.33) return `rgba(37, 99, 235, ${{0.3 + t * 2}})`;
          if (t < 0.66) return `rgba(245, 158, 11, ${{0.5 + (t - 0.33) * 1.5}})`;
          return `rgba(239, 68, 68, ${{0.7 + (t - 0.66) * 0.9}})`;
        }},
        width: (ctx) => (ctx.chart.chartArea?.width || 300) / 19 - 2,
        height: (ctx) => (ctx.chart.chartArea?.height || 150) / 5 - 2
      }}]
    }},
    options: {{
      responsive: true,
      plugins: {{
        legend: {{ display: false }},
        title: {{ display: true, text: LINE_LABELS[line], color: LINE_COLORS[line], font: {{ size: 13 }} }},
        tooltip: {{ callbacks: {{ label: ctx => `${{dayLabels[ctx.raw.y - 1]}} ${{(ctx.raw.x + 4).toString().padStart(2, '0')}}:00 — ${{ctx.raw.v}}% disrupted` }} }}
      }},
      scales: {{
        x: {{
          type: 'linear',
          offset: true,
          min: 0.5, max: 19.5,
          ticks: {{ stepSize: 1, callback: (v) => (v + 4).toString().padStart(2, '0') }},
          title: {{ display: true, text: 'Hour' }}
        }},
        y: {{
          type: 'linear',
          offset: true,
          min: 0.5, max: 5.5,
          ticks: {{ stepSize: 1, callback: (v) => dayLabels[v - 1] || '' }},
          reverse: false
        }}
      }}
    }}
  }});
}}

createHeatmap('heatmapMet', 'metropolitan');
createHeatmap('heatmapBak', 'bakerloo');
createHeatmap('heatmapCir', 'circle');
createHeatmap('heatmapHC', 'hammersmith-city');

// ── Cause Analysis ─────────────────────────────────────────────────────
new Chart(document.getElementById('causes'), {{
  type: 'bar',
  data: {{
    labels: causeLabels,
    datasets: [{{
      data: causeValues,
      backgroundColor: causeLabels.map((_, i) => {{
        const colors = ['#3b82f6', '#f59e0b', '#ef4444', '#10b981', '#8b5cf6', '#f97316', '#06b6d4', '#ec4899', '#84cc16', '#6366f1', '#14b8a6', '#e11d48', '#a855f7', '#0ea5e9', '#65a30d', '#d946ef'];
        return colors[i % colors.length];
      }}),
      borderRadius: 4,
      borderSkipped: false
    }}]
  }},
  options: {{
    indexAxis: 'y',
    responsive: true,
    plugins: {{ legend: {{ display: false }}, tooltip: {{ callbacks: {{ label: ctx => ctx.raw + ' incidents' }} }} }},
    scales: {{ x: {{ title: {{ display: true, text: 'Incidents' }}, beginAtZero: true }}, y: {{ ticks: {{ font: {{ size: 11 }} }} }} }}
  }}
}});

// ── Peak vs Off-Peak ───────────────────────────────────────────────────
new Chart(document.getElementById('peakOffpeak'), {{
  type: 'bar',
  data: {{
    labels: Object.keys(LINE_LABELS).map(k => LINE_LABELS[k]),
    datasets: ['AM Peak (07-09)', 'PM Peak (17-19)', 'Off-Peak'].map((period, i) => ({{
      label: period,
      data: Object.keys(LINE_LABELS).map(line => peakOffpeak[line]?.[period] || 0),
      backgroundColor: ['#3b82f6', '#f59e0b', '#1e2a42'][i],
      borderRadius: 4
    }}))
  }},
  options: {{
    responsive: true,
    plugins: {{
      legend: {{ position: 'top', labels: {{ usePointStyle: true, padding: 12 }} }},
      tooltip: {{ callbacks: {{ label: ctx => ctx.dataset.label + ': ' + ctx.raw + '% disrupted' }} }}
    }},
    scales: {{
      y: {{ title: {{ display: true, text: 'Disruption %' }}, beginAtZero: true }},
      x: {{ ticks: {{ font: {{ size: 10 }} }} }}
    }}
  }}
}});

// ── Correlation Table ──────────────────────────────────────────────────
(function() {{
  const lines = ['metropolitan', 'bakerloo', 'circle', 'hammersmith-city'];
  const table = document.getElementById('corrTable');
  let html = '<tr><th></th>' + lines.map(l => '<th>' + LINE_LABELS[l] + '</th>').join('') + '</tr>';
  lines.forEach(a => {{
    html += '<tr><th style="text-align:left">' + LINE_LABELS[a] + '</th>';
    lines.forEach(b => {{
      if (a === b) {{
        html += '<td style="background:#1e2a42;color:#7b8ba8">—</td>';
      }} else {{
        const val = correlation[a]?.[b] || 0;
        const intensity = Math.min(val / 50, 1);
        const bg = `rgba(239, 68, 68, ${{intensity * 0.5}})`;
        html += `<td style="background:${{bg}};font-weight:600">${{val}}%</td>`;
      }}
    }});
    html += '</tr>';
  }});
  table.innerHTML = html;
}})();

// ── Streak Distribution ────────────────────────────────────────────────
(function() {{
  const bins = ['0', '10', '20', '30', '40', '50', '60', '90', '120'];
  const binLabels = ['0-10m', '10-20m', '20-30m', '30-40m', '40-50m', '50-60m', '60-90m', '90-120m', '120m+'];
  const lines = ['metropolitan', 'bakerloo', 'circle', 'hammersmith-city'];

  new Chart(document.getElementById('streakDist'), {{
    type: 'bar',
    data: {{
      labels: binLabels,
      datasets: lines.map(line => ({{
        label: LINE_LABELS[line],
        data: bins.map(b => streakDist[line]?.[b] || 0),
        backgroundColor: LINE_COLORS[line] + 'cc',
        borderRadius: 3
      }}))
    }},
    options: {{
      responsive: true,
      plugins: {{
        legend: {{ position: 'top', labels: {{ usePointStyle: true, padding: 12 }} }},
        tooltip: {{ callbacks: {{ label: ctx => ctx.dataset.label + ': ' + ctx.raw + ' episodes' }} }}
      }},
      scales: {{
        y: {{ title: {{ display: true, text: 'Episodes' }}, beginAtZero: true }},
        x: {{ title: {{ display: true, text: 'Duration' }} }}
      }}
    }}
  }});
}})();

// ── Calendar Heatmap ───────────────────────────────────────────────────
(function() {{
  const container = document.getElementById('calendarHeatmap');
  if (!calendarData.length) return;

  const maxVal = Math.max(...calendarData.map(d => d.value));
  const getColor = (v) => {{
    if (v === 0) return '#0e1726';
    const t = v / maxVal;
    if (t < 0.15) return '#0e1726';
    if (t < 0.3) return '#1a3a5c';
    if (t < 0.5) return '#2563eb';
    if (t < 0.75) return '#f59e0b';
    return '#ef4444';
  }};

  // Group by week
  const weeks = {{}};
  calendarData.forEach(d => {{
    const dt = new Date(d.date);
    const weekStart = new Date(dt);
    weekStart.setDate(dt.getDate() - dt.getDay() + 1);
    const key = weekStart.toISOString().slice(0, 10);
    if (!weeks[key]) weeks[key] = [];
    weeks[key].push(d);
  }});

  let html = '<div style="display:flex;gap:4px;flex-wrap:wrap;align-items:flex-start">';
  const weekKeys = Object.keys(weeks).sort();

  // Month labels
  let currentMonth = '';
  weekKeys.forEach(wk => {{
    const month = wk.slice(0, 7);
    if (month !== currentMonth) {{
      currentMonth = month;
      const monthName = new Date(wk).toLocaleString('en', {{ month: 'short' }});
    }}
  }});

  weekKeys.forEach(wk => {{
    html += '<div style="display:flex;flex-direction:column;gap:3px">';
    const days = weeks[wk].sort((a, b) => a.date.localeCompare(b.date));
    // Pad to 7 days
    const dayMap = Object.fromEntries(days.map(d => [new Date(d.date).getDay(), d]));
    for (let dow = 1; dow <= 5; dow++) {{
      const d = dayMap[dow];
      if (d) {{
        const lineBreakdown = Object.entries(lineDailyData).map(([line, data]) => {{
          const mins = data[d.date] || 0;
          return mins > 0 ? LINE_LABELS[line] + ': ' + mins + 'm' : '';
        }}).filter(Boolean).join(', ');
        html += `<div class="cal-cell" style="background:${{getColor(d.value)}}" data-tip="${{d.date}}: ${{d.value}}m total (${{lineBreakdown || 'No disruption'}})"></div>`;
      }} else {{
        html += '<div style="width:20px;height:20px"></div>';
      }}
    }}
    html += '</div>';
  }});
  html += '</div>';
  container.innerHTML = html;
}})();

// ── Escalation Chart ───────────────────────────────────────────────────
(function() {{
  const allTransitions = {{}};
  Object.entries(escalations).forEach(([line, trans]) => {{
    Object.entries(trans).forEach(([t, count]) => {{
      if (!allTransitions[t]) allTransitions[t] = {{}};
      allTransitions[t][line] = count;
    }});
  }});

  const transLabels = Object.keys(allTransitions);
  const lines = ['metropolitan', 'bakerloo', 'circle', 'hammersmith-city'];

  new Chart(document.getElementById('escalation'), {{
    type: 'bar',
    data: {{
      labels: transLabels,
      datasets: lines.map(line => ({{
        label: LINE_LABELS[line],
        data: transLabels.map(t => allTransitions[t]?.[line] || 0),
        backgroundColor: LINE_COLORS[line] + 'cc',
        borderRadius: 3
      }}))
    }},
    options: {{
      responsive: true,
      indexAxis: 'y',
      plugins: {{
        legend: {{ position: 'top', labels: {{ usePointStyle: true, padding: 12 }} }},
        tooltip: {{ callbacks: {{ label: ctx => ctx.dataset.label + ': ' + ctx.raw + ' transitions' }} }}
      }},
      scales: {{
        x: {{ title: {{ display: true, text: 'Occurrences' }}, beginAtZero: true, stacked: true }},
        y: {{ ticks: {{ font: {{ size: 10 }} }}, stacked: true }}
      }}
    }}
  }});
}})();

// ── Disruption Composition by Line ─────────────────────────────────────
(function() {{
  const lines = ['metropolitan', 'bakerloo', 'circle', 'hammersmith-city'];
  const statuses = ['Minor Delays', 'Severe Delays', 'Part Suspended', 'Part Closure', 'Suspended'];
  const statusColors = {{
    'Minor Delays': '#f59e0b',
    'Severe Delays': '#ef4444',
    'Part Suspended': '#dc2626',
    'Part Closure': '#7c3aed',
    'Suspended': '#991b1b'
  }};

  new Chart(document.getElementById('composition'), {{
    type: 'bar',
    data: {{
      labels: lines.map(l => LINE_LABELS[l]),
      datasets: statuses.map(status => ({{
        label: status,
        data: lines.map(line => reliability[line]?.breakdown?.[status] || 0),
        backgroundColor: statusColors[status],
        borderRadius: 2
      }}))
    }},
    options: {{
      responsive: true,
      plugins: {{
        legend: {{ position: 'top', labels: {{ usePointStyle: true, padding: 12 }} }},
        tooltip: {{ callbacks: {{ label: ctx => ctx.dataset.label + ': ' + ctx.raw + '%' }} }}
      }},
      scales: {{
        x: {{ stacked: true }},
        y: {{ stacked: true, title: {{ display: true, text: 'Disrupted time %' }} }}
      }}
    }}
  }});
}})();
</script>
</body>
</html>"""
    return html


# ── HTML Generation (v2 – Clean Design System) ─────────────────────────────
def generate_html_v2(payload, data_file_name=DASHBOARD_DATA_FILE):
    reliability = payload["reliability"]
    total_checks = payload["totalChecks"]
    total_delays = payload["totalDelays"]
    met_pct = reliability.get("metropolitan", {}).get("good_pct", 0)
    bak_pct = reliability.get("bakerloo", {}).get("good_pct", 0)
    cir_pct = reliability.get("circle", {}).get("good_pct", 0)
    hc_pct = reliability.get("hammersmith-city", {}).get("good_pct", 0)
    date_from = payload["dateFrom"]
    date_to = payload["dateTo"]

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TFL Line Status Intelligence</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@500;600;700&family=Roboto:wght@300;400;500;600&family=Inconsolata:wght@400;500&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-chart-matrix@2.0.1/dist/chartjs-chart-matrix.min.js"></script>
<style>
  :root {{
    --primary: #3B82F6;
    --surface: #FFFFFF;
    --surface-alt: #F9FAFB;
    --border: #E5E7EB;
    --border-light: #F3F4F6;
    --text: #111827;
    --text-secondary: #6B7280;
    --text-muted: #9CA3AF;
    --radius: 8px;
    --shadow-sm: 0 1px 2px rgba(0,0,0,0.04);
    --met: #9B0056;
    --bak: #B36305;
    --cir: #D4A800;
    --hc: #D77DA0;
  }}
  *, *::before, *::after {{ margin: 0; padding: 0; box-sizing: border-box; }}
  html {{ font-size: 16px; -webkit-font-smoothing: antialiased; }}
  body {{
    font-family: 'Roboto', -apple-system, BlinkMacSystemFont, sans-serif;
    background: var(--surface-alt);
    color: var(--text);
    line-height: 1.5;
    overflow-x: hidden;
  }}
  .header {{
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 32px 32px 24px;
  }}
  .header-inner {{
    max-width: 1120px;
    margin: 0 auto;
  }}
  .header h1 {{
    font-family: 'Poppins', sans-serif;
    font-size: 24px;
    font-weight: 600;
    color: var(--text);
    letter-spacing: -0.3px;
  }}
  .header h1 span {{ color: var(--primary); }}
  .header p {{
    font-size: 14px;
    color: var(--text-secondary);
    margin-top: 4px;
  }}
  .meta-chips {{
    display: flex;
    gap: 8px;
    margin-top: 16px;
    flex-wrap: wrap;
  }}
  .chip {{
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 4px 12px;
    background: var(--surface-alt);
    border: 1px solid var(--border);
    border-radius: 100px;
    font-size: 12px;
    color: var(--text-secondary);
    font-weight: 500;
  }}
  .chip .num {{
    font-family: 'Inconsolata', monospace;
    font-weight: 500;
    color: var(--text);
  }}
  .dashboard {{
    max-width: 1120px;
    margin: 0 auto;
    padding: 24px;
    display: flex;
    flex-direction: column;
    gap: 24px;
  }}
  .row {{
    display: grid;
    gap: 24px;
  }}
  .row.cols-2 {{ grid-template-columns: 1fr 1fr; }}
  .row.cols-4 {{ grid-template-columns: repeat(4, 1fr); }}
  .card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 24px;
    box-shadow: var(--shadow-sm);
  }}
  .card h2 {{
    font-family: 'Poppins', sans-serif;
    font-size: 14px;
    font-weight: 600;
    color: var(--text);
    margin-bottom: 2px;
  }}
  .card .desc {{
    font-size: 12px;
    color: var(--text-muted);
    margin-bottom: 16px;
    line-height: 1.4;
  }}
  .card canvas {{
    width: 100% !important;
    max-height: 320px;
  }}
  .kpi {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 20px 24px;
    box-shadow: var(--shadow-sm);
    position: relative;
    overflow: hidden;
  }}
  .kpi::after {{
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 3px;
  }}
  .kpi.met::after {{ background: var(--met); }}
  .kpi.bak::after {{ background: var(--bak); }}
  .kpi.cir::after {{ background: var(--cir); }}
  .kpi.hc::after {{ background: var(--hc); }}
  .kpi-label {{
    font-size: 12px;
    font-weight: 500;
    color: var(--text-secondary);
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }}
  .kpi-value {{
    font-family: 'Poppins', sans-serif;
    font-size: 32px;
    font-weight: 700;
    line-height: 1.1;
    margin: 4px 0;
  }}
  .kpi.met .kpi-value {{ color: var(--met); }}
  .kpi.bak .kpi-value {{ color: var(--bak); }}
  .kpi.cir .kpi-value {{ color: var(--cir); }}
  .kpi.hc .kpi-value {{ color: var(--hc); }}
  .kpi-sub {{
    font-size: 12px;
    color: var(--text-muted);
  }}
  .heatmap-stack {{
    display: flex;
    flex-direction: column;
    gap: 16px;
  }}
  .heatmap-item {{
    display: flex;
    flex-direction: column;
    gap: 4px;
  }}
  .heatmap-item .hm-label {{
    font-family: 'Poppins', sans-serif;
    font-size: 12px;
    font-weight: 600;
    padding-left: 4px;
  }}
  .heatmap-item canvas {{
    width: 100% !important;
    height: 100px !important;
  }}
  .corr-table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
  }}
  .corr-table th, .corr-table td {{
    padding: 10px 12px;
    text-align: center;
    border-bottom: 1px solid var(--border-light);
  }}
  .corr-table th {{
    font-size: 11px;
    font-weight: 500;
    color: var(--text-secondary);
    text-transform: uppercase;
    letter-spacing: 0.3px;
  }}
  .corr-table td {{
    font-family: 'Inconsolata', monospace;
    font-weight: 500;
  }}
  .cal-wrap {{
    display: flex;
    gap: 3px;
    flex-wrap: wrap;
    align-items: flex-start;
  }}
  .cal-col {{
    display: flex;
    flex-direction: column;
    gap: 3px;
  }}
  .cal-cell {{
    width: 18px;
    height: 18px;
    border-radius: 3px;
    position: relative;
    cursor: pointer;
  }}
  .cal-cell:hover {{ opacity: 0.8; }}
  .cal-cell[data-tip]:hover::after {{
    content: attr(data-tip);
    position: absolute;
    bottom: calc(100% + 6px);
    left: 50%;
    transform: translateX(-50%);
    background: var(--text);
    color: var(--surface);
    padding: 4px 8px;
    border-radius: 4px;
    font-size: 11px;
    white-space: nowrap;
    z-index: 10;
    pointer-events: none;
  }}
  .cal-legend {{
    display: flex;
    align-items: center;
    gap: 4px;
    margin-top: 12px;
    font-size: 11px;
    color: var(--text-muted);
  }}
  .cal-swatch {{
    width: 14px;
    height: 14px;
    border-radius: 3px;
  }}
  @media (max-width: 768px) {{
    .header {{ padding: 24px 16px 20px; }}
    .dashboard {{ padding: 16px; gap: 16px; }}
    .row.cols-2 {{ grid-template-columns: 1fr; }}
    .row.cols-4 {{ grid-template-columns: repeat(2, 1fr); }}
    .kpi-value {{ font-size: 24px; }}
  }}
</style>
</head>
<body>
<div class="header">
  <div class="header-inner">
    <h1>TFL <span>Line Status Intelligence</span></h1>
    <p>Monitoring disruption patterns across London Underground lines</p>
    <div class="meta-chips">
      <div class="chip">Period <span class="num">{date_from} → {date_to}</span></div>
      <div class="chip">Checks <span class="num">{total_checks:,}</span></div>
      <div class="chip">Incidents <span class="num">{total_delays}</span></div>
      <div class="chip">Lines <span class="num">4</span></div>
    </div>
  </div>
</div>
<div class="dashboard">
  <div id="dashboardError" class="card" style="display:none;border-color:#FECACA;background:#FEF2F2;color:#991B1B"></div>
  <div class="row cols-4">
    <div class="kpi met">
      <div class="kpi-label">Metropolitan</div>
      <div class="kpi-value">{met_pct}%</div>
      <div class="kpi-sub">Service reliability</div>
    </div>
    <div class="kpi bak">
      <div class="kpi-label">Bakerloo</div>
      <div class="kpi-value">{bak_pct}%</div>
      <div class="kpi-sub">Service reliability</div>
    </div>
    <div class="kpi cir">
      <div class="kpi-label">Circle</div>
      <div class="kpi-value">{cir_pct}%</div>
      <div class="kpi-sub">Service reliability</div>
    </div>
    <div class="kpi hc">
      <div class="kpi-label">Hammersmith &amp; City</div>
      <div class="kpi-value">{hc_pct}%</div>
      <div class="kpi-sub">Service reliability</div>
    </div>
  </div>
  <div class="row">
    <div class="card">
      <h2>Disruption Trend by Week</h2>
      <div class="desc">Percentage of operating time spent in a disrupted status, week over week.</div>
      <canvas id="weeklyTrend"></canvas>
    </div>
  </div>
  <div class="row">
    <div class="card">
      <h2>Disruption Risk by Hour &amp; Day</h2>
      <div class="desc">Probability of disruption at each hour on each weekday. Darker cells indicate higher risk.</div>
      <div class="heatmap-stack">
        <div class="heatmap-item"><div class="hm-label" style="color:var(--met)">Metropolitan</div><canvas id="hmMet"></canvas></div>
        <div class="heatmap-item"><div class="hm-label" style="color:var(--bak)">Bakerloo</div><canvas id="hmBak"></canvas></div>
        <div class="heatmap-item"><div class="hm-label" style="color:var(--cir)">Circle</div><canvas id="hmCir"></canvas></div>
        <div class="heatmap-item"><div class="hm-label" style="color:var(--hc)">Hammersmith &amp; City</div><canvas id="hmHC"></canvas></div>
      </div>
    </div>
  </div>
  <div class="row cols-2">
    <div class="card">
      <h2>Root Cause Analysis</h2>
      <div class="desc">Delay reasons categorised from {total_delays} incident reports.</div>
      <canvas id="causes"></canvas>
    </div>
    <div class="card">
      <h2>Rush Hour Impact</h2>
      <div class="desc">Disruption rates during AM peak, PM peak, and off-peak hours.</div>
      <canvas id="peakOffpeak"></canvas>
    </div>
  </div>
  <div class="row cols-2">
    <div class="card">
      <h2>Cross-Line Disruption Cascade</h2>
      <div class="desc">When one line is disrupted, how often is another simultaneously affected?</div>
      <table class="corr-table" id="corrTable"></table>
    </div>
    <div class="card">
      <h2>Disruption Duration Distribution</h2>
      <div class="desc">How long do continuous disruption episodes last once they begin?</div>
      <canvas id="streakDist"></canvas>
    </div>
  </div>
  <div class="row">
    <div class="card">
      <h2>Daily Disruption Calendar</h2>
      <div class="desc">Each cell represents one weekday. Colour intensity shows total disruption minutes across all lines.</div>
      <div id="calendarHeatmap" class="cal-wrap"></div>
      <div class="cal-legend">
        <span>Less</span>
        <div class="cal-swatch" style="background:#EFF6FF"></div>
        <div class="cal-swatch" style="background:#BFDBFE"></div>
        <div class="cal-swatch" style="background:#3B82F6"></div>
        <div class="cal-swatch" style="background:#D97706"></div>
        <div class="cal-swatch" style="background:#DC2626"></div>
        <span>More</span>
      </div>
    </div>
  </div>
  <div class="row cols-2">
    <div class="card">
      <h2>Severity Escalation Patterns</h2>
      <div class="desc">How often do delays transition between severity levels?</div>
      <canvas id="escalation"></canvas>
    </div>
    <div class="card">
      <h2>Disruption Composition</h2>
      <div class="desc">Breakdown of disruption types for each line by severity.</div>
      <canvas id="composition"></canvas>
    </div>
  </div>
</div>
<script>
const DATA_URL = {json.dumps(data_file_name)};
const LINE_COLORS = {json.dumps(LINE_COLORS)};
const LINE_LABELS = {json.dumps(LINE_LABELS)};
const dayLabels = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri'];

Chart.defaults.color = '#6B7280';
Chart.defaults.borderColor = '#F3F4F6';
Chart.defaults.font.family = "'Roboto', sans-serif";
Chart.defaults.font.size = 12;

function showError(message) {{
  const errorCard = document.getElementById('dashboardError');
  errorCard.textContent = message;
  errorCard.style.display = 'block';
}}

function renderDashboard(dashboardData) {{
  const weeklyTrend = dashboardData.weeklyTrend || {{}};
  const heatmapData = dashboardData.heatmapData || {{}};
  const causeLabels = dashboardData.causeLabels || [];
  const causeValues = dashboardData.causeValues || [];
  const peakOffpeak = dashboardData.peakOffpeak || {{}};
  const correlation = dashboardData.correlation || {{}};
  const calendarData = dashboardData.calendarData || [];
  const lineDailyData = dashboardData.lineDailyData || {{}};
  const streakDist = dashboardData.streakDist || {{}};
  const escalations = dashboardData.escalations || {{}};
  const reliability = dashboardData.reliability || {{}};
  const allWeeks = dashboardData.allWeeks || [];

  new Chart(document.getElementById('weeklyTrend'), {{
    type: 'line',
    data: {{
      labels: allWeeks.map(w => w.replace(/^\\d{{4}}-/, '')),
      datasets: Object.entries(weeklyTrend).map(([line, data]) => {{
        const weekMap = Object.fromEntries(data.map(d => [d.week, d.pct]));
        return {{
          label: LINE_LABELS[line],
          data: allWeeks.map(w => weekMap[w] || 0),
          borderColor: LINE_COLORS[line],
          backgroundColor: LINE_COLORS[line] + '18',
          fill: false,
          tension: 0.35,
          pointRadius: 2,
          pointHoverRadius: 5,
          borderWidth: 2
        }};
      }})
    }},
    options: {{
      responsive: true,
      maintainAspectRatio: true,
      plugins: {{
        legend: {{ position: 'top', labels: {{ usePointStyle: true, pointStyle: 'circle', padding: 16, font: {{ size: 11 }} }} }},
        tooltip: {{ backgroundColor: '#111827', titleFont: {{ size: 12 }}, bodyFont: {{ size: 12 }}, padding: 10, cornerRadius: 6, callbacks: {{ label: ctx => ctx.dataset.label + ': ' + ctx.raw + '%' }} }}
      }},
      scales: {{
        y: {{ title: {{ display: true, text: 'Disruption %', font: {{ size: 11 }} }}, beginAtZero: true, grid: {{ color: '#F3F4F6' }} }},
        x: {{ grid: {{ display: false }} }}
      }}
    }}
  }});

  function createHeatmap(canvasId, line) {{
    const data = heatmapData[line] || [];
    const maxVal = Math.max(...data.map(d => d.v), 1);
    new Chart(document.getElementById(canvasId), {{
      type: 'matrix',
      data: {{
        datasets: [{{
          data: data.map(d => ({{ x: d.x - 4, y: d.y + 1, v: d.v }})),
          backgroundColor: (ctx) => {{
            const v = ctx.raw?.v || 0;
            if (v === 0) return '#F9FAFB';
            const t = Math.min(v / Math.max(maxVal * 0.7, 20), 1);
            if (t < 0.25) return '#DBEAFE';
            if (t < 0.45) return '#93C5FD';
            if (t < 0.65) return '#3B82F6';
            if (t < 0.8) return '#D97706';
            return '#DC2626';
          }},
          borderColor: '#FFFFFF',
          borderWidth: 1.5,
          width: (ctx) => (ctx.chart.chartArea?.width || 300) / 19 - 1,
          height: (ctx) => (ctx.chart.chartArea?.height || 80) / 5 - 1
        }}]
      }},
      options: {{
        responsive: true,
        maintainAspectRatio: false,
        plugins: {{
          legend: {{ display: false }},
          tooltip: {{ backgroundColor: '#111827', cornerRadius: 6, padding: 8, callbacks: {{ title: () => '', label: ctx => `${{dayLabels[ctx.raw.y - 1]}} ${{String(ctx.raw.x + 4).padStart(2, '0')}}:00 — ${{ctx.raw.v}}%` }} }}
        }},
        scales: {{
          x: {{
            type: 'linear', offset: true, min: 0.5, max: 19.5,
            ticks: {{ stepSize: 2, font: {{ size: 10 }}, callback: v => String(v + 4).padStart(2, '0') }},
            grid: {{ display: false }}
          }},
          y: {{
            type: 'linear', offset: true, min: 0.5, max: 5.5,
            ticks: {{ stepSize: 1, font: {{ size: 10 }}, callback: v => dayLabels[v - 1] || '' }},
            grid: {{ display: false }}
          }}
        }}
      }}
    }});
  }}

  createHeatmap('hmMet', 'metropolitan');
  createHeatmap('hmBak', 'bakerloo');
  createHeatmap('hmCir', 'circle');
  createHeatmap('hmHC', 'hammersmith-city');

  new Chart(document.getElementById('causes'), {{
    type: 'bar',
    data: {{
      labels: causeLabels,
      datasets: [{{ data: causeValues, backgroundColor: '#3B82F6', hoverBackgroundColor: '#2563EB', borderRadius: 4, borderSkipped: false, barPercentage: 0.7 }}]
    }},
    options: {{
      indexAxis: 'y',
      responsive: true,
      plugins: {{ legend: {{ display: false }}, tooltip: {{ backgroundColor: '#111827', cornerRadius: 6, callbacks: {{ label: ctx => ctx.raw + ' incidents' }} }} }},
      scales: {{ x: {{ beginAtZero: true, grid: {{ color: '#F3F4F6' }} }}, y: {{ grid: {{ display: false }}, ticks: {{ font: {{ size: 11 }} }} }} }}
    }}
  }});

  new Chart(document.getElementById('peakOffpeak'), {{
    type: 'bar',
    data: {{
      labels: Object.keys(LINE_LABELS).map(k => LINE_LABELS[k]),
      datasets: [
        {{ label: 'AM Peak (07–09)', data: Object.keys(LINE_LABELS).map(l => peakOffpeak[l]?.['AM Peak (07-09)'] || 0), backgroundColor: '#3B82F6', borderRadius: 4 }},
        {{ label: 'PM Peak (17–19)', data: Object.keys(LINE_LABELS).map(l => peakOffpeak[l]?.['PM Peak (17-19)'] || 0), backgroundColor: '#D97706', borderRadius: 4 }},
        {{ label: 'Off-Peak', data: Object.keys(LINE_LABELS).map(l => peakOffpeak[l]?.['Off-Peak'] || 0), backgroundColor: '#E5E7EB', borderRadius: 4 }}
      ]
    }},
    options: {{
      responsive: true,
      plugins: {{
        legend: {{ position: 'top', labels: {{ usePointStyle: true, pointStyle: 'circle', padding: 12, font: {{ size: 11 }} }} }},
        tooltip: {{ backgroundColor: '#111827', cornerRadius: 6, callbacks: {{ label: ctx => ctx.dataset.label + ': ' + ctx.raw + '%' }} }}
      }},
      scales: {{ y: {{ title: {{ display: true, text: 'Disruption %', font: {{ size: 11 }} }}, beginAtZero: true, grid: {{ color: '#F3F4F6' }} }}, x: {{ grid: {{ display: false }}, ticks: {{ font: {{ size: 10 }} }} }} }}
    }}
  }});

  (function() {{
    const lines = ['metropolitan', 'bakerloo', 'circle', 'hammersmith-city'];
    const table = document.getElementById('corrTable');
    let h = '<thead><tr><th></th>' + lines.map(l => '<th>' + LINE_LABELS[l] + '</th>').join('') + '</tr></thead><tbody>';
    lines.forEach(a => {{
      h += '<tr><th style="text-align:left;font-weight:500">' + LINE_LABELS[a] + '</th>';
      lines.forEach(b => {{
        if (a === b) {{
          h += '<td style="color:#D1D5DB">—</td>';
        }} else {{
          const val = correlation[a]?.[b] || 0;
          const intensity = Math.min(val / 50, 1);
          h += `<td style="background:rgba(220,38,38,${{intensity * 0.15}});font-weight:600">${{val}}%</td>`;
        }}
      }});
      h += '</tr>';
    }});
    h += '</tbody>';
    table.innerHTML = h;
  }})();

  (function() {{
    const bins = ['0', '10', '20', '30', '40', '50', '60', '90', '120'];
    const binLabels = ['0–10', '10–20', '20–30', '30–40', '40–50', '50–60', '60–90', '90–120', '120+'];
    const lines = ['metropolitan', 'bakerloo', 'circle', 'hammersmith-city'];
    new Chart(document.getElementById('streakDist'), {{
      type: 'bar',
      data: {{
        labels: binLabels,
        datasets: lines.map(line => ({{ label: LINE_LABELS[line], data: bins.map(b => streakDist[line]?.[b] || 0), backgroundColor: LINE_COLORS[line] + 'cc', borderRadius: 3 }}))
      }},
      options: {{
        responsive: true,
        plugins: {{
          legend: {{ position: 'top', labels: {{ usePointStyle: true, pointStyle: 'circle', padding: 12, font: {{ size: 11 }} }} }},
          tooltip: {{ backgroundColor: '#111827', cornerRadius: 6, callbacks: {{ label: ctx => ctx.dataset.label + ': ' + ctx.raw + ' episodes' }} }}
        }},
        scales: {{
          y: {{ title: {{ display: true, text: 'Episodes', font: {{ size: 11 }} }}, beginAtZero: true, grid: {{ color: '#F3F4F6' }} }},
          x: {{ title: {{ display: true, text: 'Duration (minutes)', font: {{ size: 11 }} }}, grid: {{ display: false }} }}
        }}
      }}
    }});
  }})();

  (function() {{
    const container = document.getElementById('calendarHeatmap');
    if (!calendarData.length) return;
    const maxVal = Math.max(...calendarData.map(d => d.value));
    const getColor = v => {{
      if (v === 0) return '#F3F4F6';
      const t = v / maxVal;
      if (t < 0.15) return '#EFF6FF';
      if (t < 0.3) return '#BFDBFE';
      if (t < 0.5) return '#3B82F6';
      if (t < 0.75) return '#D97706';
      return '#DC2626';
    }};
    const weeks = {{}};
    calendarData.forEach(d => {{
      const dt = new Date(d.date);
      const ws = new Date(dt);
      ws.setDate(dt.getDate() - dt.getDay() + 1);
      const key = ws.toISOString().slice(0, 10);
      if (!weeks[key]) weeks[key] = [];
      weeks[key].push(d);
    }});
    const wkKeys = Object.keys(weeks).sort();
    let html = '';
    wkKeys.forEach(wk => {{
      html += '<div class="cal-col">';
      const dayMap = Object.fromEntries(weeks[wk].map(d => [new Date(d.date).getDay(), d]));
      for (let dow = 1; dow <= 5; dow++) {{
        const d = dayMap[dow];
        if (d) {{
          const lineInfo = Object.entries(lineDailyData).map(([line, data]) => {{
            const minutes = data[d.date] || 0;
            return minutes > 0 ? LINE_LABELS[line] + ': ' + minutes + 'm' : '';
          }}).filter(Boolean).join(', ');
          html += `<div class="cal-cell" style="background:${{getColor(d.value)}}" data-tip="${{d.date}} — ${{d.value}}m (${{lineInfo || 'None'}})"></div>`;
        }} else {{
          html += '<div style="width:18px;height:18px"></div>';
        }}
      }}
      html += '</div>';
    }});
    container.innerHTML = html;
  }})();

  (function() {{
    const allTrans = {{}};
    Object.entries(escalations).forEach(([line, transitions]) => {{
      Object.entries(transitions).forEach(([transition, count]) => {{
        if (!allTrans[transition]) allTrans[transition] = {{}};
        allTrans[transition][line] = count;
      }});
    }});
    const transitionLabels = Object.keys(allTrans);
    const lines = ['metropolitan', 'bakerloo', 'circle', 'hammersmith-city'];
    new Chart(document.getElementById('escalation'), {{
      type: 'bar',
      data: {{
        labels: transitionLabels,
        datasets: lines.map(line => ({{ label: LINE_LABELS[line], data: transitionLabels.map(t => allTrans[t]?.[line] || 0), backgroundColor: LINE_COLORS[line] + 'cc', borderRadius: 3 }}))
      }},
      options: {{
        responsive: true,
        indexAxis: 'y',
        plugins: {{
          legend: {{ position: 'top', labels: {{ usePointStyle: true, pointStyle: 'circle', padding: 12, font: {{ size: 11 }} }} }},
          tooltip: {{ backgroundColor: '#111827', cornerRadius: 6, callbacks: {{ label: ctx => ctx.dataset.label + ': ' + ctx.raw }} }}
        }},
        scales: {{
          x: {{ beginAtZero: true, stacked: true, grid: {{ color: '#F3F4F6' }} }},
          y: {{ stacked: true, grid: {{ display: false }}, ticks: {{ font: {{ size: 10 }} }} }}
        }}
      }}
    }});
  }})();

  (function() {{
    const lines = ['metropolitan', 'bakerloo', 'circle', 'hammersmith-city'];
    const statuses = ['Minor Delays', 'Severe Delays', 'Part Suspended', 'Part Closure', 'Suspended'];
    const colors = {{ 'Minor Delays': '#D97706', 'Severe Delays': '#DC2626', 'Part Suspended': '#B91C1C', 'Part Closure': '#8B5CF6', 'Suspended': '#7C2D12' }};
    new Chart(document.getElementById('composition'), {{
      type: 'bar',
      data: {{
        labels: lines.map(l => LINE_LABELS[l]),
        datasets: statuses.map(status => ({{ label: status, data: lines.map(line => reliability[line]?.breakdown?.[status] || 0), backgroundColor: colors[status], borderRadius: 2 }}))
      }},
      options: {{
        responsive: true,
        plugins: {{
          legend: {{ position: 'top', labels: {{ usePointStyle: true, pointStyle: 'circle', padding: 12, font: {{ size: 11 }} }} }},
          tooltip: {{ backgroundColor: '#111827', cornerRadius: 6, callbacks: {{ label: ctx => ctx.dataset.label + ': ' + ctx.raw + '%' }} }}
        }},
        scales: {{
          x: {{ stacked: true, grid: {{ display: false }}, ticks: {{ font: {{ size: 10 }} }} }},
          y: {{ stacked: true, title: {{ display: true, text: 'Disrupted time %', font: {{ size: 11 }} }}, grid: {{ color: '#F3F4F6' }} }}
        }}
      }}
    }});
  }})();
}}

async function loadDashboard() {{
  try {{
    const response = await fetch(DATA_URL, {{ cache: 'no-store' }});
    if (!response.ok) {{
      throw new Error(`Failed to load dashboard data (${{response.status}})`);
    }}
    renderDashboard(await response.json());
  }} catch (error) {{
    console.error(error);
    showError('The dashboard data could not be loaded from Azure storage yet.');
  }}
}}

loadDashboard();
</script>
</body>
</html>"""
    return html


def build_dashboard_payload(
    reliability, heatmap, correlation, causes, causes_by_line,
    daily_disruption, line_daily, peak_offpeak, escalations,
    streaks, weekly_trend
):
    all_days = sorted(daily_disruption.keys())
    sorted_causes = sorted(causes.items(), key=lambda x: -x[1])
    all_weeks = sorted(set(w["week"] for line_weeks in weekly_trend.values() for w in line_weeks))
    streak_distribution = {line: dict(values["distribution"]) for line, values in streaks.items()}
    line_daily_data = {line: dict(values) for line, values in line_daily.items()}

    return {
        "generatedAt": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "dateFrom": all_days[0] if all_days else "—",
        "dateTo": all_days[-1] if all_days else "—",
        "totalChecks": sum(r["total_checks"] for r in reliability.values()),
        "totalDelays": sum(count for _, count in sorted_causes),
        "weeklyTrend": weekly_trend,
        "heatmapData": heatmap,
        "causeLabels": [cause for cause, _ in sorted_causes],
        "causeValues": [count for _, count in sorted_causes],
        "peakOffpeak": peak_offpeak,
        "correlation": correlation,
        "calendarData": [{"date": date, "value": daily_disruption[date]} for date in all_days],
        "lineDailyData": line_daily_data,
        "streakDist": streak_distribution,
        "escalations": escalations,
        "reliability": reliability,
        "allWeeks": all_weeks,
    }


def publish_dashboard_to_blob_storage(
    connection_string,
    dashboard_html,
    dashboard_payload,
    container_name="$web",
    dashboard_blob_name=DASHBOARD_HTML_FILE,
    data_blob_name=DASHBOARD_DATA_FILE,
    index_blob_name="index.html",
):
    if not connection_string:
        raise ValueError("Blob storage connection string is required")
    if BlobServiceClient is None or ContentSettings is None:
        raise RuntimeError("azure-storage-blob is required to publish dashboard artifacts")

    service_client = BlobServiceClient.from_connection_string(connection_string)
    container_client = service_client.get_container_client(container_name)
    try:
        container_client.create_container()
    except ResourceExistsError:
        pass

    html_settings = ContentSettings(content_type="text/html; charset=utf-8")
    json_settings = ContentSettings(content_type="application/json; charset=utf-8")

    uploaded_blobs = []
    for blob_name in (dashboard_blob_name, index_blob_name):
        container_client.get_blob_client(blob_name).upload_blob(
            dashboard_html,
            overwrite=True,
            content_settings=html_settings,
        )
        uploaded_blobs.append(blob_name)

    container_client.get_blob_client(data_blob_name).upload_blob(
        json.dumps(dashboard_payload, indent=2),
        overwrite=True,
        content_settings=json_settings,
    )
    uploaded_blobs.append(data_blob_name)
    return uploaded_blobs


# ── Main ────────────────────────────────────────────────────────────────────
def main():
    conn_str = get_connection_string()

    state_data, delay_data = download_all_data(conn_str)

    print("\nAnalysing data...")
    reliability = analyse_reliability(state_data)
    heatmap = analyse_hourly_heatmap(state_data)
    correlation = analyse_cross_line_correlation(state_data)
    causes, causes_by_line = analyse_delay_causes(delay_data)
    daily_disruption, line_daily = analyse_daily_disruption(state_data)
    peak_offpeak = analyse_peak_vs_offpeak(state_data)
    escalations = analyse_severity_escalation(delay_data)
    streaks = analyse_disruption_streaks(state_data)
    weekly_trend = analyse_weekly_trend(state_data)

    base_dir = os.path.dirname(__file__) or "."
    args = (
        reliability, heatmap, correlation, causes, causes_by_line,
        daily_disruption, line_daily, peak_offpeak, escalations,
        streaks, weekly_trend
    )
    dashboard_payload = build_dashboard_payload(*args)

    # v1 (dark theme)
    print("Generating v1 dashboard (dark theme)...")
    html_v1 = generate_html(*args)
    v1_path = os.path.join(base_dir, "tfl_status_dashboard.html")
    with open(v1_path, "w", encoding="utf-8") as f:
        f.write(html_v1)
    print(f"  ✅ {v1_path}")

    # v2 (Clean design system)
    print("Generating v2 dashboard (Clean design)...")
    html_v2 = generate_html_v2(dashboard_payload)
    v2_path = os.path.join(base_dir, DASHBOARD_HTML_FILE)
    with open(v2_path, "w", encoding="utf-8") as f:
        f.write(html_v2)
    print(f"  ✅ {v2_path}")

    data_path = os.path.join(base_dir, DASHBOARD_DATA_FILE)
    with open(data_path, "w", encoding="utf-8") as f:
        json.dump(dashboard_payload, f, indent=2)
    print(f"  ✅ {data_path}")

    if os.environ.get("PUBLISH_TO_BLOB", "").strip().lower() in {"1", "true", "yes"}:
        publish_connection = os.environ.get("DASHBOARD_BLOB_CONNECTION_STRING", "").strip() or conn_str
        container_name = os.environ.get("DASHBOARD_BLOB_CONTAINER", "$web").strip() or "$web"
        uploaded = publish_dashboard_to_blob_storage(
            connection_string=publish_connection,
            dashboard_html=html_v2,
            dashboard_payload=dashboard_payload,
            container_name=container_name,
        )
        print(f"  ✅ Published to Azure Blob Storage: {', '.join(uploaded)}")

    print(f"\nDone — open either file in a browser to view.")


if __name__ == "__main__":
    main()
