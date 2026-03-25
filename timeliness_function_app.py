import logging
import os
import azure.functions as func
from train_timeliness import run_collection, publish_artifacts_to_blob_storage

bp = func.Blueprint()

TIMELINESS_CONNECTION_ENV = "TIMELINESS_BLOB_CONNECTION_STRING"
FALLBACK_CONNECTION_ENV = "STORAGE_CONNECTION_STRING"
HOST_STORAGE_CONNECTION_ENV = "AzureWebJobsStorage"


def _resolve_blob_connection_string() -> str:
    configured = os.environ.get(TIMELINESS_CONNECTION_ENV, "").strip()
    if configured:
        return configured

    fallback = os.environ.get(FALLBACK_CONNECTION_ENV, "").strip()
    if fallback:
        logging.info(
            "%s not set; using %s for timeliness blob publishing",
            TIMELINESS_CONNECTION_ENV,
            FALLBACK_CONNECTION_ENV,
        )
        return fallback

    host_storage = os.environ.get(HOST_STORAGE_CONNECTION_ENV, "").strip()
    if host_storage:
        logging.info(
            "%s and %s not set; using %s for timeliness blob publishing",
            TIMELINESS_CONNECTION_ENV,
            FALLBACK_CONNECTION_ENV,
            HOST_STORAGE_CONNECTION_ENV,
        )
        return host_storage

    raise ValueError(
        f"Missing blob connection string. Set {TIMELINESS_CONNECTION_ENV} "
        f"or {FALLBACK_CONNECTION_ENV} or {HOST_STORAGE_CONNECTION_ENV} in app settings."
    )


@bp.timer_trigger(schedule="0 */2 * * * *", arg_name="myTimer", run_on_startup=False, use_monitor=False)
def TrainTimelinessMonitor(myTimer: func.TimerRequest) -> None:
    connection_string = _resolve_blob_connection_string()
    payload = run_collection(return_payload=True)
    uploaded = publish_artifacts_to_blob_storage(
        connection_string=connection_string,
        report_html=payload["report_html"],
        summary=payload["summary"],
        current_snapshots=payload["current_snapshots"],
        snapshot_name=payload["snapshot_name"],
        container_name=os.environ.get("TIMELINESS_BLOB_CONTAINER", "$web"),
    )
    logging.info("Uploaded: %s", ", ".join(uploaded))
