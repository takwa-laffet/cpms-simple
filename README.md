# CityOs

Minimal CPMS backend that exposes charger state as JSON through a FastAPI API.

## What it does

- Serves a dashboard frontend at `GET /`
- Exposes a backend health response at `GET /health`
- Exposes charger state and sessions at `GET /api/cp`
- Lets you set charger metadata at `GET/POST /api/cp/{cp_id}/meta`
- Lets you start charging server-side with `POST /api/cp/{cp_id}/force_start`
- Lets you stop charging server-side with `POST /api/cp/{cp_id}/force_stop`
- Lets you send a real OCPP remote start with `POST /api/cp/{cp_id}/remote_start`
- Lets you send a real OCPP remote stop with `POST /api/cp/{cp_id}/remote_stop`
- Lets you reboot the borne with `POST /api/cp/{cp_id}/remote_reboot`
- Lets you unlock a connector with `POST /api/cp/{cp_id}/unlock_connector`


## Run locally

```bash
pip install -r requirements.txt
python app.py
```

Then open:

```text
http://127.0.0.1:5000/
```

The API snapshot remains available at:

```text
http://127.0.0.1:5000/api/cp
```

## Force start/stop

You can force-start or force-stop a transaction on the server without sending OCPP Start/Stop or id_tags.

`force_start` now tries a real OCPP `RemoteStartTransaction` first when the charger is connected, then falls back to server-side tracking if the charger is offline.

Important:

- `force_start` prefers a real charger start when possible.
- It does not require `id_tag`.
- If the charger is not connected, it still creates a CPMS-side transaction.

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

Important:

- The simulator app appends the Charge Point ID to the base websocket URL.
- Use a trailing slash in the base URL, for example `wss://cpms-simple.onrender.com/`.
- Do not enter only `wss://cpms-simple.onrender.com` without the slash, or the simulator will build an invalid URL and the socket will close with code `1006`.

## Remote start charging

If the charger is connected, you can ask the CPMS to send a real OCPP `RemoteStartTransaction`:

```bash
curl -X POST "http://127.0.0.1:5000/api/cp/CP001/remote_start?connector_id=1"
```

By default the API uses a server-side `id_tag` value, so the caller does not need to provide one.
The `connector_id` is optional. If the charger rejects the first request, the CPMS retries once without `connector_id` because some chargers only accept the remote start at station level.

If you want to try without a connector number:

```bash
curl -X POST "http://127.0.0.1:5000/api/cp/CP001/remote_start"
```

To stop a remote session:

```bash
curl -X POST "http://127.0.0.1:5000/api/cp/CP001/remote_stop?transaction_id=1"
```

To reboot the borne:

```bash
curl -X POST "http://127.0.0.1:5000/api/cp/CP001/remote_reboot?reset_type=Soft"
```

To unlock a connector:

```bash
curl -X POST "http://127.0.0.1:5000/api/cp/CP001/unlock_connector?connector_id=1"
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

- `app.py`: FastAPI app, CPMS data collector, and frontend static serving
- `frontend/`: dashboard HTML, CSS, and client-side API logic
- `requirements.txt`: Python dependencies
- `cpms_data/`: generated JSON logs and snapshots
