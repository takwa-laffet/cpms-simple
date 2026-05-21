# CPMS Simple

Minimal CPMS app that exposes charger state as JSON through a Flask API.

## What it does

- Serves the current CPMS snapshot at `GET /api/cp`
- Stores state in `cpms_data/state_snapshot.json`
- Logs charger events under `cpms_data/borne_logs/`
- Uses `PORT` from the environment, so it can run on Render

## Run locally

```bash
pip install -r requirements.txt
python app.py
```

Then open:

```text
http://127.0.0.1:5000/api/cp
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
