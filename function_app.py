import logging
import azure.functions as func
import requests
import json
from azure.data.tables import TableServiceClient, TableClient
from datetime import datetime, timedelta
import os


app = func.FunctionApp()

from timeliness_function_app import bp
app.register_blueprint(bp)


@app.timer_trigger(schedule="0 */2 * * * *", arg_name="myTimer", run_on_startup=False, use_monitor=False) 
def TFLMonitor(myTimer: func.TimerRequest) -> None:
    # Skip weekends
    now = datetime.now()
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        logging.info('Skipping weekend execution')
        return
    
    if myTimer.past_due:
        logging.info('Timer is past due!')
    
    # Configuration from app settings with validation
    connection_string = os.environ.get('STORAGE_CONNECTION_STRING', '')
    if not connection_string or len(connection_string) < 50:
        raise ValueError("STORAGE_CONNECTION_STRING is missing or invalid in App Settings")
    table_name = os.environ.get('TABLE_NAME', 'MetropolitanLineDelays')
    
    # State table for previous status (separate partition)
    state_table_name = 'TFLState'
    
    try:
        # Call TfL API for Metropolitan line
        url = "https://api.tfl.gov.uk/Line/metropolitan/Status"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        
        data = response.json()
        logging.info(f'TfL API response: {json.dumps(data, indent=2)}')
        
        # Parse line status (TfL returns a list; first element is the line object)
        if not isinstance(data, list) or len(data) == 0:
            logging.warning('TfL API returned no line data')
            return

        line = data[0]
        line_statuses = line.get('lineStatuses', [])
        if not line_statuses:
            logging.warning('No line statuses found')
            return

        current_status = line_statuses[0]
        status_description = current_status.get('statusSeverityDescription', 'Unknown')
        status_severity = current_status.get('statusSeverity', 10)
        reason = current_status.get('reason', '')
        affected_stops = current_status.get('affectedStops', [])
        
        logging.info(f'Metropolitan status: {status_description} (Severity: {status_severity})')
        
        # Connect to Azure Table Storage
        service_client = TableServiceClient.from_connection_string(connection_string)
        
        # Check previous status from state table
        state_table_client = service_client.get_table_client(state_table_name)
        
        # Create table if it doesn't exist
        try:
            service_client.create_table(state_table_name)
        except Exception:
            pass  # Table already exists
        
        # Get the most recent status by querying for the latest timestamp
        previous_status = None
        previous_timestamp = None
        try:
            # Query for the most recent record
            entities = state_table_client.query_entities(
                query_filter="PartitionKey eq 'metropolitan'",
                select=['previous_status', 'timestamp', 'RowKey']
            )
            # Get the latest record (sorted by timestamp)
            latest_entities = sorted(entities, key=lambda x: x['timestamp'], reverse=True)
            if latest_entities:
                latest = latest_entities[0]
                previous_status = latest['previous_status']
                previous_timestamp = datetime.fromisoformat(latest['timestamp'])
                logging.info(f'Previous status: {previous_status}')
        except Exception as e:
            logging.info(f'No previous state found: {e}')
        
        # Define delay statuses
        delay_statuses = ['Minor Delays', 'Severe Delays', 'Part Suspended', 'Planned Closure']
        
        # Detect status change and record delay
        delay_recorded = False
        
        if previous_status == 'Good Service' and status_description in delay_statuses:
            # Delay started - create new record
            delay_entity = {
                'PartitionKey': now.strftime('%Y-%m-%d'),
                'RowKey': f'metropolitan_{int(now.timestamp())}',
                'timestamp': now.isoformat(),
                'start_time': now.isoformat(),
                'end_time': None,
                'severity': status_description,
                'description': status_description,
                'duration_minutes': None,
                'reason': reason,
                'affected_stops': json.dumps([stop['name'] for stop in affected_stops])
            }
            
            table_client = service_client.get_table_client(table_name)
            try:
                service_client.create_table(table_name)
            except Exception:
                pass  # Table already exists
            table_client.upsert_entity(delay_entity)
            
            logging.info(f'RECORDED NEW DELAY: {status_description} - {reason}')
            delay_recorded = True
        
        elif previous_status in delay_statuses and status_description == 'Good Service':
            # Delay ended - update latest record
            today_partition = now.strftime('%Y-%m-%d')
            table_client = service_client.get_table_client(table_name)
            try:
                service_client.create_table(table_name)
            except Exception:
                pass  # Table already exists
            
            # Find today's latest Metropolitan record
            latest_rowkey = f'metropolitan_{int(previous_timestamp.timestamp())}'
            try:
                delay_entity = table_client.get_entity(partition_key=today_partition, row_key=latest_rowkey)
                end_time = now.isoformat()
                duration = int((now - previous_timestamp).total_seconds() / 60)
                
                delay_entity['end_time'] = end_time
                delay_entity['duration_minutes'] = duration
                
                table_client.upsert_entity(delay_entity)
                logging.info(f'UPDATED DELAY END: {duration} minutes - {delay_entity["reason"]}')
                delay_recorded = True
            except Exception as e:
                logging.warning(f'Could not update delay record: {e}')
        
        elif previous_status in delay_statuses and status_description in delay_statuses and previous_status != status_description:
            # Severity changed (e.g., Minor → Severe or Severe → Minor)
            # Close the previous delay record and start a new one
            table_client = service_client.get_table_client(table_name)
            try:
                service_client.create_table(table_name)
            except Exception:
                pass  # Table already exists
            
            # Close the previous delay record
            today_partition = now.strftime('%Y-%m-%d')
            latest_rowkey = f'metropolitan_{int(previous_timestamp.timestamp())}'
            try:
                delay_entity = table_client.get_entity(partition_key=today_partition, row_key=latest_rowkey)
                end_time = now.isoformat()
                duration = int((now - previous_timestamp).total_seconds() / 60)
                
                delay_entity['end_time'] = end_time
                delay_entity['duration_minutes'] = duration
                
                table_client.upsert_entity(delay_entity)
                logging.info(f'CLOSED PREVIOUS DELAY: {previous_status} - {duration} minutes')
            except Exception as e:
                logging.warning(f'Could not update previous delay record: {e}')
            
            # Create new delay record with new severity
            new_delay_entity = {
                'PartitionKey': now.strftime('%Y-%m-%d'),
                'RowKey': f'metropolitan_{int(now.timestamp())}',
                'timestamp': now.isoformat(),
                'start_time': now.isoformat(),
                'end_time': None,
                'severity': status_description,
                'description': status_description,
                'duration_minutes': None,
                'reason': reason,
                'affected_stops': json.dumps([stop['name'] for stop in affected_stops])
            }
            
            table_client.upsert_entity(new_delay_entity)
            logging.info(f'RECORDED SEVERITY CHANGE: {previous_status} → {status_description} - {reason}')
            delay_recorded = True
        
        # Always append new state record with timestamp-based RowKey
        state_entity = {
            'PartitionKey': 'metropolitan',
            'RowKey': f'status_{int(now.timestamp())}',
            'previous_status': status_description,
            'timestamp': now.isoformat(),
            'severity': status_severity,
            'reason': reason
        }
        
        state_table_client.upsert_entity(state_entity)
        logging.info(f'State recorded: {status_description}')
        
    except requests.exceptions.RequestException as e:
        logging.error(f'TfL API error: {e}')
    except Exception as e:
        logging.error(f'Unexpected error: {str(e)}')
        raise


@app.timer_trigger(schedule="0 */2 * * * *", arg_name="myTimer", run_on_startup=False, use_monitor=False)
def BakerlooMonitor(myTimer: func.TimerRequest) -> None:
    now = datetime.now()
    if now.weekday() >= 5:
        logging.info('Skipping weekend execution (Bakerloo)')
        return

    if myTimer.past_due:
        logging.info('Timer is past due! (Bakerloo)')

    connection_string = os.environ.get('STORAGE_CONNECTION_STRING', '')
    if not connection_string or len(connection_string) < 50:
        raise ValueError("STORAGE_CONNECTION_STRING is missing or invalid in App Settings")
    table_name = 'BakLineDelays'
    state_table_name = 'TFLState'

    try:
        url = "https://api.tfl.gov.uk/Line/bakerloo/Status"
        response = requests.get(url, timeout=10)
        response.raise_for_status()

        data = response.json()
        logging.info(f'TfL API response (Bakerloo): {json.dumps(data, indent=2)}')

        if not isinstance(data, list) or len(data) == 0:
            logging.warning('TfL API returned no line data (Bakerloo)')
            return

        line = data[0]
        line_statuses = line.get('lineStatuses', [])
        if not line_statuses:
            logging.warning('No line statuses found (Bakerloo)')
            return

        current_status = line_statuses[0]
        status_description = current_status.get('statusSeverityDescription', 'Unknown')
        status_severity = current_status.get('statusSeverity', 10)
        reason = current_status.get('reason', '')
        affected_stops = current_status.get('affectedStops', [])

        logging.info(f'Bakerloo status: {status_description} (Severity: {status_severity})')

        service_client = TableServiceClient.from_connection_string(connection_string)
        state_table_client = service_client.get_table_client(state_table_name)

        try:
            service_client.create_table(state_table_name)
        except Exception:
            pass

        previous_status = None
        previous_timestamp = None
        try:
            entities = state_table_client.query_entities(
                query_filter="PartitionKey eq 'bakerloo'",
                select=['previous_status', 'timestamp', 'RowKey']
            )
            latest_entities = sorted(entities, key=lambda x: x['timestamp'], reverse=True)
            if latest_entities:
                latest = latest_entities[0]
                previous_status = latest['previous_status']
                previous_timestamp = datetime.fromisoformat(latest['timestamp'])
                logging.info(f'Previous status (Bakerloo): {previous_status}')
        except Exception as e:
            logging.info(f'No previous state found (Bakerloo): {e}')

        delay_statuses = ['Minor Delays', 'Severe Delays', 'Part Suspended', 'Planned Closure']
        delay_recorded = False

        if previous_status == 'Good Service' and status_description in delay_statuses:
            delay_entity = {
                'PartitionKey': now.strftime('%Y-%m-%d'),
                'RowKey': f'bakerloo_{int(now.timestamp())}',
                'timestamp': now.isoformat(),
                'start_time': now.isoformat(),
                'end_time': None,
                'severity': status_description,
                'description': status_description,
                'duration_minutes': None,
                'reason': reason,
                'affected_stops': json.dumps([stop['name'] for stop in affected_stops])
            }
            table_client = service_client.get_table_client(table_name)
            try:
                service_client.create_table(table_name)
            except Exception:
                pass
            table_client.upsert_entity(delay_entity)
            logging.info(f'RECORDED NEW DELAY (Bakerloo): {status_description} - {reason}')
            delay_recorded = True

        elif previous_status in delay_statuses and status_description == 'Good Service':
            today_partition = now.strftime('%Y-%m-%d')
            table_client = service_client.get_table_client(table_name)
            try:
                service_client.create_table(table_name)
            except Exception:
                pass
            latest_rowkey = f'bakerloo_{int(previous_timestamp.timestamp())}'
            try:
                delay_entity = table_client.get_entity(partition_key=today_partition, row_key=latest_rowkey)
                end_time = now.isoformat()
                duration = int((now - previous_timestamp).total_seconds() / 60)
                delay_entity['end_time'] = end_time
                delay_entity['duration_minutes'] = duration
                table_client.upsert_entity(delay_entity)
                logging.info(f'UPDATED DELAY END (Bakerloo): {duration} minutes - {delay_entity["reason"]}')
                delay_recorded = True
            except Exception as e:
                logging.warning(f'Could not update delay record (Bakerloo): {e}')

        elif previous_status in delay_statuses and status_description in delay_statuses and previous_status != status_description:
            table_client = service_client.get_table_client(table_name)
            try:
                service_client.create_table(table_name)
            except Exception:
                pass
            today_partition = now.strftime('%Y-%m-%d')
            latest_rowkey = f'bakerloo_{int(previous_timestamp.timestamp())}'
            try:
                delay_entity = table_client.get_entity(partition_key=today_partition, row_key=latest_rowkey)
                end_time = now.isoformat()
                duration = int((now - previous_timestamp).total_seconds() / 60)
                delay_entity['end_time'] = end_time
                delay_entity['duration_minutes'] = duration
                table_client.upsert_entity(delay_entity)
                logging.info(f'CLOSED PREVIOUS DELAY (Bakerloo): {previous_status} - {duration} minutes')
            except Exception as e:
                logging.warning(f'Could not update previous delay record (Bakerloo): {e}')
            new_delay_entity = {
                'PartitionKey': now.strftime('%Y-%m-%d'),
                'RowKey': f'bakerloo_{int(now.timestamp())}',
                'timestamp': now.isoformat(),
                'start_time': now.isoformat(),
                'end_time': None,
                'severity': status_description,
                'description': status_description,
                'duration_minutes': None,
                'reason': reason,
                'affected_stops': json.dumps([stop['name'] for stop in affected_stops])
            }
            table_client.upsert_entity(new_delay_entity)
            logging.info(f'RECORDED SEVERITY CHANGE (Bakerloo): {previous_status} → {status_description} - {reason}')
            delay_recorded = True

        state_entity = {
            'PartitionKey': 'bakerloo',
            'RowKey': f'status_{int(now.timestamp())}',
            'previous_status': status_description,
            'timestamp': now.isoformat(),
            'severity': status_severity,
            'reason': reason
        }
        state_table_client.upsert_entity(state_entity)
        logging.info(f'State recorded (Bakerloo): {status_description}')

    except requests.exceptions.RequestException as e:
        logging.error(f'TfL API error (Bakerloo): {e}')
    except Exception as e:
        logging.error(f'Unexpected error (Bakerloo): {str(e)}')
        raise
# ...existing code...

@app.timer_trigger(schedule="0 */2 * * * *", arg_name="myTimer", run_on_startup=False, use_monitor=False)
def HammersmithCityMonitor(myTimer: func.TimerRequest) -> None:
    now = datetime.now()
    if now.weekday() >= 5:
        logging.info('Skipping weekend execution (Hammersmith & City)')
        return

    if myTimer.past_due:
        logging.info('Timer is past due! (Hammersmith & City)')

    connection_string = os.environ.get('STORAGE_CONNECTION_STRING', '')
    if not connection_string or len(connection_string) < 50:
        raise ValueError("STORAGE_CONNECTION_STRING is missing or invalid in App Settings")
    table_name = 'H&CDelays'
    state_table_name = 'TFLState'

    try:
        url = "https://api.tfl.gov.uk/Line/hammersmith-city/Status"
        response = requests.get(url, timeout=10)
        response.raise_for_status()

        data = response.json()
        logging.info(f'TfL API response (Hammersmith & City): {json.dumps(data, indent=2)}')

        if not isinstance(data, list) or len(data) == 0:
            logging.warning('TfL API returned no line data (Hammersmith & City)')
            return

        line = data[0]
        line_statuses = line.get('lineStatuses', [])
        if not line_statuses:
            logging.warning('No line statuses found (Hammersmith & City)')
            return

        current_status = line_statuses[0]
        status_description = current_status.get('statusSeverityDescription', 'Unknown')
        status_severity = current_status.get('statusSeverity', 10)
        reason = current_status.get('reason', '')
        affected_stops = current_status.get('affectedStops', [])

        logging.info(f'Hammersmith & City status: {status_description} (Severity: {status_severity})')

        service_client = TableServiceClient.from_connection_string(connection_string)
        state_table_client = service_client.get_table_client(state_table_name)

        try:
            service_client.create_table(state_table_name)
        except Exception:
            pass

        previous_status = None
        previous_timestamp = None
        try:
            entities = state_table_client.query_entities(
                query_filter="PartitionKey eq 'hammersmith-city'",
                select=['previous_status', 'timestamp', 'RowKey']
            )
            latest_entities = sorted(entities, key=lambda x: x['timestamp'], reverse=True)
            if latest_entities:
                latest = latest_entities[0]
                previous_status = latest['previous_status']
                previous_timestamp = datetime.fromisoformat(latest['timestamp'])
                logging.info(f'Previous status (Hammersmith & City): {previous_status}')
        except Exception as e:
            logging.info(f'No previous state found (Hammersmith & City): {e}')

        delay_statuses = ['Minor Delays', 'Severe Delays', 'Part Suspended', 'Planned Closure']
        delay_recorded = False

        if previous_status == 'Good Service' and status_description in delay_statuses:
            delay_entity = {
                'PartitionKey': now.strftime('%Y-%m-%d'),
                'RowKey': f'hammersmith-city_{int(now.timestamp())}',
                'timestamp': now.isoformat(),
                'start_time': now.isoformat(),
                'end_time': None,
                'severity': status_description,
                'description': status_description,
                'duration_minutes': None,
                'reason': reason,
                'affected_stops': json.dumps([stop['name'] for stop in affected_stops])
            }
            table_client = service_client.get_table_client(table_name)
            try:
                service_client.create_table(table_name)
            except Exception:
                pass
            table_client.upsert_entity(delay_entity)
            logging.info(f'RECORDED NEW DELAY (Hammersmith & City): {status_description} - {reason}')
            delay_recorded = True

        elif previous_status in delay_statuses and status_description == 'Good Service':
            today_partition = now.strftime('%Y-%m-%d')
            table_client = service_client.get_table_client(table_name)
            try:
                service_client.create_table(table_name)
            except Exception:
                pass
            latest_rowkey = f'hammersmith-city_{int(previous_timestamp.timestamp())}'
            try:
                delay_entity = table_client.get_entity(partition_key=today_partition, row_key=latest_rowkey)
                end_time = now.isoformat()
                duration = int((now - previous_timestamp).total_seconds() / 60)
                delay_entity['end_time'] = end_time
                delay_entity['duration_minutes'] = duration
                table_client.upsert_entity(delay_entity)
                logging.info(f'UPDATED DELAY END (Hammersmith & City): {duration} minutes - {delay_entity["reason"]}')
                delay_recorded = True
            except Exception as e:
                logging.warning(f'Could not update delay record (Hammersmith & City): {e}')

        elif previous_status in delay_statuses and status_description in delay_statuses and previous_status != status_description:
            table_client = service_client.get_table_client(table_name)
            try:
                service_client.create_table(table_name)
            except Exception:
                pass
            today_partition = now.strftime('%Y-%m-%d')
            latest_rowkey = f'hammersmith-city_{int(previous_timestamp.timestamp())}'
            try:
                delay_entity = table_client.get_entity(partition_key=today_partition, row_key=latest_rowkey)
                end_time = now.isoformat()
                duration = int((now - previous_timestamp).total_seconds() / 60)
                delay_entity['end_time'] = end_time
                delay_entity['duration_minutes'] = duration
                table_client.upsert_entity(delay_entity)
                logging.info(f'CLOSED PREVIOUS DELAY (Hammersmith & City): {previous_status} - {duration} minutes')
            except Exception as e:
                logging.warning(f'Could not update previous delay record (Hammersmith & City): {e}')
            new_delay_entity = {
                'PartitionKey': now.strftime('%Y-%m-%d'),
                'RowKey': f'hammersmith-city_{int(now.timestamp())}',
                'timestamp': now.isoformat(),
                'start_time': now.isoformat(),
                'end_time': None,
                'severity': status_description,
                'description': status_description,
                'duration_minutes': None,
                'reason': reason,
                'affected_stops': json.dumps([stop['name'] for stop in affected_stops])
            }
            table_client.upsert_entity(new_delay_entity)
            logging.info(f'RECORDED SEVERITY CHANGE (Hammersmith & City): {previous_status} → {status_description} - {reason}')
            delay_recorded = True

        state_entity = {
            'PartitionKey': 'hammersmith-city',
            'RowKey': f'status_{int(now.timestamp())}',
            'previous_status': status_description,
            'timestamp': now.isoformat(),
            'severity': status_severity,
            'reason': reason
        }
        state_table_client.upsert_entity(state_entity)
        logging.info(f'State recorded (Hammersmith & City): {status_description}')

    except requests.exceptions.RequestException as e:
        logging.error(f'TfL API error (Hammersmith & City): {e}')
    except Exception as e:
        logging.error(f'Unexpected error (Hammersmith & City): {str(e)}')
        raise

@app.timer_trigger(schedule="0 */2 * * * *", arg_name="myTimer", run_on_startup=False, use_monitor=False)
def CircleMonitor(myTimer: func.TimerRequest) -> None:
    now = datetime.now()
    if now.weekday() >= 5:
        logging.info('Skipping weekend execution (Circle)')
        return

    if myTimer.past_due:
        logging.info('Timer is past due! (Circle)')

    connection_string = os.environ.get('STORAGE_CONNECTION_STRING', '')
    if not connection_string or len(connection_string) < 50:
        raise ValueError("STORAGE_CONNECTION_STRING is missing or invalid in App Settings")
    table_name = 'CircleDelays'
    state_table_name = 'TFLState'

    try:
        url = "https://api.tfl.gov.uk/Line/circle/Status"
        response = requests.get(url, timeout=10)
        response.raise_for_status()

        data = response.json()
        logging.info(f'TfL API response (Circle): {json.dumps(data, indent=2)}')

        if not isinstance(data, list) or len(data) == 0:
            logging.warning('TfL API returned no line data (Circle)')
            return

        line = data[0]
        line_statuses = line.get('lineStatuses', [])
        if not line_statuses:
            logging.warning('No line statuses found (Circle)')
            return

        current_status = line_statuses[0]
        status_description = current_status.get('statusSeverityDescription', 'Unknown')
        status_severity = current_status.get('statusSeverity', 10)
        reason = current_status.get('reason', '')
        affected_stops = current_status.get('affectedStops', [])

        logging.info(f'Circle status: {status_description} (Severity: {status_severity})')

        service_client = TableServiceClient.from_connection_string(connection_string)
        state_table_client = service_client.get_table_client(state_table_name)

        try:
            service_client.create_table(state_table_name)
        except Exception:
            pass

        previous_status = None
        previous_timestamp = None
        try:
            entities = state_table_client.query_entities(
                query_filter="PartitionKey eq 'circle'",
                select=['previous_status', 'timestamp', 'RowKey']
            )
            latest_entities = sorted(entities, key=lambda x: x['timestamp'], reverse=True)
            if latest_entities:
                latest = latest_entities[0]
                previous_status = latest['previous_status']
                previous_timestamp = datetime.fromisoformat(latest['timestamp'])
                logging.info(f'Previous status (Circle): {previous_status}')
        except Exception as e:
            logging.info(f'No previous state found (Circle): {e}')

        delay_statuses = ['Minor Delays', 'Severe Delays', 'Part Suspended', 'Planned Closure']
        delay_recorded = False

        if previous_status == 'Good Service' and status_description in delay_statuses:
            delay_entity = {
                'PartitionKey': now.strftime('%Y-%m-%d'),
                'RowKey': f'circle_{int(now.timestamp())}',
                'timestamp': now.isoformat(),
                'start_time': now.isoformat(),
                'end_time': None,
                'severity': status_description,
                'description': status_description,
                'duration_minutes': None,
                'reason': reason,
                'affected_stops': json.dumps([stop['name'] for stop in affected_stops])
            }
            table_client = service_client.get_table_client(table_name)
            try:
                service_client.create_table(table_name)
            except Exception:
                pass
            table_client.upsert_entity(delay_entity)
            logging.info(f'RECORDED NEW DELAY (Circle): {status_description} - {reason}')
            delay_recorded = True

        elif previous_status in delay_statuses and status_description == 'Good Service':
            today_partition = now.strftime('%Y-%m-%d')
            table_client = service_client.get_table_client(table_name)
            try:
                service_client.create_table(table_name)
            except Exception:
                pass
            latest_rowkey = f'circle_{int(previous_timestamp.timestamp())}'
            try:
                delay_entity = table_client.get_entity(partition_key=today_partition, row_key=latest_rowkey)
                end_time = now.isoformat()
                duration = int((now - previous_timestamp).total_seconds() / 60)
                delay_entity['end_time'] = end_time
                delay_entity['duration_minutes'] = duration
                table_client.upsert_entity(delay_entity)
                logging.info(f'UPDATED DELAY END (Circle): {duration} minutes - {delay_entity["reason"]}')
                delay_recorded = True
            except Exception as e:
                logging.warning(f'Could not update delay record (Circle): {e}')

        elif previous_status in delay_statuses and status_description in delay_statuses and previous_status != status_description:
            table_client = service_client.get_table_client(table_name)
            try:
                service_client.create_table(table_name)
            except Exception:
                pass
            today_partition = now.strftime('%Y-%m-%d')
            latest_rowkey = f'circle_{int(previous_timestamp.timestamp())}'
            try:
                delay_entity = table_client.get_entity(partition_key=today_partition, row_key=latest_rowkey)
                end_time = now.isoformat()
                duration = int((now - previous_timestamp).total_seconds() / 60)
                delay_entity['end_time'] = end_time
                delay_entity['duration_minutes'] = duration
                table_client.upsert_entity(delay_entity)
                logging.info(f'CLOSED PREVIOUS DELAY (Circle): {previous_status} - {duration} minutes')
            except Exception as e:
                logging.warning(f'Could not update previous delay record (Circle): {e}')
            new_delay_entity = {
                'PartitionKey': now.strftime('%Y-%m-%d'),
                'RowKey': f'circle_{int(now.timestamp())}',
                'timestamp': now.isoformat(),
                'start_time': now.isoformat(),
                'end_time': None,
                'severity': status_description,
                'description': status_description,
                'duration_minutes': None,
                'reason': reason,
                'affected_stops': json.dumps([stop['name'] for stop in affected_stops])
            }
            table_client.upsert_entity(new_delay_entity)
            logging.info(f'RECORDED SEVERITY CHANGE (Circle): {previous_status} → {status_description} - {reason}')
            delay_recorded = True

        state_entity = {
            'PartitionKey': 'circle',
            'RowKey': f'status_{int(now.timestamp())}',
            'previous_status': status_description,
            'timestamp': now.isoformat(),
            'severity': status_severity,
            'reason': reason
        }
        state_table_client.upsert_entity(state_entity)
        logging.info(f'State recorded (Circle): {status_description}')

    except requests.exceptions.RequestException as e:
        logging.error(f'TfL API error (Circle): {e}')
    except Exception as e:
        logging.error(f'Unexpected error (Circle): {str(e)}')
        raise
