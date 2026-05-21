# Deploying cpms-simple to Render

This file documents all steps to deploy `cpms-simple` to Render.com.

## Prerequisites

- A GitHub repository with your project (e.g., `takwa-laffet/cpms-simple`).
- `requirements.txt` in the repo (this project already contains one).
- A Render account (https://render.com/) and permission to connect GitHub.

---

## Create a Web Service on Render

1. Go to the Render dashboard and click **New** → **Web Service**.
2. Select **Connect a repository** and choose your GitHub repo `takwa-laffet/cpms-simple`.
3. Choose branch: `main` (or whichever branch you want to deploy).
4. Fill service details:
   - Name: `cpms-simple` (or your chosen name)
   - Region: choose closest region
   - Environment: **Python**

5. Set the Build Command:

```
pip install -r requirements.txt
```

6. Set the Start Command:

```
uvicorn app:app --host 0.0.0.0 --port $PORT
```

Render automatically injects the `$PORT` environment variable.

7. (Optional) Set Environment Variables in the Render UI if needed (none are required by default).

8. Click **Create Web Service** and wait for Build + Deploy to complete.

---

## Verify the deployment

- Open the public URL provided by Render and append the HTTP endpoint:

```
https://<your-service>.onrender.com/api/cp
```

You should receive the JSON snapshot produced by the collector.

---

## WebSocket (OCPP) notes

- This project accepts OCPP websocket connections on the same ASGI server path when the `ocpp` package is installed. That means chargers can connect to:

```text
wss://<your-service>.onrender.com/<borne_id>
```

- This is recommended for Render since Render exposes a single public port. The app uses a FastAPI websocket route to handle OCPP connections through the same `uvicorn` process.

---

## Recommended Render configuration for WSS on the same domain

- Best experience: integrate the OCPP websocket handling into the same ASGI app (single `uvicorn` process). If you want, I will refactor the code to accept OCPP websocket connections through the same uvicorn/ASGI server and a path like `/<borne_id>`. That will allow public `wss://<your-service>.onrender.com/<borne_id>`.

---

## Local testing steps

1. Install dependencies locally:

```bash
pip install -r requirements.txt
```

2. Run the app and (optionally) separate websocket server locally:

```bash
# Run HTTP ASGI on port 5000 (local)
uvicorn app:app --host 0.0.0.0 --port 5000

# (Optional) run the separate websocket server if WS_PORT used by app startup
WS_PORT=9000 python app.py
```

- Visit `http://127.0.0.1:5000/api/cp`.
- Local websocket (if separate): `ws://127.0.0.1:9000/<borne_id>`.

---

## Troubleshooting

- Build fails: check `requirements.txt` for typos or missing packages.
- App crashes on start: review Render Live Logs for Python exceptions.
- WebSocket unreachable: remember Render exposes only the main service port; use the same ASGI port for WSS to be reachable.

---

## Quick checklist to deploy successfully

- [ ] `requirements.txt` is present and correct
- [ ] Start command uses `$PORT` (see Start Command above)
- [ ] Branch is correctly selected in Render
- [ ] Environment variables set if needed
- [ ] Confirm whether you need public `wss://` and choose integration option

---

If you want, I can now:

- Refactor the OCPP websocket handling to run over the same ASGI port/path so chargers can connect with `wss://<your-service>.onrender.com/<borne_id>` (I recommend this for Render deployments).
- Or I can provide a small test script that simulates a charger connecting to the websocket server.

Which would you like next?
