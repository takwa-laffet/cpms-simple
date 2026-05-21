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
    from ocpp.v16 import call  # pyright: ignore[reportMissingImports]
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
    call = None
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
META_FILE = DATA_DIR / "borne_meta.json"


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


def read_meta() -> dict:
    return read_json_file(META_FILE, {})


def write_meta(meta: dict) -> None:
    META_FILE.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")


def build_sessions(events: list[dict], borne_logs: dict[str, list[dict]]) -> dict[str, list[dict]]:
    # Build simple session summaries per cp_id using StartTransaction/StopTransaction events
    sessions_by_cp: dict[str, dict[int, dict]] = {}

    for ev in events:
        if ev.get("event_type") != "ocpp_message":
            continue
        cp_id = ev.get("cp_id")
        action = ev.get("action")
        payload = ev.get("payload") or {}

        if cp_id not in sessions_by_cp:
            sessions_by_cp[cp_id] = {}

        if action == "StartTransaction":
            tx = payload.get("transaction_id") or payload.get("transactionId") or None
            # If start transaction id is not present, use collector-assigned id in payload
            tx_id = tx if tx is not None else payload.get("transaction_id")
            if tx_id is None:
                # fallback: use internal open transactions if any
                tx_id = payload.get("transactionId")

            sessions_by_cp[cp_id][tx_id] = {
                "session_id": tx_id,
                "start": ev.get("timestamp"),
                "connector_id": payload.get("connector_id"),
                "id_tag": payload.get("id_tag"),
                "meter_start": payload.get("meter_start"),
                "events": [ev],
            }

        if action == "StopTransaction":
            tx_id = payload.get("transaction_id") or payload.get("transactionId")
            if tx_id is None:
                continue
            sess = sessions_by_cp.get(cp_id, {}).get(tx_id)
            if sess is None:
                # create partial session if missing
                sessions_by_cp.setdefault(cp_id, {})[tx_id] = {
                    "session_id": tx_id,
                    "start": None,
                    "end": ev.get("timestamp"),
                    "connector_id": payload.get("connector_id"),
                    "meter_stop": payload.get("meter_stop"),
                    "events": [ev],
                }
            else:
                sess["end"] = ev.get("timestamp")
                sess["meter_stop"] = payload.get("meter_stop")
                sess.setdefault("events", []).append(ev)

    # Attach meter_values from borne_logs where possible
    for cp_id, logs in borne_logs.items():
        for entry in logs:
            # look for meter values entries and attach to session by transaction_id
            if entry.get("event_type") == "ocpp_message" and entry.get("action") == "MeterValues":
                payload = entry.get("payload", {})
                tx = payload.get("transaction_id") or payload.get("transactionId")
                if tx is None:
                    # try to attach by time proximity: skip for now
                    continue
                sess = sessions_by_cp.get(cp_id, {}).get(tx)
                if sess:
                    sess.setdefault("meter_values", []).append(payload)

    # Convert to lists and compute basic metrics
    result: dict[str, list[dict]] = {}
    for cp_id, txs in sessions_by_cp.items():
        result[cp_id] = []
        for tx_id, s in txs.items():
            start = s.get("start")
            end = s.get("end")
            meter_start = s.get("meter_start")
            meter_stop = s.get("meter_stop")
            energy = None
            duration = None
            try:
                if meter_start is not None and meter_stop is not None:
                    energy = (meter_stop - meter_start) / 1000.0
                if start and end:
                    from datetime import datetime
                    fmt = None
                    try:
                        # ISO parse
                        start_dt = datetime.fromisoformat(start)
                        end_dt = datetime.fromisoformat(end)
                        duration = (end_dt - start_dt).total_seconds()
                    except Exception:
                        duration = None
            except Exception:
                energy = None

            item = {
                "session_id": tx_id,
                "start": start,
                "end": end,
                "duration_s": duration,
                "meter_start": meter_start,
                "meter_stop": meter_stop,
                "energy_kwh": energy,
                "connector_id": s.get("connector_id"),
                "id_tag": s.get("id_tag"),
                "meter_values": s.get("meter_values", []),
                "events": s.get("events", []),
            }
            result[cp_id].append(item)

    return result


def parse_meter_payload(payload: dict) -> list[dict]:
    """Extract readings from a MeterValues payload.

    Returns list of dict: {timestamp: iso, energy_wh: float|None, power_w: float|None, current_a: float|None, voltage_v: float|None}
    """
    readings = []
    # common OCPP MeterValues structure: payload contains 'meter_value' list
    meter_values = payload.get("meter_value") or payload.get("meterValues") or []
    for mv in meter_values:
        ts = mv.get("timestamp")
        sampled = mv.get("sampled_value") or mv.get("sampledValue") or []
        entry = {"timestamp": ts, "energy_wh": None, "power_w": None, "current_a": None, "voltage_v": None}
        for sv in sampled:
            meas = sv.get("measurand") or sv.get("measurand")
            val = sv.get("value")
            if val is None:
                continue
            try:
                num = float(val)
            except Exception:
                continue

            # detect energy cumulative
            if meas and "Energy" in meas:
                # assume value in Wh or kWh? many chargers report Wh in Wh or in kWh; try to guess
                # if value > 10000 assume Wh, else if < 1000 assume Wh too; best effort: if value > 1e6 treat as Wh
                entry["energy_wh"] = num if num > 1000 else num * 1000 if num < 1 else num * 1000
            elif meas and ("Power" in meas or meas.lower().startswith("power")):
                entry["power_w"] = num
            elif meas and ("Current" in meas or meas.lower().startswith("current")):
                entry["current_a"] = num
            elif meas and ("Voltage" in meas or meas.lower().startswith("voltage")):
                entry["voltage_v"] = num

        readings.append(entry)

    return readings


def aggregate_session_meter_values(session: dict, borne_log_entries: list[dict]) -> dict:
    """Aggregate meter readings for a single session into minute buckets and compute KPIs."""
    from datetime import datetime, timezone, timedelta

    start = session.get("start")
    end = session.get("end")
    if not start:
        return session

    try:
        start_dt = datetime.fromisoformat(start)
    except Exception:
        return session

    if end:
        try:
            end_dt = datetime.fromisoformat(end)
        except Exception:
            end_dt = None
    else:
        end_dt = None

    # collect readings matching this session: by transaction id or by timestamp range
    tx = session.get("session_id")
    readings = []
    for entry in borne_log_entries:
        if entry.get("event_type") != "ocpp_message":
            continue
        if entry.get("action") != "MeterValues":
            continue
        payload = entry.get("payload") or {}
        # check transaction id
        tx_id = payload.get("transaction_id") or payload.get("transactionId")
        entry_ts = payload.get("timestamp") or payload.get("ts")
        if tx is not None and tx_id is not None and str(tx_id) == str(tx):
            readings.extend(parse_meter_payload(payload))
        else:
            # if timestamp within session range, include
            if entry_ts:
                try:
                    entry_dt = datetime.fromisoformat(entry_ts)
                    if entry_dt >= start_dt and (end_dt is None or entry_dt <= end_dt):
                        readings.extend(parse_meter_payload(payload))
                except Exception:
                    continue

    if not readings:
        session["charge_curve"] = []
        session["kpis"] = {"total_kwh": session.get("energy_kwh"), "peak_kw": None, "avg_kw": None}
        return session

    # sort readings by timestamp
    def parse_ts(r):
        try:
            return datetime.fromisoformat(r.get("timestamp"))
        except Exception:
            return None

    readings = [r for r in readings if parse_ts(r) is not None]
    readings.sort(key=lambda r: parse_ts(r))

    # if energy_wh available, compute energy by diff; else integrate power
    energy_kwh = session.get("energy_kwh")
    peak_power_w = 0.0
    power_points = []  # list of (ts, power_w)

    prev_energy = None
    prev_ts = None
    for r in readings:
        ts_dt = parse_ts(r)
        pw = r.get("power_w")
        ew = r.get("energy_wh")
        if ew is not None and prev_energy is not None and prev_ts is not None:
            # compute power estimate
            dt = (ts_dt - prev_ts).total_seconds()
            if dt > 0:
                power_est = ((ew - prev_energy) / 1000.0) * 3600.0 / dt * 1000.0 / 1000.0
                # fallback: approximate instantaneous power if available
                # but keep power_est as (delta energy / time) in W
                power_points.append((ts_dt, power_est))
                if power_est > peak_power_w:
                    peak_power_w = power_est
        if pw is not None:
            power_points.append((ts_dt, pw))
            if pw > peak_power_w:
                peak_power_w = pw

        if ew is not None:
            prev_energy = ew
            prev_ts = ts_dt

    # if energy_kwh absent but cumulative energy present, compute from first/last
    if energy_kwh is None:
        first_ew = next((r.get("energy_wh") for r in readings if r.get("energy_wh") is not None), None)
        last_ew = next((r.get("energy_wh") for r in reversed(readings) if r.get("energy_wh") is not None), None)
        if first_ew is not None and last_ew is not None:
            try:
                energy_kwh = (last_ew - first_ew) / 1000.0
            except Exception:
                energy_kwh = None

    # build minute buckets
    if end_dt is None:
        end_dt = readings[-1] and parse_ts(readings[-1])
    if end_dt is None:
        session["charge_curve"] = []
        session["kpis"] = {"total_kwh": energy_kwh, "peak_kw": peak_power_w / 1000.0 if peak_power_w else None, "avg_kw": None}
        return session

    # create minute intervals from start_dt to end_dt
    start_min = start_dt.replace(second=0, microsecond=0)
    end_min = end_dt.replace(second=0, microsecond=0)
    minutes = int(((end_min - start_min).total_seconds() // 60) + 1)
    buckets = []
    for i in range(minutes):
        bucket_start = start_min + timedelta(minutes=i)
        bucket_end = bucket_start + timedelta(minutes=1)
        # collect power points in bucket
        p_vals = [p for (t, p) in power_points if t >= bucket_start and t < bucket_end]
        avg_power = sum(p_vals) / len(p_vals) if p_vals else None
        energy_kwh_min = (avg_power / 1000.0) * (1.0 / 60.0) if avg_power is not None else None
        buckets.append({"minute_start": bucket_start.isoformat(), "avg_power_w": avg_power, "energy_kwh": energy_kwh_min})

    # compute avg power across session
    power_vals = [b["avg_power_w"] for b in buckets if b["avg_power_w"] is not None]
    avg_power_w = sum(power_vals) / len(power_vals) if power_vals else None

    session["charge_curve"] = buckets
    session["kpis"] = {
        "total_kwh": energy_kwh,
        "peak_kw": (peak_power_w / 1000.0) if peak_power_w else None,
        "avg_kw": (avg_power_w / 1000.0) if avg_power_w else None,
    }

    return session


def aggregate_sessions_metrics(sessions: dict[str, list[dict]], borne_logs: dict[str, list[dict]]) -> dict[str, list[dict]]:
    # augment each session with charge_curve and KPIs
    for cp_id, sess_list in sessions.items():
        logs = borne_logs.get(cp_id, [])
        for i, s in enumerate(sess_list):
            sess_list[i] = aggregate_session_meter_values(s, logs)
    return sessions


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
        "boot_notifications_by_cp": COLLECTOR.boot_notifications_by_cp,
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

    # add metadata and sessions
    meta = read_meta()
    payload["metadata"] = meta

    # expose serial number and boot data at the top level for convenience
    payload["boot_notifications_by_cp"] = snapshot.get("boot_notifications_by_cp", {})
    payload["serial_number_by_cp"] = {}
    for cp_id, boot in payload["boot_notifications_by_cp"].items():
        serial = (
            boot.get("serial_number")
            or boot.get("charge_box_serial_number")
            or boot.get("charge_point_serial_number")
            or meta.get(cp_id, {}).get("serialNumber")
            or meta.get(cp_id, {}).get("serial_number")
        )
        payload["serial_number_by_cp"][cp_id] = serial

    payload["general_info_by_cp"] = {}
    for cp_id in set(list(meta.keys()) + list(payload["boot_notifications_by_cp"].keys())):
        boot = payload["boot_notifications_by_cp"].get(cp_id, {})
        m = meta.get(cp_id, {})
        payload["general_info_by_cp"][cp_id] = {
            "cp_id": cp_id,
            "manufacturer": m.get("manufacturer") or boot.get("charge_point_vendor") or boot.get("vendor_id"),
            "model": m.get("model") or boot.get("charge_point_model"),
            "serial_number": payload["serial_number_by_cp"].get(cp_id),
            "firmware_version": m.get("firmwareVersion") or m.get("firmware_version") or boot.get("firmware_version"),
            "ip_address": m.get("ipAddress") or m.get("ip_address"),
            "iccid": m.get("iccid"),
            "imsi": m.get("imsi"),
            "commissioning_date": m.get("commissioningDate") or m.get("commissioning_date"),
            "uptime": m.get("uptime"),
            "cpms_connection_status": "connected" if cp_id in COLLECTOR.active_connections else "disconnected",
            "reboot_logs": m.get("rebootLogs") or m.get("reboot_logs") or [],
            "boot_notification": boot,
            "metadata": m,
        }

    sessions = build_sessions(payload["events"], payload["borne_logs"])
    # enrich sessions with meter-values aggregation and KPIs
    sessions = aggregate_sessions_metrics(sessions, payload["borne_logs"])
    payload["sessions"] = sessions

    # basic energy aggregates
    total_kwh = 0.0
    kwh_per_cp = {}
    for cp_id, sess_list in sessions.items():
        cp_sum = 0.0
        for s in sess_list:
            if s.get("energy_kwh"):
                cp_sum += float(s["energy_kwh"])
        if cp_sum:
            kwh_per_cp[cp_id] = cp_sum
            total_kwh += cp_sum

    payload["energy"] = {
        "total_kwh": total_kwh,
        "kwh_per_cp": kwh_per_cp,
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
        self.charge_points = {}
        self.last_seen = {}
        self.message_count_by_action = defaultdict(int)
        self.message_count_by_cp = defaultdict(int)
        self.status_by_cp = {}
        self.last_meter_values_by_cp = {}
        self.boot_notifications_by_cp = {}
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
            "boot_notifications_by_cp": self.boot_notifications_by_cp,
            "open_transactions_by_cp": self.open_transactions_by_cp,
        }
        STATE_FILE.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8")

    async def register_charge_point(self, cp_id: str, charge_point) -> None:
        async with self._lock:
            self.charge_points[cp_id] = charge_point

    async def unregister_charge_point(self, cp_id: str) -> None:
        async with self._lock:
            self.charge_points.pop(cp_id, None)

    async def get_charge_point(self, cp_id: str):
        async with self._lock:
            return self.charge_points.get(cp_id)

    async def update_boot_notification(self, cp_id: str, boot_payload: dict) -> None:
        async with self._lock:
            self.boot_notifications_by_cp[cp_id] = {
                "timestamp": utc_now_iso(),
                **boot_payload,
            }
            self._write_state()

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


@app.post("/api/cp/{cp_id}/force_start")
async def force_start(cp_id: str, connector_id: int = 1, meter_start: int = 0):
    """Force-start a transaction server-side without requiring CP StartTransaction or id_tag."""
    tx_id = await COLLECTOR.start_transaction(cp_id, {"connector_id": connector_id, "meter_start": meter_start})
    await COLLECTOR.record_action(cp_id, "ForceStart", {"transaction_id": tx_id, "connector_id": connector_id, "meter_start": meter_start})
    return JSONResponse({"result": "started", "transaction_id": tx_id})


@app.post("/api/cp/{cp_id}/force_stop")
async def force_stop(cp_id: str, transaction_id: int | None = None, meter_stop: int = 0):
    """Force-stop a transaction server-side without requiring CP StopTransaction."""
    payload = {"meter_stop": meter_stop, "transaction_id": transaction_id}
    await COLLECTOR.record_action(cp_id, "ForceStop", payload)
    await COLLECTOR.stop_transaction(cp_id, payload)
    return JSONResponse({"result": "stopped", "transaction_id": transaction_id})


@app.post("/api/cp/{cp_id}/remote_start")
async def remote_start(cp_id: str, connector_id: int = 1, id_tag: str = "REMOTE_START"):
    """Send a real OCPP RemoteStartTransaction to the connected charger.

    The API caller does not need to provide an id_tag; a server-side default is used.
    """
    if not OCPP_AVAILABLE or call is None:
        return JSONResponse({"result": "error", "detail": "OCPP is not available on this server"}, status_code=503)

    cp_instance = await COLLECTOR.get_charge_point(cp_id)
    if cp_instance is None:
        return JSONResponse({"result": "error", "detail": f"Charge point {cp_id} is not connected"}, status_code=404)

    request = call.RemoteStartTransaction(id_tag=id_tag, connector_id=connector_id)
    try:
        response = await cp_instance.call(request)
        await COLLECTOR.record_action(cp_id, "RemoteStartTransaction", {"connector_id": connector_id, "id_tag": id_tag, "response_status": getattr(response, "status", None)})
        return JSONResponse({"result": "sent", "response_status": getattr(response, "status", None), "connector_id": connector_id, "id_tag_used": id_tag})
    except Exception as error:
        LOGGER.exception("RemoteStartTransaction failed for %s: %s", cp_id, error)
        return JSONResponse({"result": "error", "detail": str(error)}, status_code=500)


@app.post("/api/cp/{cp_id}/remote_stop")
async def remote_stop(cp_id: str, transaction_id: int):
    """Send a real OCPP RemoteStopTransaction to the connected charger."""
    if not OCPP_AVAILABLE or call is None:
        return JSONResponse({"result": "error", "detail": "OCPP is not available on this server"}, status_code=503)

    cp_instance = await COLLECTOR.get_charge_point(cp_id)
    if cp_instance is None:
        return JSONResponse({"result": "error", "detail": f"Charge point {cp_id} is not connected"}, status_code=404)

    request = call.RemoteStopTransaction(transaction_id=transaction_id)
    try:
        response = await cp_instance.call(request)
        await COLLECTOR.record_action(cp_id, "RemoteStopTransaction", {"transaction_id": transaction_id, "response_status": getattr(response, "status", None)})
        return JSONResponse({"result": "sent", "response_status": getattr(response, "status", None), "transaction_id": transaction_id})
    except Exception as error:
        LOGGER.exception("RemoteStopTransaction failed for %s: %s", cp_id, error)
        return JSONResponse({"result": "error", "detail": str(error)}, status_code=500)


@app.get("/api/cp/{cp_id}/meta")
def get_meta(cp_id: str):
    meta = read_meta()
    return JSONResponse(meta.get(cp_id, {}))


@app.post("/api/cp/{cp_id}/meta")
def set_meta(cp_id: str, meta: dict):
    store = read_meta()
    store[cp_id] = {**store.get(cp_id, {}), **meta}
    write_meta(store)
    return JSONResponse({"result": "ok", "meta": store[cp_id]})


if OCPP_AVAILABLE:

    class ChargePoint(cp):
        @on("BootNotification")
        async def on_boot_notification(self, charge_point_model, charge_point_vendor, **kwargs):
            payload = {
                "charge_point_model": charge_point_model,
                "charge_point_vendor": charge_point_vendor,
                **kwargs,
            }
            await COLLECTOR.update_boot_notification(self.id, payload)
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
        await COLLECTOR.register_charge_point(cp_id, cp_instance)

        try:
            await cp_instance.start()
        except WebSocketDisconnect:
            LOGGER.info("WebSocket disconnect for %s", cp_id)
        except Exception as error:
            LOGGER.exception("Unexpected error for %s: %s", cp_id, error)
        finally:
            await COLLECTOR.unregister_charge_point(cp_id)
            await COLLECTOR.register_disconnection(cp_id)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, log_level="info")