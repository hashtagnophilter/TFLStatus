import unittest
from unittest.mock import MagicMock, patch

from generate_visualisations import build_dashboard_payload, publish_dashboard_to_blob_storage


class TestDashboardPayload(unittest.TestCase):
    def test_build_dashboard_payload_includes_summary_fields(self):
        payload = build_dashboard_payload(
            reliability={
                "metropolitan": {"good_pct": 80.2, "total_checks": 100, "breakdown": {}},
                "bakerloo": {"good_pct": 90.0, "total_checks": 50, "breakdown": {}},
            },
            heatmap={"metropolitan": [{"x": 5, "y": 0, "v": 10.0}]},
            correlation={"metropolitan": {"bakerloo": 12.5}},
            causes={"Signal Failure": 3, "Customer Incident": 1},
            causes_by_line={},
            daily_disruption={"2026-03-01": 10.0, "2026-03-02": 20.0},
            line_daily={"metropolitan": {"2026-03-01": 10.0}},
            peak_offpeak={"metropolitan": {"Off-Peak": 7.0}},
            escalations={"metropolitan": {"Minor Delays → Severe Delays": 2}},
            streaks={"metropolitan": {"distribution": {"10": 1}}},
            weekly_trend={"metropolitan": [{"week": "2026-W09", "pct": 12.5}]},
        )

        self.assertEqual(payload["dateFrom"], "2026-03-01")
        self.assertEqual(payload["dateTo"], "2026-03-02")
        self.assertEqual(payload["totalChecks"], 150)
        self.assertEqual(payload["totalDelays"], 4)
        self.assertEqual(payload["causeLabels"], ["Signal Failure", "Customer Incident"])
        self.assertEqual(payload["allWeeks"], ["2026-W09"])
        self.assertEqual(payload["streakDist"], {"metropolitan": {"10": 1}})


class TestDashboardBlobPublishing(unittest.TestCase):
    @patch("generate_visualisations.BlobServiceClient")
    @patch("generate_visualisations.ContentSettings")
    def test_publish_dashboard_to_blob_storage_uploads_expected_blobs(
        self,
        mock_content_settings,
        mock_blob_service,
    ):
        service_client = MagicMock()
        container_client = MagicMock()
        mock_blob_service.from_connection_string.return_value = service_client
        service_client.get_container_client.return_value = container_client

        uploaded = publish_dashboard_to_blob_storage(
            connection_string="UseDevelopmentStorage=true",
            dashboard_html="<html>dashboard</html>",
            dashboard_payload={"hello": "world"},
        )

        self.assertEqual(
            uploaded,
            ["tfl_status_dashboard_v2.html", "index.html", "latest_dashboard_data.json"],
        )
        self.assertEqual(
            [call.args[0] for call in container_client.get_blob_client.call_args_list],
            ["tfl_status_dashboard_v2.html", "index.html", "latest_dashboard_data.json"],
        )
        self.assertEqual(container_client.get_blob_client.return_value.upload_blob.call_count, 3)

    def test_publish_dashboard_to_blob_storage_requires_connection_string(self):
        with self.assertRaises(ValueError):
            publish_dashboard_to_blob_storage(
                connection_string="",
                dashboard_html="<html>dashboard</html>",
                dashboard_payload={},
            )


if __name__ == "__main__":
    unittest.main()
