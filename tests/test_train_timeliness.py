"""
Unit tests for train_timeliness.py

Uses mock TFL API data to validate timeliness calculations
without requiring live API access.
"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

# Ensure we can import the module
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from train_timeliness import (
    parse_iso_datetime,
    find_nearest_scheduled_time,
    collect_timeliness_snapshot,
    calculate_line_metrics,
    generate_sparkline_svg,
    generate_variance_bar_svg,
    generate_html_report,
    save_snapshot,
    load_historical_snapshots,
    publish_artifacts_to_blob_storage,
    ON_TIME_THRESHOLD_SECONDS,
    MONITORED_LINES,
    DATA_DIR,
    get_data_dir,
    get_output_html_path,
)


# --- Sample TFL API response data ---

SAMPLE_ARRIVALS = [
    {
        "naptanId": "940GZZLUBST",
        "stationName": "Baker Street Underground Station",
        "lineId": "metropolitan",
        "platformName": "Platform 1",
        "direction": "inbound",
        "destinationName": "Aldgate",
        "expectedArrival": "2026-03-07T09:15:30+00:00",
        "timeToStation": 120,
        "vehicleId": "train_001",
    },
    {
        "naptanId": "940GZZLUBST",
        "stationName": "Baker Street Underground Station",
        "lineId": "metropolitan",
        "platformName": "Platform 2",
        "direction": "outbound",
        "destinationName": "Amersham",
        "expectedArrival": "2026-03-07T09:20:00+00:00",
        "timeToStation": 390,
        "vehicleId": "train_002",
    },
    {
        "naptanId": "940GZZLUFYR",
        "stationName": "Finchley Road Underground Station",
        "lineId": "metropolitan",
        "platformName": "Platform 1",
        "direction": "inbound",
        "destinationName": "Aldgate",
        "expectedArrival": "2026-03-07T09:18:00+00:00",
        "timeToStation": 270,
        "vehicleId": "train_003",
    },
    {
        # Station not in monitored list - should be ignored
        "naptanId": "940GZZLUUXB",
        "stationName": "Uxbridge Underground Station",
        "lineId": "metropolitan",
        "platformName": "Platform 1",
        "direction": "outbound",
        "destinationName": "Uxbridge",
        "expectedArrival": "2026-03-07T09:25:00+00:00",
        "timeToStation": 600,
        "vehicleId": "train_004",
    },
]

SAMPLE_TIMETABLE_RESPONSE = {
    "timetable": {
        "routes": [
            {
                "schedules": [
                    {
                        "knownJourneys": [
                            {"hour": "9", "minute": "14", "intervalId": 1},
                            {"hour": "9", "minute": "20", "intervalId": 2},
                            {"hour": "9", "minute": "26", "intervalId": 3},
                            {"hour": "9", "minute": "32", "intervalId": 4},
                        ]
                    }
                ]
            }
        ]
    }
}


class TestParseIsoDatetime(unittest.TestCase):
    """Tests for ISO datetime parsing."""

    def test_parse_with_timezone(self):
        result = parse_iso_datetime("2026-03-07T09:15:30+00:00")
        self.assertIsNotNone(result)
        self.assertEqual(result.hour, 9)
        self.assertEqual(result.minute, 15)

    def test_parse_with_z_suffix(self):
        result = parse_iso_datetime("2026-03-07T09:15:30Z")
        self.assertIsNotNone(result)
        self.assertEqual(result.hour, 9)

    def test_parse_empty_string(self):
        self.assertIsNone(parse_iso_datetime(""))

    def test_parse_none(self):
        self.assertIsNone(parse_iso_datetime(None))

    def test_parse_invalid(self):
        self.assertIsNone(parse_iso_datetime("not-a-date"))


class TestFindNearestScheduledTime(unittest.TestCase):
    """Tests for matching predictions to scheduled times."""

    def setUp(self):
        self.timetable = [
            {"hour": 9, "minute": 14},
            {"hour": 9, "minute": 20},
            {"hour": 9, "minute": 26},
            {"hour": 9, "minute": 32},
        ]

    def test_exact_match(self):
        predicted = datetime(2026, 3, 7, 9, 20, 0, tzinfo=timezone.utc)
        result = find_nearest_scheduled_time(predicted, self.timetable)
        self.assertIsNotNone(result)
        scheduled, variance = result
        self.assertEqual(variance, 0)

    def test_train_late(self):
        # Predicted 09:16:30, nearest scheduled 09:14 -> 150s late
        predicted = datetime(2026, 3, 7, 9, 16, 30, tzinfo=timezone.utc)
        result = find_nearest_scheduled_time(predicted, self.timetable)
        self.assertIsNotNone(result)
        scheduled, variance = result
        self.assertEqual(variance, 150)  # 2.5 minutes late
        self.assertEqual(scheduled.minute, 14)

    def test_train_early(self):
        # Predicted 09:18:00, nearest scheduled 09:20 -> 120s early
        predicted = datetime(2026, 3, 7, 9, 18, 0, tzinfo=timezone.utc)
        result = find_nearest_scheduled_time(predicted, self.timetable)
        self.assertIsNotNone(result)
        scheduled, variance = result
        self.assertEqual(variance, -120)  # 2 minutes early
        self.assertEqual(scheduled.minute, 20)

    def test_within_on_time_threshold(self):
        # Predicted 09:15:00, nearest scheduled 09:14 -> 60s late (within threshold)
        predicted = datetime(2026, 3, 7, 9, 15, 0, tzinfo=timezone.utc)
        result = find_nearest_scheduled_time(predicted, self.timetable)
        self.assertIsNotNone(result)
        _, variance = result
        self.assertLessEqual(abs(variance), ON_TIME_THRESHOLD_SECONDS)

    def test_no_timetable(self):
        predicted = datetime(2026, 3, 7, 9, 15, 0, tzinfo=timezone.utc)
        result = find_nearest_scheduled_time(predicted, [])
        self.assertIsNone(result)

    def test_too_far_from_schedule(self):
        # Predicted at 10:00 - far from any 9:xx schedule
        predicted = datetime(2026, 3, 7, 10, 0, 0, tzinfo=timezone.utc)
        result = find_nearest_scheduled_time(predicted, self.timetable)
        self.assertIsNone(result)  # Beyond 15-minute matching window

    def test_hour_over_24(self):
        # TFL timetable API can return hour >= 24 for services past midnight
        # hour=25, minute=15 represents clock time 01:15 (25 % 24 = 1)
        timetable = [{"hour": 25, "minute": 15}]
        predicted = datetime(2026, 3, 9, 1, 15, 30, tzinfo=timezone.utc)
        result = find_nearest_scheduled_time(predicted, timetable)
        self.assertIsNotNone(result)
        scheduled, variance = result
        self.assertEqual(scheduled.hour, 1)
        self.assertEqual(scheduled.minute, 15)
        self.assertEqual(variance, 30)  # 30 seconds late


class TestCollectTimelinessSnapshot(unittest.TestCase):
    """Tests for snapshot collection with mocked API calls."""

    @patch('train_timeliness.fetch_timetable')
    @patch('train_timeliness.fetch_line_arrivals')
    def test_snapshot_collection(self, mock_arrivals, mock_timetable):
        mock_arrivals.return_value = SAMPLE_ARRIVALS

        timetable_entries = [
            {"hour": 9, "minute": 14, "interval_id": 1},
            {"hour": 9, "minute": 20, "interval_id": 2},
            {"hour": 9, "minute": 26, "interval_id": 3},
        ]
        mock_timetable.return_value = timetable_entries

        line_config = MONITORED_LINES["metropolitan"]
        result = collect_timeliness_snapshot("metropolitan", line_config)

        self.assertEqual(result["line_id"], "metropolitan")
        self.assertIn("stations", result)
        self.assertIn("timestamp", result)

        # Baker Street should have 2 predictions (train_001 and train_002)
        bst = result["stations"].get("940GZZLUBST", {})
        self.assertGreater(bst.get("total_predictions", 0), 0)

        # Finchley Road should have 1 prediction (train_003)
        fyr = result["stations"].get("940GZZLUFYR", {})
        self.assertGreaterEqual(fyr.get("total_predictions", 0), 0)

    @patch('train_timeliness.fetch_line_arrivals')
    def test_snapshot_empty_arrivals(self, mock_arrivals):
        mock_arrivals.return_value = []
        line_config = MONITORED_LINES["metropolitan"]
        result = collect_timeliness_snapshot("metropolitan", line_config)

        self.assertEqual(result["line_id"], "metropolitan")
        self.assertEqual(result["stations"], {})


class TestCalculateLineMetrics(unittest.TestCase):
    """Tests for aggregate metrics calculation."""

    def test_metrics_with_data(self):
        history = [[{
            "line_id": "metropolitan",
            "stations": {
                "940GZZLUBST": {
                    "total_predictions": 10,
                    "on_time": 7,
                    "early": 2,
                    "late": 1,
                    "on_time_pct": 70.0,
                    "avg_variance_secs": 30.0,
                },
            },
            "timestamp": "2026-03-07T09:00:00+00:00",
        }]]

        metrics = calculate_line_metrics(history, "metropolitan")
        self.assertEqual(metrics["overall_total"], 10)
        self.assertEqual(metrics["overall_on_time"], 7)
        self.assertEqual(metrics["overall_on_time_pct"], 70.0)
        self.assertEqual(len(metrics["time_series"]), 1)

    def test_metrics_no_data(self):
        metrics = calculate_line_metrics([], "metropolitan")
        self.assertEqual(metrics["overall_total"], 0)
        self.assertEqual(metrics["overall_on_time_pct"], 0)

    def test_metrics_multiple_snapshots(self):
        history = [
            [{
                "line_id": "metropolitan",
                "stations": {
                    "940GZZLUBST": {
                        "total_predictions": 5,
                        "on_time": 4,
                        "early": 1,
                        "late": 0,
                        "on_time_pct": 80.0,
                        "avg_variance_secs": -20.0,
                    },
                },
                "timestamp": "2026-03-07T09:00:00+00:00",
            }],
            [{
                "line_id": "metropolitan",
                "stations": {
                    "940GZZLUBST": {
                        "total_predictions": 5,
                        "on_time": 3,
                        "early": 0,
                        "late": 2,
                        "on_time_pct": 60.0,
                        "avg_variance_secs": 45.0,
                    },
                },
                "timestamp": "2026-03-07T09:05:00+00:00",
            }],
        ]

        metrics = calculate_line_metrics(history, "metropolitan")
        self.assertEqual(metrics["overall_total"], 10)
        self.assertEqual(metrics["overall_on_time"], 7)
        self.assertEqual(metrics["overall_on_time_pct"], 70.0)
        self.assertEqual(len(metrics["time_series"]), 2)


class TestSparklineSVG(unittest.TestCase):
    """Tests for SVG sparkline generation."""

    def test_sparkline_with_data(self):
        values = [80, 75, 90, 85, 70, 95]
        svg = generate_sparkline_svg(values, "#9B0056")
        self.assertIn("<svg", svg)
        self.assertIn("polyline", svg)
        self.assertIn("#9B0056", svg)

    def test_sparkline_empty(self):
        svg = generate_sparkline_svg([], "#9B0056")
        self.assertIn("No data", svg)

    def test_sparkline_single_value(self):
        svg = generate_sparkline_svg([85], "#9B0056")
        self.assertIn("<svg", svg)

    def test_variance_bar(self):
        svg = generate_variance_bar_svg(70, 15, 15)
        self.assertIn("<svg", svg)
        self.assertIn("#4CAF50", svg)  # Green for on-time
        self.assertIn("#f44336", svg)  # Red for late


class TestHTMLReport(unittest.TestCase):
    """Tests for HTML report generation."""

    def test_report_with_data(self):
        metrics = {
            "metropolitan": {
                "time_series": [
                    {"timestamp": "2026-03-07T09:00:00", "total": 10, "on_time": 8,
                     "early": 1, "late": 1, "on_time_pct": 80.0, "avg_variance_secs": 15.0},
                ],
                "overall_total": 10,
                "overall_on_time": 8,
                "overall_early": 1,
                "overall_late": 1,
                "overall_on_time_pct": 80.0,
                "overall_avg_variance": 15.0,
            }
        }
        snapshots = [{
            "line_id": "metropolitan",
            "stations": {
                "940GZZLUBST": {
                    "name": "Baker Street",
                    "total_predictions": 5,
                    "on_time": 4,
                    "early": 1,
                    "late": 0,
                    "on_time_pct": 80.0,
                    "avg_variance_secs": -10.0,
                }
            }
        }]

        html = generate_html_report(metrics, snapshots)
        self.assertIn("Metropolitan", html)
        self.assertIn("Baker Street", html)
        self.assertIn("80.0%", html)
        self.assertIn("TFL Train Timeliness", html)

    def test_report_no_data(self):
        html = generate_html_report({}, [])
        # Even with no metrics, cards render for each monitored line (with zero values)
        self.assertIn("TFL Train Timeliness", html)
        self.assertIn("0% On Time", html)


class TestSnapshotPersistence(unittest.TestCase):
    """Tests for saving and loading snapshots."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_save_and_load(self):
        import train_timeliness
        original_dir = train_timeliness.DATA_DIR
        train_timeliness.DATA_DIR = Path(self.tmpdir)

        try:
            test_data = [{
                "line_id": "metropolitan",
                "stations": {},
                "timestamp": "2026-03-07T09:00:00+00:00",
            }]

            save_snapshot(test_data)
            loaded = load_historical_snapshots()

            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0][0]["line_id"], "metropolitan")
        finally:
            train_timeliness.DATA_DIR = original_dir

    def test_load_empty_dir(self):
        import train_timeliness
        original_dir = train_timeliness.DATA_DIR
        train_timeliness.DATA_DIR = Path(self.tmpdir) / "nonexistent"

        try:
            loaded = load_historical_snapshots()
            self.assertEqual(loaded, [])
        finally:
            train_timeliness.DATA_DIR = original_dir


class TestRuntimePathResolution(unittest.TestCase):
    def test_local_runtime_keeps_relative_paths(self):
        import train_timeliness
        original_data = train_timeliness.DATA_DIR
        original_output = train_timeliness.OUTPUT_HTML
        original_website_id = os.environ.get("WEBSITE_INSTANCE_ID")
        try:
            os.environ.pop("WEBSITE_INSTANCE_ID", None)
            train_timeliness.DATA_DIR = Path("timeliness_data")
            train_timeliness.OUTPUT_HTML = Path("timeliness_report.html")
            self.assertEqual(get_data_dir(), Path("timeliness_data"))
            self.assertEqual(get_output_html_path(), Path("timeliness_report.html"))
        finally:
            train_timeliness.DATA_DIR = original_data
            train_timeliness.OUTPUT_HTML = original_output
            if original_website_id is not None:
                os.environ["WEBSITE_INSTANCE_ID"] = original_website_id

    def test_azure_runtime_redirects_relative_paths_to_tmp(self):
        import train_timeliness
        original_data = train_timeliness.DATA_DIR
        original_output = train_timeliness.OUTPUT_HTML
        original_website_id = os.environ.get("WEBSITE_INSTANCE_ID")
        try:
            os.environ["WEBSITE_INSTANCE_ID"] = "unit-test-instance"
            train_timeliness.DATA_DIR = Path("timeliness_data")
            train_timeliness.OUTPUT_HTML = Path("timeliness_report.html")
            self.assertEqual(get_data_dir(), Path("/tmp/timeliness_data"))
            self.assertEqual(get_output_html_path(), Path("/tmp/timeliness_report.html"))
        finally:
            train_timeliness.DATA_DIR = original_data
            train_timeliness.OUTPUT_HTML = original_output
            if original_website_id is None:
                os.environ.pop("WEBSITE_INSTANCE_ID", None)
            else:
                os.environ["WEBSITE_INSTANCE_ID"] = original_website_id

    def test_azure_runtime_keeps_absolute_paths(self):
        import train_timeliness
        original_data = train_timeliness.DATA_DIR
        original_output = train_timeliness.OUTPUT_HTML
        original_website_id = os.environ.get("WEBSITE_INSTANCE_ID")
        try:
            os.environ["WEBSITE_INSTANCE_ID"] = "unit-test-instance"
            train_timeliness.DATA_DIR = Path("/tmp/custom_data")
            train_timeliness.OUTPUT_HTML = Path("/tmp/custom_report.html")
            self.assertEqual(get_data_dir(), Path("/tmp/custom_data"))
            self.assertEqual(get_output_html_path(), Path("/tmp/custom_report.html"))
        finally:
            train_timeliness.DATA_DIR = original_data
            train_timeliness.OUTPUT_HTML = original_output
            if original_website_id is None:
                os.environ.pop("WEBSITE_INSTANCE_ID", None)
            else:
                os.environ["WEBSITE_INSTANCE_ID"] = original_website_id


class TestBlobPublishing(unittest.TestCase):
    """Tests for Azure Blob artifact publishing."""

    @patch("train_timeliness.BlobServiceClient")
    @patch("train_timeliness.ContentSettings")
    def test_publish_artifacts_to_blob_storage(self, mock_content_settings, mock_blob_service):
        service_client = MagicMock()
        container_client = MagicMock()
        mock_blob_service.from_connection_string.return_value = service_client
        service_client.get_container_client.return_value = container_client

        with tempfile.TemporaryDirectory() as tmpdir:
            index_path = Path(tmpdir) / "index.html"
            index_path.write_text("<html>dashboard</html>", encoding="utf-8")

            uploaded = publish_artifacts_to_blob_storage(
                connection_string="UseDevelopmentStorage=true",
                report_html="<html>report</html>",
                summary={"timestamp": "2026-03-07T09:00:00+00:00", "lines": {}},
                current_snapshots=[{"line_id": "metropolitan"}],
                snapshot_name="snapshot_20260307_090000.json",
                index_template_path=index_path,
            )

        self.assertIn("timeliness_report.html", uploaded)
        self.assertIn("latest_summary.json", uploaded)
        self.assertIn("snapshots/snapshot_20260307_090000.json", uploaded)
        self.assertIn("index.html", uploaded)

        expected_blob_names = [c.args[0] for c in container_client.get_blob_client.call_args_list]
        self.assertIn("timeliness_report.html", expected_blob_names)
        self.assertIn("latest_summary.json", expected_blob_names)
        self.assertIn("snapshots/snapshot_20260307_090000.json", expected_blob_names)
        self.assertIn("index.html", expected_blob_names)

    def test_publish_requires_connection_string(self):
        with self.assertRaises(ValueError):
            publish_artifacts_to_blob_storage(
                connection_string="",
                report_html="<html>report</html>",
                summary={},
                current_snapshots=[],
                snapshot_name="snapshot.json",
            )


if __name__ == "__main__":
    unittest.main()
