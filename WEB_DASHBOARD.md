TFL Status — Web dashboard deployment

Summary

This branch (web-dashboard) adds public hosting for the line-status dashboard using Azure Storage static website hosting. Changes include:

- generate_visualisations.py: builds a separate JSON payload (latest_dashboard_data.json), generates a data-driven HTML (tfl_status_dashboard_v2.html), and can publish artifacts to the $web container.
- Tests: added tests/test_generate_visualisations.py covering payload creation and publishing call paths.
- README updates: document new env vars and publish workflow.

Public URL

The site is published at:
https://tflmetropolitandata.z33.web.core.windows.net/

The dashboard fetches its data from:
https://tflmetropolitandata.z33.web.core.windows.net/latest_dashboard_data.json

How the JSON links to the page

- The generated HTML (tfl_status_dashboard_v2.html) loads a small JS snippet that fetches latest_dashboard_data.json from the same origin (no CORS necessary).
- Charts and page content are rendered client-side from that JSON payload.

Publish / run locally

1. Set STORAGE_CONNECTION_STRING in your environment (do not commit secrets).
2. Optionally set DASHBOARD_BLOB_CONNECTION_STRING or DASHBOARD_BLOB_CONTAINER to override where files are published.
3. Enable publishing and run:

   export PUBLISH_TO_BLOB=true
   python generate_visualisations.py

Security note

No storage keys, connection strings, or secrets are committed in this branch. Always set credentials via environment variables or Azure-managed identities before publishing.
