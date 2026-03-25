import os
import unittest
from unittest.mock import patch

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from timeliness_function_app import (
    TrainTimelinessMonitor,
    _resolve_blob_connection_string,
)


class TestTimelinessFunctionConfig(unittest.TestCase):
    def test_resolve_prefers_timeliness_connection_string(self):
        with patch.dict(
            os.environ,
            {
                "TIMELINESS_BLOB_CONNECTION_STRING": "timeliness-conn",
                "STORAGE_CONNECTION_STRING": "storage-conn",
            },
            clear=False,
        ):
            resolved = _resolve_blob_connection_string()
        self.assertEqual(resolved, "timeliness-conn")

    def test_resolve_falls_back_to_storage_connection_string(self):
        with patch.dict(os.environ, {"STORAGE_CONNECTION_STRING": "storage-conn"}, clear=True):
            resolved = _resolve_blob_connection_string()
        self.assertEqual(resolved, "storage-conn")

    def test_resolve_raises_when_no_connection_string_configured(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError) as ctx:
                _resolve_blob_connection_string()
        self.assertIn("TIMELINESS_BLOB_CONNECTION_STRING", str(ctx.exception))
        self.assertIn("STORAGE_CONNECTION_STRING", str(ctx.exception))
        self.assertIn("AzureWebJobsStorage", str(ctx.exception))

    def test_resolve_falls_back_to_azure_webjobs_storage(self):
        with patch.dict(os.environ, {"AzureWebJobsStorage": "host-storage-conn"}, clear=True):
            resolved = _resolve_blob_connection_string()
        self.assertEqual(resolved, "host-storage-conn")


class TestTrainTimelinessMonitor(unittest.TestCase):
    @patch("timeliness_function_app.publish_artifacts_to_blob_storage")
    @patch("timeliness_function_app.run_collection")
    def test_trigger_uses_fallback_storage_connection(self, mock_run_collection, mock_publish):
        mock_run_collection.return_value = {
            "summary": {"timestamp": "2026-03-21T11:40:00+00:00", "lines": {}},
            "report_html": "<html>report</html>",
            "current_snapshots": [{"line_id": "metropolitan"}],
            "snapshot_name": "snapshot_20260321_114000.json",
        }
        mock_publish.return_value = ["timeliness_report.html"]

        with patch.dict(
            os.environ,
            {
                "STORAGE_CONNECTION_STRING": "storage-conn",
            },
            clear=True,
        ):
            TrainTimelinessMonitor(None)

        call_kwargs = mock_publish.call_args.kwargs
        self.assertEqual(call_kwargs["connection_string"], "storage-conn")
        self.assertEqual(call_kwargs["container_name"], "$web")
        self.assertEqual(call_kwargs["snapshot_name"], "snapshot_20260321_114000.json")

    @patch("timeliness_function_app.publish_artifacts_to_blob_storage")
    @patch("timeliness_function_app.run_collection")
    def test_trigger_uses_explicit_timeliness_connection_and_container(self, mock_run_collection, mock_publish):
        mock_run_collection.return_value = {
            "summary": {"timestamp": "2026-03-21T11:40:00+00:00", "lines": {}},
            "report_html": "<html>report</html>",
            "current_snapshots": [{"line_id": "metropolitan"}],
            "snapshot_name": "snapshot_20260321_114000.json",
        }
        mock_publish.return_value = ["timeliness_report.html"]

        with patch.dict(
            os.environ,
            {
                "TIMELINESS_BLOB_CONNECTION_STRING": "timeliness-conn",
                "STORAGE_CONNECTION_STRING": "storage-conn",
                "TIMELINESS_BLOB_CONTAINER": "timeliness-artifacts",
            },
            clear=True,
        ):
            TrainTimelinessMonitor(None)

        call_kwargs = mock_publish.call_args.kwargs
        self.assertEqual(call_kwargs["connection_string"], "timeliness-conn")
        self.assertEqual(call_kwargs["container_name"], "timeliness-artifacts")

    @patch("timeliness_function_app.publish_artifacts_to_blob_storage")
    @patch("timeliness_function_app.run_collection")
    def test_trigger_uses_azure_webjobs_storage_when_others_missing(self, mock_run_collection, mock_publish):
        mock_run_collection.return_value = {
            "summary": {"timestamp": "2026-03-21T11:40:00+00:00", "lines": {}},
            "report_html": "<html>report</html>",
            "current_snapshots": [{"line_id": "metropolitan"}],
            "snapshot_name": "snapshot_20260321_114000.json",
        }
        mock_publish.return_value = ["timeliness_report.html"]

        with patch.dict(
            os.environ,
            {
                "AzureWebJobsStorage": "host-storage-conn",
            },
            clear=True,
        ):
            TrainTimelinessMonitor(None)

        call_kwargs = mock_publish.call_args.kwargs
        self.assertEqual(call_kwargs["connection_string"], "host-storage-conn")
        self.assertEqual(call_kwargs["container_name"], "$web")


if __name__ == "__main__":
    unittest.main()
