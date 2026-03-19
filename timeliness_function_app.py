import os, logging
import azure.functions as func
from train_timeliness import run_collection, publish_artifacts_to_blob_storage

bp = func.Blueprint()


@bp.timer_trigger(schedule="0 */2 * * * *", arg_name="myTimer", run_on_startup=False, use_monitor=False)
def TrainTimelinessMonitor(myTimer: func.TimerRequest) -> None:
    payload = run_collection(return_payload=True)
    uploaded = publish_artifacts_to_blob_storage(
        connection_string=os.environ["TIMELINESS_BLOB_CONNECTION_STRING"],
        report_html=payload["report_html"],
        summary=payload["summary"],
        current_snapshots=payload["current_snapshots"],
        snapshot_name=payload["snapshot_name"],
        container_name=os.environ.get("TIMELINESS_BLOB_CONTAINER", "$web"),
    )
    logging.info("Uploaded: %s", ", ".join(uploaded))
