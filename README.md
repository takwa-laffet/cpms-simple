# CPMS Simple

Minimal CPMS app that exposes charger state as JSON through a Flask API.

## What it does

- Exposes charger state and sessions at `GET /api/cp`
- Lets you set charger metadata at `GET/POST /api/cp/{cp_id}/meta`
- Lets you start charging server-side with `POST /api/cp/{cp_id}/force_start`
- Lets you stop charging server-side with `POST /api/cp/{cp_id}/force_stop`
- Lets you send a real OCPP remote start with `POST /api/cp/{cp_id}/remote_start`
- Lets you send a real OCPP remote stop with `POST /api/cp/{cp_id}/remote_stop`


## Run locally

```bash
pip install -r requirements.txt
python app.py
```

Then open:

```text
http://127.0.0.1:5000/api/cp
```

## Force start/stop (server-side)

You can force-start or force-stop a transaction on the server without sending OCPP Start/Stop or id_tags.

This is the current way to start charging in this project.

Important:

- `force_start` creates a transaction in CPMS even if the charger is not sending a StartTransaction message.
- It does not require `id_tag`.
- It is server-side tracking, not a real OCPP remote command to the charger.
- If you want the charger itself to start via OCPP RemoteStartTransaction, I can add that next.

Start:

```bash
curl -X POST http://127.0.0.1:5000/api/cp/borne1/force_start -d "connector_id=1&meter_start=0"
```

Stop:

```bash
curl -X POST http://127.0.0.1:5000/api/cp/borne1/force_stop -d "transaction_id=1&meter_stop=100"
```

WebSocket (OCPP) server (optional):

The application accepts OCPP websocket connections on the same ASGI server. When `ocpp` is installed, connect chargers to:

```text
wss://<your-service>.onrender.com/<borne_id>
```

## Remote start charging

If the charger is connected, you can ask the CPMS to send a real OCPP `RemoteStartTransaction`:

```bash
curl -X POST "http://127.0.0.1:5000/api/cp/CP001/remote_start?connector_id=1"
```

By default the API uses a server-side `id_tag` value, so the caller does not need to provide one.

To stop a remote session:

```bash
curl -X POST "http://127.0.0.1:5000/api/cp/CP001/remote_stop?transaction_id=1"
```

## Serial number of the borne

The serial number is exposed in `/api/cp` and `/api/cp?cp_id=CP001` through:

- `general_info_by_cp.CP001.serial_number`
- `serial_number_by_cp.CP001`
- `boot_notifications_by_cp.CP001`

If the charger sends the serial number in `BootNotification`, it is stored automatically.
If not, you can set it manually with:

```bash
curl -X POST "http://127.0.0.1:5000/api/cp/CP001/meta" \
	-H "Content-Type: application/json" \
	-d '{"serialNumber":"SN-0001"}'
```

## Render deploy

- Build command: `pip install -r requirements.txt`
- Start command: `python app.py`

## Files

- `app.py`: Flask app and CPMS data collector
- `requirements.txt`: Python dependencies
- `cpms_data/`: generated JSON logs and snapshots
