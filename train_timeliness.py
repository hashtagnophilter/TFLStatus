"""TFL Train Timeliness Monitor - Proof of Concept

Polls the TFL Arrivals API to collect real-time arrival predictions,
compares them against timetabled schedules, and generates an HTML report
with sparkline visualizations showing on-time performance.

TFL API Endpoints Used:
- /Line/{id}/Arrivals - Real-time arrival predictions
- /Line/{id}/Timetable/{fromStopPointId} - Scheduled timetable data

The approach:
1. Fetch arrival predictions for monitored lines
2. Fetch timetable data for key stations on those lines
3. Match each predicted arrival to the nearest scheduled departure
4. Calculate the variance (early/on-time/late)
5. Persist snapshots to JSON for historical tracking
6. Generate an HTML report with sparkline visuals and on-time %
"""

import requests
import json
import os
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    from azure.storage.blob import BlobServiceClient, ContentSettings
except ImportError:  # pragma: no cover - handled at runtime for Azure deployments
    BlobServiceClient = None
    ContentSettings = None

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# TFL API base URL
TFL_API_BASE = "https://api.tfl.gov.uk"

# Lines to monitor (matching the existing function_app.py lines)
MONITORED_LINES = {
    "metropolitan": {
        "name": "Metropolitan",
        "colour": "#9B0056",
        # Key stations to monitor (NaptanId: display name)
        "stations": {
            "940GZZLUBST": "Baker Street",
            "940GZZLUFYR": "Finchley Road",
            "940GZZLUMSP": "Moor Park",
            "940GZZLUHOH": "Harrow-on-the-Hill",
            "940GZZLUKSX": "King's Cross St. Pancras",
        }
    },
    "bakerloo": {
        "name": "Bakerloo",
        "colour": "#B36305",
        "stations": {
            "940GZZLUPAC": "Paddington",
            "940GZZLUOXC": "Oxford Circus",
            "940GZZLUWLO": "Waterloo",
            "940GZZLUEMB": "Embankment",
            "940GZZLUEAC": "Elephant & Castle",
        }
    },
    "hammersmith-city": {
        "name": "Hammersmith & City",
        "colour": "#F3A9BB",
        "stations": {
            "940GZZLUHSC": "Hammersmith",
            "940GZZLUBST": "Baker Street",
            "940GZZLUKSX": "King's Cross St. Pancras",
            "940GZZLULVT": "Liverpool Street",
            "940GZZLUBKG": "Barking",
        }
    },
    "circle": {
        "name": "Circle",
        "colour": "#FFD300",
        "stations": {
            "940GZZLUHSC": "Hammersmith",
            "940GZZLUBST": "Baker Street",
            "940GZZLUKSX": "King's Cross St. Pancras",
            "940GZZLULVT": "Liverpool Street",
            "940GZZLUEMB": "Embankment",
        }
    },
}

# On-time threshold in seconds (trains within this window are considered "on time")
ON_TIME_THRESHOLD_SECONDS = 120  # 2 minutes

# Maximum window in seconds for matching a prediction to a scheduled time
MAX_MATCHING_WINDOW_SECONDS = 900  # 15 minutes

# Data directory for persisting snapshots
DATA_DIR = Path(os.environ.get("DATA_DIR", "timeliness_data"))

# Output HTML file
OUTPUT_HTML = Path(os.environ.get("OUTPUT_HTML", "timeliness_report.html"))

def _read_collection_interval_minutes() -> int:
    value = os.environ.get("TIMELINESS_INTERVAL_MINUTES", "2")
    try:
        parsed = int(value)
        return parsed if parsed > 0 else 2
    except ValueError:
        return 2


# Collection cadence (used by report text and Azure timer configuration docs)
COLLECTION_INTERVAL_MINUTES = _read_collection_interval_minutes()


def fetch_line_arrivals(line_id: str) -> List[Dict]:
    """Fetch real-time arrival predictions for a line from the TFL API."""
    url = f"{TFL_API_BASE}/Line/{line_id}/Arrivals"
    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, list):
            logger.warning(f"Unexpected response format for {line_id} arrivals")
            return []
        logger.info(f"Fetched {len(data)} arrival predictions for {line_id}")
        return data
    except requests.RequestException as e:
        logger.error(f"Failed to fetch arrivals for {line_id}: {e}")
        return []


def fetch_timetable(line_id: str, station_id: str) -> List[Dict]:
    """Fetch timetable data for a specific station on a line.

    Returns a list of scheduled departure times with their details.
    The TFL Timetable endpoint returns route-based schedules.
    """
    url = f"{TFL_API_BASE}/Line/{line_id}/Timetable/{station_id}"
    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        data = response.json()
        schedules = []
        # Parse the timetable response to extract scheduled times
        timetable = data.get("timetable", {})
        routes = timetable.get("routes", [])
        for route in routes:
            for schedule_section in route.get("schedules", []):
                known_journeys = schedule_section.get("knownJourneys", [])
                for journey in known_journeys:
                    hour = journey.get("hour", "")
                    minute = journey.get("minute", "")
                    if hour and minute:
                        schedules.append({
                            "hour": int(hour),
                            "minute": int(minute),
                            "interval_id": journey.get("intervalId", 0),
                        })
        logger.info(f"Fetched {len(schedules)} timetable entries for {line_id} at {station_id}")
        return schedules
    except requests.RequestException as e:
        logger.error(f"Failed to fetch timetable for {line_id}/{station_id}: {e}")
        return []


def parse_iso_datetime(iso_str: str) -> Optional[datetime]:
    """Parse an ISO 8601 datetime string from the TFL API."""
    if not iso_str:
        return None
    try:
        # Handle Z suffix and various formats
        iso_str = iso_str.replace("Z", "+00:00")
        return datetime.fromisoformat(iso_str)
    except (ValueError, TypeError):
        return None


def find_nearest_scheduled_time(
    predicted_arrival: datetime,
    timetable_entries: List[Dict],
) -> Optional[Tuple[datetime, int]]:
    """Find the nearest scheduled time to a predicted arrival.

    Returns (scheduled_datetime, variance_seconds) or None if no match found.
    Variance is positive if the train is late, negative if early.
    """
    if not timetable_entries:
        return None

    arrival_date = predicted_arrival.date()
    best_match = None
    best_variance = float('inf')

    for entry in timetable_entries:
        hour = entry["hour"]
        minute = entry["minute"]
        # TFL timetable API can return hours >= 24 for services running past midnight
        # on the same service day (e.g. hour=25 means clock time 01:xx).
        # Normalise to a valid 0-23 hour so datetime.replace() doesn't raise.
        scheduled_time = predicted_arrival.replace(
            hour=hour % 24,
            minute=minute,
            second=0,
            microsecond=0,
        )
        variance = (predicted_arrival - scheduled_time).total_seconds()

        # Only match within a reasonable window (15 minutes)
        if abs(variance) < abs(best_variance) and abs(variance) <= MAX_MATCHING_WINDOW_SECONDS:
            best_variance = variance
            best_match = scheduled_time

    if best_match is not None:
        return (best_match, int(best_variance))
    return None


def collect_timeliness_snapshot(line_id: str, line_config: Dict) -> Dict:
    """Collect a timeliness snapshot for a single line.

    Returns a dict with station-level timeliness data.
    """
    arrivals = fetch_line_arrivals(line_id)
    if not arrivals:
        return {"line_id": line_id, "stations": {}, "timestamp": datetime.now(timezone.utc).isoformat()}

    # Group arrivals by station
    station_arrivals: Dict[str, List[Dict]] = {}
    for arrival in arrivals:
        naptan_id = arrival.get("naptanId", "")
        if naptan_id in line_config["stations"]:
            if naptan_id not in station_arrivals:
                station_arrivals[naptan_id] = []
            station_arrivals[naptan_id].append(arrival)

    # Fetch timetables and calculate variances for each monitored station
    station_results = {}
    for station_id, station_name in line_config["stations"].items():
        timetable = fetch_timetable(line_id, station_id)
        predictions = station_arrivals.get(station_id, [])

        variances = []
        for pred in predictions:
            predicted_arrival = parse_iso_datetime(pred.get("expectedArrival", ""))
            if predicted_arrival is None:
                continue

            match = find_nearest_scheduled_time(predicted_arrival, timetable)
            if match:
                scheduled_time, variance_secs = match
                variances.append({
                    "vehicle_id": pred.get("vehicleId", "unknown"),
                    "destination": pred.get("destinationName", "unknown"),
                    "predicted_arrival": predicted_arrival.isoformat(),
                    "scheduled_time": scheduled_time.isoformat(),
                    "variance_seconds": variance_secs,
                    "time_to_station": pred.get("timeToStation", 0),
                    "platform": pred.get("platformName", ""),
                    "direction": pred.get("direction", ""),
                })

        # Calculate metrics
        total = len(variances)
        on_time = sum(1 for v in variances if abs(v["variance_seconds"]) <= ON_TIME_THRESHOLD_SECONDS)
        early = sum(1 for v in variances if v["variance_seconds"] < -ON_TIME_THRESHOLD_SECONDS)
        late = sum(1 for v in variances if v["variance_seconds"] > ON_TIME_THRESHOLD_SECONDS)

        station_results[station_id] = {
            "name": station_name,
            "total_predictions": total,
            "on_time": on_time,
            "early": early,
            "late": late,
            "on_time_pct": round((on_time / total) * 100, 1) if total > 0 else 0,
            "avg_variance_secs": round(sum(v["variance_seconds"] for v in variances) / total, 1) if total > 0 else 0,
            "variances": variances,
        }

    return {
        "line_id": line_id,
        "line_name": line_config["name"],
        "colour": line_config["colour"],
        "stations": station_results,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def collect_all_snapshots() -> List[Dict]:
    """Collect timeliness snapshots for all monitored lines."""
    snapshots = []
    for line_id, line_config in MONITORED_LINES.items():
        logger.info(f"Collecting timeliness data for {line_config['name']} line...")
        snapshot = collect_timeliness_snapshot(line_id, line_config)
        snapshots.append(snapshot)
    return snapshots


def save_snapshot(snapshots: List[Dict]) -> Path:
    """Save a snapshot to the data directory as a timestamped JSON file."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filepath = DATA_DIR / f"snapshot_{timestamp}.json"
    with open(filepath, "w") as f:
        json.dump(snapshots, f, indent=2)
    logger.info(f"Saved snapshot to {filepath}")
    return filepath


def load_historical_snapshots(max_files: int = 50) -> List[List[Dict]]:
    """Load historical snapshots from the data directory.

    Returns the most recent snapshots, sorted by time.
    """
    if not DATA_DIR.exists():
        return []

    files = sorted(DATA_DIR.glob("snapshot_*.json"), reverse=True)[:max_files]
    files.reverse()  # Oldest first

    snapshots = []
    for f in files:
        try:
            with open(f) as fp:
                snapshots.append(json.load(fp))
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Failed to load snapshot {f}: {e}")
    return snapshots


def calculate_line_metrics(history: List[List[Dict]], line_id: str) -> Dict:
    """Calculate aggregate metrics for a line from historical snapshots.

    Returns metrics including time-series data for sparklines.
    """
    time_series = []
    for snapshot_group in history:
        for snapshot in snapshot_group:
            if snapshot.get("line_id") != line_id:
                continue
            stations = snapshot.get("stations", {})
            total_preds = sum(s.get("total_predictions", 0) for s in stations.values())
            total_on_time = sum(s.get("on_time", 0) for s in stations.values())
            total_early = sum(s.get("early", 0) for s in stations.values())
            total_late = sum(s.get("late", 0) for s in stations.values())
            avg_variance = 0
            if total_preds > 0:
                avg_variance = sum(
                    s.get("avg_variance_secs", 0) * s.get("total_predictions", 0)
                    for s in stations.values()
                ) / total_preds

            time_series.append({
                "timestamp": snapshot.get("timestamp", ""),
                "total": total_preds,
                "on_time": total_on_time,
                "early": total_early,
                "late": total_late,
                "on_time_pct": round((total_on_time / total_preds) * 100, 1) if total_preds > 0 else 0,
                "avg_variance_secs": round(avg_variance, 1),
            })

    # Overall metrics
    all_total = sum(t["total"] for t in time_series)
    all_on_time = sum(t["on_time"] for t in time_series)
    all_early = sum(t["early"] for t in time_series)
    all_late = sum(t["late"] for t in time_series)

    return {
        "time_series": time_series,
        "overall_total": all_total,
        "overall_on_time": all_on_time,
        "overall_early": all_early,
        "overall_late": all_late,
        "overall_on_time_pct": round((all_on_time / all_total) * 100, 1) if all_total > 0 else 0,
        "overall_avg_variance": round(
            sum(t["avg_variance_secs"] * t["total"] for t in time_series) / all_total, 1
        ) if all_total > 0 else 0,
    }


def generate_sparkline_svg(values: List[float], colour: str, width: int = 200, height: int = 40) -> str:
    """Generate an inline SVG sparkline from a list of values.

    Values represent on-time percentage at each snapshot.
    The sparkline includes colour-coded regions:
    - Green zone (>= 80%): good performance
    - Amber zone (60-80%): moderate
    - Red zone (< 60%): poor
    """
    if not values:
        return f'<svg width="{width}" height="{height}"><text x="5" y="20" font-size="12" fill="#999">No data</text></svg>'

    min_val = 0
    max_val = 100
    padding = 2

    # Build SVG path
    points = []
    for i, val in enumerate(values):
        x = padding + (i / max(len(values) - 1, 1)) * (width - 2 * padding)
        y = height - padding - ((val - min_val) / max(max_val - min_val, 1)) * (height - 2 * padding)
        points.append(f"{x:.1f},{y:.1f}")

    polyline = " ".join(points)

    # Build filled area path (for area chart effect)
    area_points = points.copy()
    area_points.append(f"{width - padding:.1f},{height - padding:.1f}")
    area_points.append(f"{padding:.1f},{height - padding:.1f}")
    area_path = " ".join(area_points)

    # Zone backgrounds
    good_y = height - padding - (80 / 100) * (height - 2 * padding)
    moderate_y = height - padding - (60 / 100) * (height - 2 * padding)

    svg = f'''<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">
  <rect x="{padding}" y="{padding}" width="{width - 2*padding}" height="{good_y - padding}" fill="#e8f5e9" opacity="0.3"/>
  <rect x="{padding}" y="{good_y}" width="{width - 2*padding}" height="{moderate_y - good_y}" fill="#fff3e0" opacity="0.3"/>
  <rect x="{padding}" y="{moderate_y}" width="{width - 2*padding}" height="{height - padding - moderate_y}" fill="#ffebee" opacity="0.3"/>
  <polygon points="{area_path}" fill="{colour}" opacity="0.15"/>
  <polyline points="{polyline}" fill="none" stroke="{colour}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
  <circle cx="{points[-1].split(',')[0]}" cy="{points[-1].split(',')[1]}" r="3" fill="{colour}"/>
</svg>'''
    return svg


def generate_variance_bar_svg(on_time_pct: float, early_pct: float, late_pct: float,
                               width: int = 200, height: int = 20) -> str:
    """Generate a stacked bar SVG showing early/on-time/late distribution."""
    early_w = (early_pct / 100) * width
    on_time_w = (on_time_pct / 100) * width
    late_w = (late_pct / 100) * width

    svg = f'''<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">
  <rect x="0" y="0" width="{early_w:.1f}" height="{height}" fill="#2196F3" rx="2"/>
  <rect x="{early_w:.1f}" y="0" width="{on_time_w:.1f}" height="{height}" fill="#4CAF50" rx="0"/>
  <rect x="{early_w + on_time_w:.1f}" y="0" width="{late_w:.1f}" height="{height}" fill="#f44336" rx="2"/>
</svg>'''
    return svg


def generate_html_report(all_metrics: Dict[str, Dict], current_snapshots: List[Dict]) -> str:
    """Generate a complete HTML report with sparkline visualizations."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines_html = []
    for line_id, line_config in MONITORED_LINES.items():
        metrics = all_metrics.get(line_id, {})
        time_series = metrics.get("time_series", [])
        on_time_pcts = [t["on_time_pct"] for t in time_series]

        # Current snapshot data
        current = next((s for s in current_snapshots if s.get("line_id") == line_id), {})
        current_stations = current.get("stations", {})

        sparkline = generate_sparkline_svg(on_time_pcts, line_config["colour"], width=300, height=50)

        overall_on_time = metrics.get("overall_on_time_pct", 0)
        overall_total = metrics.get("overall_total", 0)
        overall_early = metrics.get("overall_early", 0)
        overall_late = metrics.get("overall_late", 0)
        overall_on_time_count = metrics.get("overall_on_time", 0)
        avg_variance = metrics.get("overall_avg_variance", 0)

        # Distribution percentages for bar chart
        if overall_total > 0:
            early_pct = (overall_early / overall_total) * 100
            on_time_pct_bar = (overall_on_time_count / overall_total) * 100
            late_pct = (overall_late / overall_total) * 100
        else:
            early_pct = on_time_pct_bar = late_pct = 0

        dist_bar = generate_variance_bar_svg(on_time_pct_bar, early_pct, late_pct, width=300, height=20)

        # Performance indicator
        if overall_on_time >= 80:
            perf_class = "perf-good"
            perf_icon = "✅"
        elif overall_on_time >= 60:
            perf_class = "perf-moderate"
            perf_icon = "⚠️"
        else:
            perf_class = "perf-poor"
            perf_icon = "❌"

        # Station breakdown table
        station_rows = ""
        for station_id, station_data in current_stations.items():
            s_total = station_data.get("total_predictions", 0)
            s_on_time = station_data.get("on_time_pct", 0)
            s_avg = station_data.get("avg_variance_secs", 0)
            s_early = station_data.get("early", 0)
            s_late = station_data.get("late", 0)
            s_on_time_count = station_data.get("on_time", 0)

            variance_text = f"{abs(s_avg):.0f}s {'late' if s_avg > 0 else 'early'}" if s_avg != 0 else "On time"

            station_rows += f"""
            <tr>
              <td>{station_data['name']}</td>
              <td>{s_total}</td>
              <td class="{'good' if s_on_time >= 80 else 'moderate' if s_on_time >= 60 else 'poor'}">{s_on_time}%</td>
              <td>{s_early}</td>
              <td>{s_on_time_count}</td>
              <td>{s_late}</td>
              <td>{variance_text}</td>
            </tr>"""

        variance_display = f"{abs(avg_variance):.0f}s {'late' if avg_variance > 0 else 'early'}" if avg_variance != 0 else "On time"

        line_html = f"""
    <div class="line-card" style="border-left: 5px solid {line_config['colour']}">
      <div class="line-header">
        <div class="line-name" style="color: {line_config['colour']}">{line_config['name']} Line</div>
        <div class="{perf_class}">{perf_icon} {overall_on_time}% On Time</div>
      </div>

      <div class="metrics-row">
        <div class="metric">
          <div class="metric-value">{overall_total}</div>
          <div class="metric-label">Total Predictions</div>
        </div>
        <div class="metric">
          <div class="metric-value good">{overall_on_time_count}</div>
          <div class="metric-label">On Time (±{ON_TIME_THRESHOLD_SECONDS}s)</div>
        </div>
        <div class="metric">
          <div class="metric-value early-val">{overall_early}</div>
          <div class="metric-label">Early</div>
        </div>
        <div class="metric">
          <div class="metric-value poor">{overall_late}</div>
          <div class="metric-label">Late</div>
        </div>
        <div class="metric">
          <div class="metric-value">{variance_display}</div>
          <div class="metric-label">Avg Variance</div>
        </div>
      </div>

      <div class="charts-row">
        <div class="chart-container">
          <div class="chart-title">On-Time % Over Time</div>
          {sparkline}
        </div>
        <div class="chart-container">
          <div class="chart-title">Distribution: Early | On Time | Late</div>
          {dist_bar}
          <div class="legend">
            <span class="legend-item"><span class="dot early-dot"></span> Early</span>
            <span class="legend-item"><span class="dot ontime-dot"></span> On Time</span>
            <span class="legend-item"><span class="dot late-dot"></span> Late</span>
          </div>
        </div>
      </div>

      <details class="station-details">
        <summary>Station Breakdown</summary>
        <table class="station-table">
          <thead>
            <tr>
              <th>Station</th>
              <th>Predictions</th>
              <th>On Time %</th>
              <th>Early</th>
              <th>On Time</th>
              <th>Late</th>
              <th>Avg Variance</th>
            </tr>
          </thead>
          <tbody>
            {station_rows if station_rows else '<tr><td colspan="7">No current data available</td></tr>'}
          </tbody>
        </table>
      </details>
    </div>"""
        lines_html.append(line_html)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>TFL Train Timeliness Report</title>
  <style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      background: #f5f5f5;
      color: #333;
      padding: 20px;
    }}
    .container {{ max-width: 900px; margin: 0 auto; }}
    .header {{
      text-align: center;
      padding: 20px;
      margin-bottom: 20px;
      background: white;
      border-radius: 8px;
      box-shadow: 0 1px 3px rgba(0,0,0,0.1);
    }}
    .header h1 {{ font-size: 24px; margin-bottom: 5px; }}
    .header .subtitle {{ color: #666; font-size: 14px; }}
    .header .timestamp {{ color: #999; font-size: 12px; margin-top: 5px; }}
    .methodology {{
      background: #e3f2fd;
      border-radius: 8px;
      padding: 15px;
      margin-bottom: 20px;
      font-size: 13px;
      color: #1565C0;
    }}
    .methodology strong {{ display: block; margin-bottom: 5px; }}
    .line-card {{
      background: white;
      border-radius: 8px;
      padding: 20px;
      margin-bottom: 15px;
      box-shadow: 0 1px 3px rgba(0,0,0,0.1);
    }}
    .line-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 15px;
    }}
    .line-name {{ font-size: 20px; font-weight: 700; text-shadow: 0 1px 2px rgba(0,0,0,0.15); }}
    .perf-good {{ color: #4CAF50; font-weight: 600; font-size: 18px; }}
    .perf-moderate {{ color: #FF9800; font-weight: 600; font-size: 18px; }}
    .perf-poor {{ color: #f44336; font-weight: 600; font-size: 18px; }}
    .metrics-row {{
      display: flex;
      gap: 15px;
      margin-bottom: 15px;
      flex-wrap: wrap;
    }}
    .metric {{
      flex: 1;
      min-width: 100px;
      text-align: center;
      padding: 10px;
      background: #f8f9fa;
      border-radius: 6px;
    }}
    .metric-value {{ font-size: 22px; font-weight: 700; }}
    .metric-label {{ font-size: 11px; color: #666; margin-top: 3px; }}
    .good {{ color: #4CAF50; }}
    .moderate {{ color: #FF9800; }}
    .poor {{ color: #f44336; }}
    .early-val {{ color: #2196F3; }}
    .charts-row {{
      display: flex;
      gap: 20px;
      margin-bottom: 15px;
      flex-wrap: wrap;
    }}
    .chart-container {{ flex: 1; min-width: 250px; }}
    .chart-title {{ font-size: 12px; color: #666; margin-bottom: 5px; font-weight: 600; }}
    .legend {{ display: flex; gap: 12px; margin-top: 5px; font-size: 11px; color: #666; }}
    .legend-item {{ display: flex; align-items: center; gap: 4px; }}
    .dot {{ width: 8px; height: 8px; border-radius: 50%; display: inline-block; }}
    .early-dot {{ background: #2196F3; }}
    .ontime-dot {{ background: #4CAF50; }}
    .late-dot {{ background: #f44336; }}
    .station-details {{ margin-top: 10px; }}
    .station-details summary {{
      cursor: pointer;
      font-size: 13px;
      color: #666;
      padding: 5px 0;
    }}
    .station-table {{
      width: 100%;
      border-collapse: collapse;
      margin-top: 10px;
      font-size: 13px;
    }}
    .station-table th, .station-table td {{
      padding: 8px 10px;
      text-align: left;
      border-bottom: 1px solid #eee;
    }}
    .station-table th {{ background: #f8f9fa; font-weight: 600; }}
    .no-data {{
      text-align: center;
      padding: 40px;
      color: #999;
      font-size: 16px;
      background: white;
      border-radius: 8px;
    }}
    .footer {{
      text-align: center;
      padding: 15px;
      font-size: 12px;
      color: #999;
      margin-top: 20px;
    }}
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1>🚇 TFL Train Timeliness Monitor</h1>
      <div class="subtitle">Real-time on-time performance tracking for London Underground</div>
      <div class="timestamp">Last updated: {now} | Threshold: ±{ON_TIME_THRESHOLD_SECONDS}s</div>
    </div>

    <div class="methodology">
      <strong>📊 How this works</strong>
      Arrival predictions from the TFL API are compared against scheduled timetable data.
      Trains arriving within ±{ON_TIME_THRESHOLD_SECONDS} seconds of their scheduled time are
      counted as "on time". The sparkline shows on-time % over time across all monitored stations.
      Data is collected every {COLLECTION_INTERVAL_MINUTES} minutes during operating hours.
    </div>

    {''.join(lines_html) if lines_html else '<div class="no-data">No timeliness data available yet. Data collection starts after 8:00 AM on weekdays.</div>'}

    <div class="footer">
      TFL Train Timeliness Monitor &mdash; Proof of Concept<br>
      Data source: <a href="https://api.tfl.gov.uk">TFL Unified API</a> |
      Threshold: ±{ON_TIME_THRESHOLD_SECONDS}s = on time
    </div>
  </div>
</body>
</html>"""
    return html


def run_collection(return_payload: bool = False):
    """Main entry point: collect data, save snapshot, generate report.

    If return_payload is True, includes in-memory report data for optional
    Azure blob publishing.
    """
    logger.info("Starting timeliness data collection...")

    # Collect current snapshots
    current_snapshots = collect_all_snapshots()

    # Save the snapshot
    snapshot_path = save_snapshot(current_snapshots)

    # Load historical data (including the one we just saved)
    history = load_historical_snapshots()

    # Calculate metrics for each line
    all_metrics = {}
    for line_id in MONITORED_LINES:
        all_metrics[line_id] = calculate_line_metrics(history, line_id)

    # Generate HTML report
    html = generate_html_report(all_metrics, current_snapshots)
    OUTPUT_HTML.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_HTML, "w") as f:
        f.write(html)
    logger.info(f"Report generated: {OUTPUT_HTML}")

    # Also save a summary JSON
    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "lines": {}
    }
    for line_id, metrics in all_metrics.items():
        summary["lines"][line_id] = {
            "name": MONITORED_LINES[line_id]["name"],
            "on_time_pct": metrics["overall_on_time_pct"],
            "total_predictions": metrics["overall_total"],
            "avg_variance_secs": metrics["overall_avg_variance"],
        }

    summary_path = DATA_DIR / "latest_summary.json"
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)
    logger.info(f"Summary saved: {summary_path}")

    if return_payload:
        return {
            "summary": summary,
            "report_html": html,
            "current_snapshots": current_snapshots,
            "snapshot_name": snapshot_path.name,
        }

    return summary


def publish_artifacts_to_blob_storage(
    *,
    connection_string: str,
    report_html: str,
    summary: Dict,
    current_snapshots: List[Dict],
    snapshot_name: str,
    container_name: str = "$web",
    index_template_path: Path = Path("web/index.html"),
) -> List[str]:
    """Publish timeliness artifacts to Azure Blob Storage for static website use."""
    if not connection_string:
        raise ValueError("Blob storage connection string is required")
    if BlobServiceClient is None:
        raise RuntimeError("azure-storage-blob is required to publish artifacts")

    service_client = BlobServiceClient.from_connection_string(connection_string)
    container_client = service_client.get_container_client(container_name)
    try:
        container_client.create_container()
    except Exception:
        # Container already exists (or no-op based on account permissions)
        pass

    uploaded_blobs = []

    report_blob = container_client.get_blob_client("timeliness_report.html")
    report_blob.upload_blob(
        report_html,
        overwrite=True,
        content_settings=ContentSettings(content_type="text/html; charset=utf-8") if ContentSettings else None,
    )
    uploaded_blobs.append("timeliness_report.html")

    summary_blob = container_client.get_blob_client("latest_summary.json")
    summary_blob.upload_blob(
        json.dumps(summary, indent=2),
        overwrite=True,
        content_settings=ContentSettings(content_type="application/json; charset=utf-8") if ContentSettings else None,
    )
    uploaded_blobs.append("latest_summary.json")

    snapshot_blob_name = f"snapshots/{snapshot_name}"
    snapshot_blob = container_client.get_blob_client(snapshot_blob_name)
    snapshot_blob.upload_blob(
        json.dumps(current_snapshots, indent=2),
        overwrite=True,
        content_settings=ContentSettings(content_type="application/json; charset=utf-8") if ContentSettings else None,
    )
    uploaded_blobs.append(snapshot_blob_name)

    if index_template_path.exists():
        index_blob = container_client.get_blob_client("index.html")
        index_blob.upload_blob(
            index_template_path.read_text(encoding="utf-8"),
            overwrite=True,
            content_settings=ContentSettings(content_type="text/html; charset=utf-8") if ContentSettings else None,
        )
        uploaded_blobs.append("index.html")
    else:
        logger.warning(f"Web index template not found at {index_template_path}; skipping index upload")

    logger.info(f"Published {len(uploaded_blobs)} blob artifacts to container '{container_name}'")
    return uploaded_blobs


if __name__ == "__main__":
    run_collection()
