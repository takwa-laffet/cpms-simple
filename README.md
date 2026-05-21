# CPMS Simple

Minimal CPMS app that exposes charger state as JSON through a Flask API.

## What it does


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

## Render deploy

- Build command: `pip install -r requirements.txt`
- Start command: `python app.py`

## Files

- `app.py`: Flask app and CPMS data collector
- `requirements.txt`: Python dependencies
- `cpms_data/`: generated JSON logs and snapshots
