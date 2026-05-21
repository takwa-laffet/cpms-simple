import asyncio
import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
import uvicorn

try:
    import websockets
    from ocpp.routing import on  # pyright: ignore[reportMissingImports]
    from ocpp.v16 import ChargePoint as cp  # pyright: ignore[reportMissingImports]
    from ocpp.v16 import call_result  # pyright: ignore[reportMissingImports]
    from ocpp.v16.enums import (  # pyright: ignore[reportMissingImports]
        AuthorizationStatus,
        DataTransferStatus,
        RegistrationStatus,
    )
    from websockets.exceptions import ConnectionClosed

    OCPP_AVAILABLE = True
except ImportError:
    websockets = None
    on = None
    cp = object
    call_result = None
    AuthorizationStatus = None
    DataTransferStatus = None
    RegistrationStatus = None
    ConnectionClosed = Exception
    OCPP_AVAILABLE = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
LOGGER = logging.getLogger("cpms")

DATA_DIR = Path("cpms_data")
DATA_DIR.mkdir(exist_ok=True)
EVENTS_FILE = DATA_DIR / "events.jsonl"
STATE_FILE = DATA_DIR / "state_snapshot.json"
BORNE_LOGS_DIR = DATA_DIR / "borne_logs"
BORNE_LOGS_DIR.mkdir(exist_ok=True)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json_file(file_path: Path, default):
    if not file_path.exists():
        return default

    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default

    return data if data is not None else default


def read_jsonl_file(file_path: Path) -> list[dict]:
    if not file_path.exists():
        return []

    items = []
    try:
        for line in file_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                items.append({"_raw": line, "_invalid_json": True})
    except OSError:
        return []

    return items


def read_borne_logs() -> dict[str, list[dict]]:
    logs = {}
    if not BORNE_LOGS_DIR.exists():
        return logs

    for log_file in sorted(BORNE_LOGS_DIR.glob("*.json")):
        logs[log_file.stem] = read_json_file(log_file, [])

    return logs


def build_cp_payload() -> dict:
    snapshot = read_json_file(STATE_FILE, {
        "generated_at": utc_now_iso(),
        "server_started_at": COLLECTOR.server_started_at,
        "metrics": {
            "total_connections": COLLECTOR.total_connections,
            "active_connections": len(COLLECTOR.active_connections),
            "total_messages": COLLECTOR.total_messages,
        },
        "message_count_by_action": dict(COLLECTOR.message_count_by_action),
        "message_count_by_cp": dict(COLLECTOR.message_count_by_cp),
        "last_seen": COLLECTOR.last_seen,
        "status_by_cp": COLLECTOR.status_by_cp,
        "last_meter_values_by_cp": COLLECTOR.last_meter_values_by_cp,
        "open_transactions_by_cp": COLLECTOR.open_transactions_by_cp,
    })

    payload = {
        "snapshot": snapshot,
        "events": read_jsonl_file(EVENTS_FILE),
        "borne_logs": read_borne_logs(),
        "files": {
            "events_jsonl": str(EVENTS_FILE),
            "state_snapshot": str(STATE_FILE),
            "borne_logs_dir": str(BORNE_LOGS_DIR),
        },
    }

    payload.update(snapshot)
    return payload


class DataCollector:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.server_started_at = utc_now_iso()
        self.total_messages = 0
        self.total_connections = 0
        self.active_connections = {}
        self.last_seen = {}
        self.message_count_by_action = defaultdict(int)
        self.message_count_by_cp = defaultdict(int)
        self.status_by_cp = {}
        self.last_meter_values_by_cp = {}
        self.open_transactions_by_cp = {}
        self._next_transaction_id = 1

    def _append_jsonl(self, file_path: Path, item: dict) -> None:
        with file_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(item, ensure_ascii=False) + "\n")

    def _safe_cp_id(self, cp_id: str) -> str:
        return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in cp_id)

    def _append_borne_json(self, cp_id: str, item: dict) -> None:
        safe_cp_id = self._safe_cp_id(cp_id)
        cp_file = BORNE_LOGS_DIR / f"{safe_cp_id}.json"

        if cp_file.exists():
            existing_data = json.loads(cp_file.read_text(encoding="utf-8"))
            if not isinstance(existing_data, list):
                existing_data = []
        else:
            existing_data = []

        existing_data.append(item)
        cp_file.write_text(json.dumps(existing_data, indent=2, ensure_ascii=False), encoding="utf-8")

    def _write_state(self) -> None:
        snapshot = {
            "generated_at": utc_now_iso(),
            "server_started_at": self.server_started_at,
            "metrics": {
                "total_connections": self.total_connections,
                "active_connections": len(self.active_connections),
                "total_messages": self.total_messages,
            },
            "message_count_by_action": dict(self.message_count_by_action),
            "message_count_by_cp": dict(self.message_count_by_cp),
            "last_seen": self.last_seen,
            "status_by_cp": self.status_by_cp,
            "last_meter_values_by_cp": self.last_meter_values_by_cp,
            "open_transactions_by_cp": self.open_transactions_by_cp,
        }
        STATE_FILE.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8")

    async def register_connection(self, cp_id: str, remote_address: str, path: str) -> None:
        event = {
            "event_type": "connection_opened",
            "timestamp": utc_now_iso(),
            "cp_id": cp_id,
            "remote_address": remote_address,
            "path": path,
        }
        async with self._lock:
            self.total_connections += 1
            self.active_connections[cp_id] = event["timestamp"]
            self.last_seen[cp_id] = event["timestamp"]
            self._append_jsonl(EVENTS_FILE, event)
            self._append_borne_json(cp_id, event)
            self._write_state()

    async def register_disconnection(self, cp_id: str, reason: str = "closed") -> None:
        event = {
            "event_type": "connection_closed",
            "timestamp": utc_now_iso(),
            "cp_id": cp_id,
            "reason": reason,
        }
        async with self._lock:
            self.active_connections.pop(cp_id, None)
            self.last_seen[cp_id] = event["timestamp"]
            self._append_jsonl(EVENTS_FILE, event)
            self._append_borne_json(cp_id, event)
            self._write_state()

    async def record_action(self, cp_id: str, action: str, payload: dict | None = None) -> None:
        event = {
            "event_type": "ocpp_message",
            "timestamp": utc_now_iso(),
            "cp_id": cp_id,
            "action": action,
            "payload": payload or {},
        }
        async with self._lock:
            self.total_messages += 1
            self.message_count_by_action[action] += 1
            self.message_count_by_cp[cp_id] += 1
            self.last_seen[cp_id] = event["timestamp"]
            self._append_jsonl(EVENTS_FILE, event)
            self._append_borne_json(cp_id, event)
            self._write_state()

    async def update_status(self, cp_id: str, status_payload: dict) -> None:
        async with self._lock:
            self.status_by_cp[cp_id] = {
                "timestamp": utc_now_iso(),
                **status_payload,
            }
            self._write_state()

    async def update_meter_values(self, cp_id: str, meter_payload: dict) -> None:
        async with self._lock:
            self.last_meter_values_by_cp[cp_id] = {
                "timestamp": utc_now_iso(),
                **meter_payload,
            }
            self._write_state()

    async def start_transaction(self, cp_id: str, transaction_payload: dict) -> int:
        async with self._lock:
            tx_id = self._next_transaction_id
            self._next_transaction_id += 1
            self.open_transactions_by_cp[cp_id] = {
                "transaction_id": tx_id,
                "opened_at": utc_now_iso(),
                **transaction_payload,
            }
            self._write_state()
            return tx_id

    async def stop_transaction(self, cp_id: str, stop_payload: dict) -> None:
        event = {
            "event_type": "transaction_closed",
            "timestamp": utc_now_iso(),
            "cp_id": cp_id,
            "payload": stop_payload,
        }
        async with self._lock:
            self.open_transactions_by_cp.pop(cp_id, None)
            self._append_jsonl(EVENTS_FILE, event)
            self._append_borne_json(cp_id, event)
            self._write_state()


COLLECTOR = DataCollector()
app = FastAPI()


@app.get("/api/cp")
def get_cp_state(cp_id: str | None = None):
    payload = build_cp_payload()

    if not cp_id:
        return JSONResponse(payload)

    filtered_events = [event for event in payload["events"] if event.get("cp_id") == cp_id]
    filtered_borne_logs = {cp_id: payload["borne_logs"].get(cp_id, [])}

    filtered_payload = dict(payload)
    filtered_payload["events"] = filtered_events
    filtered_payload["borne_logs"] = filtered_borne_logs
    filtered_payload["selected_cp_id"] = cp_id

    if "message_count_by_cp" in filtered_payload:
        filtered_payload["message_count_by_cp"] = {
            cp_id: filtered_payload["message_count_by_cp"].get(cp_id, 0)
        }

    if "last_seen" in filtered_payload:
        filtered_payload["last_seen"] = {
            cp_id: filtered_payload["last_seen"].get(cp_id)
        }

    if "status_by_cp" in filtered_payload:
        filtered_payload["status_by_cp"] = {
            cp_id: filtered_payload["status_by_cp"].get(cp_id)
        }

    if "last_meter_values_by_cp" in filtered_payload:
        filtered_payload["last_meter_values_by_cp"] = {
            cp_id: filtered_payload["last_meter_values_by_cp"].get(cp_id)
        }

    if "open_transactions_by_cp" in filtered_payload:
        filtered_payload["open_transactions_by_cp"] = {
            cp_id: filtered_payload["open_transactions_by_cp"].get(cp_id)
        }

    return JSONResponse(filtered_payload)


if OCPP_AVAILABLE:

    class ChargePoint(cp):
        @on("BootNotification")
        async def on_boot_notification(self, charge_point_model, charge_point_vendor, **kwargs):
            payload = {
                "charge_point_model": charge_point_model,
                "charge_point_vendor": charge_point_vendor,
                **kwargs,
            }
            await COLLECTOR.record_action(self.id, "BootNotification", payload)
            LOGGER.info("BootNotification from %s (%s)", self.id, charge_point_vendor)
            return call_result.BootNotification(
                current_time=utc_now_iso(),
                interval=10,
                status=RegistrationStatus.accepted,
            )

        @on("Heartbeat")
        async def on_heartbeat(self):
            await COLLECTOR.record_action(self.id, "Heartbeat", {})
            return call_result.Heartbeat(current_time=utc_now_iso())

        @on("Authorize")
        async def on_authorize(self, id_tag: str, **kwargs):
            await COLLECTOR.record_action(self.id, "Authorize", {"id_tag": id_tag, **kwargs})
            return call_result.Authorize(
                id_tag_info={"status": AuthorizationStatus.accepted}
            )

        @on("StatusNotification")
        async def on_status_notification(self, connector_id: int, status: str, error_code: str, **kwargs):
            payload = {
                "connector_id": connector_id,
                "status": status,
                "error_code": error_code,
                **kwargs,
            }
            await COLLECTOR.record_action(self.id, "StatusNotification", payload)
            await COLLECTOR.update_status(self.id, payload)
            return call_result.StatusNotification()

        @on("MeterValues")
        async def on_meter_values(self, connector_id: int, meter_value: list, **kwargs):
            payload = {
                "connector_id": connector_id,
                "meter_value": meter_value,
                **kwargs,
            }
            await COLLECTOR.record_action(self.id, "MeterValues", payload)
            await COLLECTOR.update_meter_values(self.id, payload)
            return call_result.MeterValues()

        @on("StartTransaction")
        async def on_start_transaction(
            self,
            connector_id: int,
            id_tag: str,
            meter_start: int,
            timestamp: str,
            **kwargs,
        ):
            payload = {
                "connector_id": connector_id,
                "id_tag": id_tag,
                "meter_start": meter_start,
                "timestamp": timestamp,
                **kwargs,
            }
            await COLLECTOR.record_action(self.id, "StartTransaction", payload)
            transaction_id = await COLLECTOR.start_transaction(self.id, payload)
            return call_result.StartTransaction(
                transaction_id=transaction_id,
                id_tag_info={"status": AuthorizationStatus.accepted},
            )

        @on("StopTransaction")
        async def on_stop_transaction(self, meter_stop: int, timestamp: str, transaction_id: int, **kwargs):
            payload = {
                "meter_stop": meter_stop,
                "timestamp": timestamp,
                "transaction_id": transaction_id,
                **kwargs,
            }
            await COLLECTOR.record_action(self.id, "StopTransaction", payload)
            await COLLECTOR.stop_transaction(self.id, payload)
            return call_result.StopTransaction()

        @on("DataTransfer")
        async def on_data_transfer(self, vendor_id: str, **kwargs):
            payload = {"vendor_id": vendor_id, **kwargs}
            await COLLECTOR.record_action(self.id, "DataTransfer", payload)
            return call_result.DataTransfer(
                status=DataTransferStatus.accepted,
                data="accepted",
            )

        @on("DiagnosticsStatusNotification")
        async def on_diagnostics_status_notification(self, status: str, **kwargs):
            await COLLECTOR.record_action(
                self.id,
                "DiagnosticsStatusNotification",
                {"status": status, **kwargs},
            )
            return call_result.DiagnosticsStatusNotification()

        @on("FirmwareStatusNotification")
        async def on_firmware_status_notification(self, status: str, **kwargs):
            await COLLECTOR.record_action(
                self.id,
                "FirmwareStatusNotification",
                {"status": status, **kwargs},
            )
            return call_result.FirmwareStatusNotification()


    class ASGIWebSocketAdapter:
        def __init__(self, websocket):
            self._ws = websocket
            # expose a similar attribute used elsewhere
            self.remote_address = websocket.client

        async def send(self, message: str) -> None:
            await self._ws.send_text(message)

        async def recv(self) -> str:
            # receive_text will raise WebSocketDisconnect when closed
            data = await self._ws.receive_text()
            return data

        async def close(self) -> None:
            await self._ws.close()


    from fastapi import WebSocket, WebSocketDisconnect


    @app.websocket("/{cp_id}")
    async def cpms_websocket(cp_id: str, websocket: WebSocket):
        await websocket.accept()
        remote = websocket.client
        path = websocket.url.path if hasattr(websocket, "url") else f"/{cp_id}"
        LOGGER.info("New charger connected: %s from %s", cp_id, remote)
        await COLLECTOR.register_connection(cp_id, str(remote), path)

        adapter = ASGIWebSocketAdapter(websocket)
        cp_instance = ChargePoint(cp_id, adapter)

        try:
            await cp_instance.start()
        except WebSocketDisconnect:
            LOGGER.info("WebSocket disconnect for %s", cp_id)
        except Exception as error:
            LOGGER.exception("Unexpected error for %s: %s", cp_id, error)
        finally:
            await COLLECTOR.register_disconnection(cp_id)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, log_level="info")