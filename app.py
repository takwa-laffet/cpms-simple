import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import socket
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
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
        ResetType,
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
USERS_FILE = DATA_DIR / "users.json"
CHARGE_POINTS_FILE = DATA_DIR / "charge_points.json"
STATIONS_FILE = DATA_DIR / "stations.json"
FRONTEND_DIR = Path("frontend")
FRONTEND_INDEX_FILE = FRONTEND_DIR / "index.html"
FRONTEND_DASHBOARD_FILE = FRONTEND_DIR / "dashboard.html"
AUTH_COOKIE_NAME = "cpms_auth"
AUTH_EMAIL = os.environ.get("CPMS_LOGIN_EMAIL", "mvp@prelabel.tn")
AUTH_PASSWORD = os.environ.get("CPMS_LOGIN_PASSWORD", "Cpms_Secure#48Tz@2026")
AUTH_SECRET_KEY = os.environ.get("AUTH_SECRET_KEY", "cityos-dev-secret")
AUTH_SESSION_TTL_SECONDS = int(os.environ.get("AUTH_SESSION_TTL_SECONDS", "86400"))
BILLING_TARIFF_PER_KWH = float(os.environ.get("CITYOS_TARIFF_PER_KWH", "0.35"))
SUPERVISION_ROLES = {"admin", "institution"}
FULL_MANAGEMENT_ROLES = {"admin"}
COMMAND_ROLES = {"admin", "operator"}
USER_MANAGEMENT_ROLES = {"admin"}


class LoginPayload(BaseModel):
    email: str
    password: str


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def current_unix_timestamp() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def is_port_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def resolve_server_port(preferred_port: int, host: str = "0.0.0.0") -> int:
    if is_port_available(host, preferred_port):
        return preferred_port

    for candidate_port in range(preferred_port + 1, preferred_port + 101):
        if is_port_available(host, candidate_port):
            LOGGER.warning(
                "Port %s is busy; falling back to %s. Set PORT or APP_PORT to choose a fixed port.",
                preferred_port,
                candidate_port,
            )
            return candidate_port

    raise OSError(f"No free port found near {preferred_port}")


def env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _auth_signature(payload: str) -> str:
    digest = hmac.new(AUTH_SECRET_KEY.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def build_auth_cookie_value(email: str) -> str:
    issued_at = str(current_unix_timestamp())
    payload = f"{email}:{issued_at}"
    return f"{payload}:{_auth_signature(payload)}"


def verify_auth_cookie_value(cookie_value: str | None) -> str | None:
    if not cookie_value:
        return None

    try:
        email, issued_at_text, signature = cookie_value.rsplit(":", 2)
    except ValueError:
        return None

    payload = f"{email}:{issued_at_text}"
    expected_signature = _auth_signature(payload)
    if not hmac.compare_digest(signature, expected_signature):
        return None

    try:
        issued_at = int(issued_at_text)
    except ValueError:
        return None

    if current_unix_timestamp() - issued_at > AUTH_SESSION_TTL_SECONDS:
        return None

    user = get_user_by_email(email)
    if user is None or not user.get("active", True):
        return None

    return user.get("email")


def get_authenticated_email(request: Request) -> str | None:
    return verify_auth_cookie_value(request.cookies.get(AUTH_COOKIE_NAME))


def require_authenticated_email(request: Request) -> str:
    email = get_authenticated_email(request)
    if email is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    return email


def build_auth_response(email: str, wants_json: bool, redirect_url: str = "/cityos"):
    if wants_json:
        response = JSONResponse({"status": "ok", "email": email})
    else:
        response = RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)

    response.set_cookie(
        AUTH_COOKIE_NAME,
        build_auth_cookie_value(email),
        httponly=True,
        samesite="lax",
        secure=False,
        max_age=AUTH_SESSION_TTL_SECONDS,
        path="/",
    )
    return response


def hash_password(password: str) -> str:
    return hashlib.sha256(f"{AUTH_SECRET_KEY}:{password}".encode("utf-8")).hexdigest()


def ensure_seed_users() -> list[dict]:
    users = read_json_file(USERS_FILE, [])
    if not isinstance(users, list):
        users = []

    if not any(isinstance(user, dict) and user.get("email") == AUTH_EMAIL for user in users):
        users.append(
            {
                "id": "admin-1",
                "email": AUTH_EMAIL,
                "password_hash": hash_password(AUTH_PASSWORD),
                "role": "admin",
                "name": "CityOs Admin",
                "active": True,
                "created_at": utc_now_iso(),
            }
        )
        write_json = json.dumps(users, indent=2, ensure_ascii=False)
        USERS_FILE.write_text(write_json, encoding="utf-8")

    return users


def read_users() -> list[dict]:
    return ensure_seed_users()


def write_users(users: list[dict]) -> None:
    USERS_FILE.write_text(json.dumps(users, indent=2, ensure_ascii=False), encoding="utf-8")


def read_stations() -> list[dict]:
    data = read_json_file(STATIONS_FILE, [])
    return data if isinstance(data, list) else []


def write_stations(stations: list[dict]) -> None:
    STATIONS_FILE.write_text(json.dumps(stations, indent=2, ensure_ascii=False), encoding="utf-8")


def get_station_by_id(station_id: str) -> dict | None:
    sid = str(station_id).strip()
    for s in read_stations():
        if str(s.get("id") or s.get("station_id") or "").strip() == sid:
            return s
    return None


def upsert_station(record: dict) -> dict:
    stations = read_stations()
    station_id = str(record.get("id") or record.get("station_id") or record.get("name") or "").strip()
    if not station_id:
        raise ValueError("station id is required")

    now = utc_now_iso()
    normalized = {
        "id": station_id,
        "name": record.get("name") or station_id,
        "zone": record.get("zone"),
        "location": record.get("location"),
        "notes": record.get("notes"),
        "created_at": record.get("created_at") or now,
        "updated_at": now,
    }

    updated = False
    for i, existing in enumerate(stations):
        if str(existing.get("id") or "").strip() == station_id:
            stations[i] = {**existing, **normalized, "created_at": existing.get("created_at") or normalized["created_at"]}
            updated = True
            break

    if not updated:
        stations.append(normalized)

    write_stations(stations)
    return normalized


def public_user(user: dict) -> dict:
    return {
        "id": user.get("id"),
        "email": user.get("email"),
        "role": user.get("role", "operator"),
        "name": user.get("name") or user.get("email"),
        "active": bool(user.get("active", True)),
        "created_at": user.get("created_at"),
    }


def get_user_by_email(email: str) -> dict | None:
    normalized_email = email.strip().lower()
    for user in read_users():
        if str(user.get("email", "")).strip().lower() == normalized_email:
            return user
    return None


def authenticate_user(email: str, password: str) -> dict | None:
    user = get_user_by_email(email)
    if user is None or not user.get("active", True):
        return None

    if not hmac.compare_digest(str(user.get("password_hash", "")), hash_password(password)):
        return None

    return user


def get_authenticated_user(request: Request) -> dict | None:
    email = get_authenticated_email(request)
    if email is None:
        return None

    user = get_user_by_email(email)
    if user is None or not user.get("active", True):
        return None

    return user


def require_authenticated_user(request: Request) -> dict:
    user = get_authenticated_user(request)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    return user


def require_role(request: Request, allowed_roles: set[str]) -> dict:
    user = require_authenticated_user(request)
    if user.get("role") not in allowed_roles:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")

    return user


def read_charge_point_registry() -> list[dict]:
    registry = read_json_file(CHARGE_POINTS_FILE, [])
    return registry if isinstance(registry, list) else []


def write_charge_point_registry(registry: list[dict]) -> None:
    CHARGE_POINTS_FILE.write_text(json.dumps(registry, indent=2, ensure_ascii=False), encoding="utf-8")


def get_charge_point_record(cp_id: str) -> dict | None:
    normalized_cp_id = str(cp_id).strip().lower()
    for record in read_charge_point_registry():
        if str(record.get("cp_id", "")).strip().lower() == normalized_cp_id:
            return record
    return None


def upsert_charge_point_record(record: dict) -> dict:
    registry = read_charge_point_registry()
    cp_id = str(record.get("cp_id") or record.get("id") or "").strip()
    if not cp_id:
        raise ValueError("cp_id is required")

    timestamp = utc_now_iso()
    normalized = {
        "cp_id": cp_id,
        "label": record.get("label") or cp_id,
        "site": record.get("site"),
        "station_id": record.get("station_id"),
        "status": record.get("status") or record.get("state") or "offline",
        "connector_count": int(record.get("connector_count") or 1),
        "tariff_per_kwh": float(record.get("tariff_per_kwh") or BILLING_TARIFF_PER_KWH),
        "notes": record.get("notes"),
        "created_at": record.get("created_at") or timestamp,
        "updated_at": timestamp,
    }

    updated = False
    for index, existing in enumerate(registry):
        if str(existing.get("cp_id")).strip().lower() == cp_id.lower():
            registry[index] = {**existing, **normalized, "created_at": existing.get("created_at") or normalized["created_at"]}
            updated = True
            break

    if not updated:
        registry.append(normalized)

    write_charge_point_registry(registry)
    return normalized


def update_charge_point_tariff(cp_id: str, tariff_per_kwh: float) -> dict:
    registry = read_charge_point_registry()
    normalized_cp_id = str(cp_id).strip().lower()
    timestamp = utc_now_iso()

    for index, existing in enumerate(registry):
        if str(existing.get("cp_id", "")).strip().lower() == normalized_cp_id:
            updated = {
                **existing,
                "tariff_per_kwh": float(tariff_per_kwh),
                "updated_at": timestamp,
            }
            registry[index] = updated
            write_charge_point_registry(registry)
            return updated

    updated = {
        "cp_id": cp_id,
        "label": cp_id,
        "site": None,
        "connector_count": 1,
        "tariff_per_kwh": float(tariff_per_kwh),
        "notes": None,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    registry.append(updated)
    write_charge_point_registry(registry)
    return updated


def build_charge_point_view(cp_id: str, snapshot: dict, general_info: dict, registry_record: dict | None) -> dict:
    status_payload = snapshot.get("status_by_cp", {}).get(cp_id) or {}
    ocpp_status = status_payload.get("status")
    # Consider a CP 'connected' either when there's an active WS connection
    # or when recent status updates indicate availability (useful for simulators
    # that may not open a real WS but still report status).
    available_states = {"available", "charging", "preparing", "reserved"}
    is_connected = cp_id in COLLECTOR.active_connections or (
        isinstance(ocpp_status, str) and ocpp_status.strip().lower() in available_states
    )
    if is_connected:
        visible_status = ocpp_status or "Available"
    else:
        visible_status = "Offline"

    current_tx = snapshot.get("open_transactions_by_cp", {}).get(cp_id) or {}
    last_meter_payload = snapshot.get("last_meter_values_by_cp", {}).get(cp_id) or {}
    tariff = float((registry_record or {}).get("tariff_per_kwh") or BILLING_TARIFF_PER_KWH)
    energy_kwh = 0.0
    for session in (snapshot.get("sessions", {}) or {}).get(cp_id, []):
        if session.get("energy_kwh"):
            energy_kwh += float(session["energy_kwh"])

    return {
        "cp_id": cp_id,
        "label": (registry_record or {}).get("label") or general_info.get("model") or cp_id,
        "site": (registry_record or {}).get("site"),
        "status": visible_status,
        "connection_state": "connected" if is_connected else "offline",
        "model": general_info.get("model"),
        "serial_number": general_info.get("serial_number"),
        "assigned_user": (registry_record or {}).get("assigned_user") or general_info.get("assigned_user"),
        "error_code": status_payload.get("error_code") or status_payload.get("errorCode"),
        "last_meter_value": last_meter_payload.get("last_meter_value") or last_meter_payload.get("energy_wh"),
        "connector_count": (registry_record or {}).get("connector_count") or 1,
        "tariff_per_kwh": tariff,
        "last_seen": snapshot.get("last_seen", {}).get(cp_id),
        "active_transaction": current_tx.get("transaction_id"),
        "estimated_revenue": round(energy_kwh * tariff, 2),
        "energy_kwh": round(energy_kwh, 3),
        "notes": (registry_record or {}).get("notes"),
    }


def parse_iso_datetime(value: str | None):
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(value)
    except Exception:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed


def normalize_transaction_id(value):
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, int):
        return value

    if isinstance(value, float) and value.is_integer():
        return int(value)

    text_value = str(value).strip()
    if not text_value:
        return None

    try:
        return int(text_value)
    except ValueError:
        return text_value


def normalize_energy_wh(value: float | None, unit: str | None = None) -> float | None:
    if value is None:
        return None

    normalized_unit = (unit or "").strip().lower()
    if normalized_unit in {"kwh", "kilowatt hour", "kilowatt-hours", "kilowatthour"}:
        return float(value) * 1000.0

    if normalized_unit in {"wh", "watt hour", "watt-hours", "watthour"}:
        return float(value)

    # Best-effort fallback for chargers that omit the unit field.
    if value >= 1000:
        return float(value)

    if not float(value).is_integer():
        return float(value) * 1000.0

    return None


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


def normalize_boot_payload(payload: dict) -> dict:
    """Normalize BootNotification payload fields across OCPP naming variants."""
    normalized = dict(payload or {})
    aliases = {
        "charge_point_vendor": ["charge_point_vendor", "chargePointVendor", "vendor", "manufacturer"],
        "charge_point_model": ["charge_point_model", "chargePointModel", "model"],
        "serial_number": [
            "serial_number",
            "serialNumber",
            "charge_point_serial_number",
            "chargePointSerialNumber",
            "charge_box_serial_number",
            "chargeBoxSerialNumber",
            "charge_box_serialNumber",
        ],
        "firmware_version": ["firmware_version", "firmwareVersion", "firmware"],
        "imsi": ["imsi", "IMSI"],
        "meter_type": ["meter_type", "meterType"],
        "meter_serial_number": ["meter_serial_number", "meterSerialNumber"],
    }

    for target, keys in aliases.items():
        if normalized.get(target) is not None:
            continue
        for key in keys:
            if normalized.get(key) is not None:
                normalized[target] = normalized.get(key)
                break

    return normalized


def build_sessions(events: list[dict], borne_logs: dict[str, list[dict]]) -> dict[str, list[dict]]:
    # Build simple session summaries per cp_id using StartTransaction/StopTransaction events.
    sessions_by_cp: dict[str, dict[object, dict]] = {}

    for ev in events:
        if ev.get("event_type") != "ocpp_message":
            continue
        cp_id = ev.get("cp_id")
        action = ev.get("action")
        payload = ev.get("payload") or {}

        cp_sessions = sessions_by_cp.setdefault(cp_id, {})

        if action == "StartTransaction":
            tx_id = normalize_transaction_id(payload.get("transaction_id") or payload.get("transactionId"))
            if tx_id is None:
                tx_id = f"{cp_id}:{ev.get('timestamp')}:{payload.get('connector_id') or 'unknown'}"

            cp_sessions[tx_id] = {
                "session_id": tx_id,
                "start": ev.get("timestamp"),
                "connector_id": payload.get("connector_id"),
                "id_tag": payload.get("id_tag"),
                "meter_start": payload.get("meter_start"),
                "events": [ev],
            }

        if action == "StopTransaction":
            tx_id = normalize_transaction_id(payload.get("transaction_id") or payload.get("transactionId"))
            sess = cp_sessions.get(tx_id) if tx_id is not None else None

            if sess is None and tx_id is None:
                connector_id = payload.get("connector_id")
                for existing_tx_id, candidate in reversed(list(cp_sessions.items())):
                    if candidate.get("end") is not None:
                        continue
                    if connector_id is None or candidate.get("connector_id") == connector_id:
                        sess = candidate
                        tx_id = existing_tx_id
                        break

            if sess is None:
                if tx_id is None:
                    tx_id = f"{cp_id}:stop:{ev.get('timestamp')}:{payload.get('connector_id') or 'unknown'}"

                cp_sessions[tx_id] = {
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
                tx = normalize_transaction_id(payload.get("transaction_id") or payload.get("transactionId"))
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
        ordered_sessions = sorted(
            txs.values(),
            key=lambda session: parse_iso_datetime(session.get("start")) or parse_iso_datetime(session.get("end")) or datetime.min.replace(tzinfo=timezone.utc),
        )
        for s in ordered_sessions:
            start = s.get("start")
            end = s.get("end")
            meter_start = s.get("meter_start")
            meter_stop = s.get("meter_stop")
            energy = None
            duration = None
            try:
                if meter_start is not None and meter_stop is not None:
                    energy = (float(meter_stop) - float(meter_start)) / 1000.0
                if start and end:
                    start_dt = parse_iso_datetime(start)
                    end_dt = parse_iso_datetime(end)
                    if start_dt is not None and end_dt is not None:
                        duration = (end_dt - start_dt).total_seconds()
            except Exception:
                energy = None

            item = {
                "session_id": s.get("session_id"),
                "start": start,
                "end": end,
                "duration_s": duration,
                "meter_start": meter_start,
                "meter_stop": meter_stop,
                "energy_kwh": energy,
                "connector_id": s.get("connector_id"),
                "id_tag": s.get("id_tag"),
                "user": s.get("id_tag") or s.get("user"),
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
        entry = {
            "timestamp": ts,
            "energy_wh": None,
            "energy_value": None,
            "energy_unit": None,
            "power_w": None,
            "current_a": None,
            "voltage_v": None,
        }
        for sv in sampled:
            meas = sv.get("measurand") or sv.get("measurand")
            unit = sv.get("unit") or sv.get("Unit")
            val = sv.get("value")
            if val is None:
                continue
            try:
                num = float(val)
            except Exception:
                continue

            # detect energy cumulative
            if meas and "Energy" in meas:
                entry["energy_value"] = num
                entry["energy_unit"] = unit
                entry["energy_wh"] = normalize_energy_wh(num, unit)
            elif meas and ("Power" in meas or meas.lower().startswith("power")):
                entry["power_w"] = num
            elif meas and ("Current" in meas or meas.lower().startswith("current")):
                entry["current_a"] = num
            elif meas and ("Voltage" in meas or meas.lower().startswith("voltage")):
                entry["voltage_v"] = num

        readings.append(entry)

    return readings


def aggregate_session_meter_values(session: dict, borne_log_entries: list[dict], tariff_per_kwh: float | None = None) -> dict:
    """Aggregate meter readings for a single session into minute buckets and compute KPIs."""
    from datetime import timedelta

    start = session.get("start")
    end = session.get("end")
    if not start:
        return session

    start_dt = parse_iso_datetime(start)
    if start_dt is None:
        return session

    end_dt = parse_iso_datetime(end) if end else None

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
        tx_id = normalize_transaction_id(payload.get("transaction_id") or payload.get("transactionId"))
        entry_ts = payload.get("timestamp") or payload.get("ts")
        if tx is not None and tx_id is not None and str(tx_id) == str(tx):
            readings.extend(parse_meter_payload(payload))
        else:
            # if timestamp within session range, include
            if entry_ts:
                entry_dt = parse_iso_datetime(entry_ts)
                if entry_dt is not None and entry_dt >= start_dt and (end_dt is None or entry_dt <= end_dt):
                    readings.extend(parse_meter_payload(payload))

    if not readings:
        session["charge_curve"] = []
        total_kwh = session.get("energy_kwh")
        session["kpis"] = {"total_kwh": total_kwh, "peak_kw": None, "avg_kw": None}
        effective_tariff = tariff_per_kwh or BILLING_TARIFF_PER_KWH
        if total_kwh is not None:
            session["price"] = round(float(total_kwh) * effective_tariff, 2)
            session["currency"] = "dt"
        return session

    # sort readings by timestamp
    def parse_ts(r):
        return parse_iso_datetime(r.get("timestamp"))

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
        end_dt = parse_ts(readings[-1])
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
    effective_tariff = tariff_per_kwh or BILLING_TARIFF_PER_KWH
    if energy_kwh is not None:
        session["price"] = round(float(energy_kwh) * effective_tariff, 2)
        session["currency"] = "dt"

    return session


def aggregate_sessions_metrics(sessions: dict[str, list[dict]], borne_logs: dict[str, list[dict]], tariff_by_cp: dict[str, float] | None = None) -> dict[str, list[dict]]:
    # augment each session with charge_curve and KPIs
    for cp_id, sess_list in sessions.items():
        logs = borne_logs.get(cp_id, [])
        cp_tariff = None
        if tariff_by_cp and cp_id in tariff_by_cp:
            cp_tariff = tariff_by_cp[cp_id]
        for i, s in enumerate(sess_list):
            sess_list[i] = aggregate_session_meter_values(s, logs, cp_tariff)
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
        normalized_boot = normalize_boot_payload(boot)
        serial = (
            normalized_boot.get("serial_number")
            or normalized_boot.get("charge_box_serial_number")
            or normalized_boot.get("charge_point_serial_number")
            or meta.get(cp_id, {}).get("serialNumber")
            or meta.get(cp_id, {}).get("serial_number")
        )
        payload["serial_number_by_cp"][cp_id] = serial

    payload["general_info_by_cp"] = {}
    for cp_id in set(list(meta.keys()) + list(payload["boot_notifications_by_cp"].keys())):
        boot = normalize_boot_payload(payload["boot_notifications_by_cp"].get(cp_id, {}))
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
            "assigned_user": m.get("assigned_user"),
            "uptime": m.get("uptime"),
            "cpms_connection_status": "connected" if cp_id in COLLECTOR.active_connections else "disconnected",
            "reboot_logs": m.get("rebootLogs") or m.get("reboot_logs") or [],
            "boot_notification": boot,
            "metadata": m,
        }

    registry = {str(record.get("cp_id")): record for record in read_charge_point_registry() if record.get("cp_id")}
    charge_points = []
    all_cp_ids = set(list(payload["general_info_by_cp"].keys()) + list(registry.keys()) + list(snapshot.get("last_seen", {}).keys()))
    for cp_id in sorted(all_cp_ids):
        charge_points.append(
            build_charge_point_view(
                cp_id,
                snapshot,
                payload["general_info_by_cp"].get(cp_id, {}),
                registry.get(cp_id),
            )
        )
    payload["charge_points"] = charge_points
    payload["registered_charge_points"] = list(registry.values())
    payload["users"] = [public_user(user) for user in read_users()]

    sessions = build_sessions(payload["events"], payload["borne_logs"])
    # enrich sessions with meter-values aggregation and KPIs
    # build tariff lookup by cp_id from registry
    tariff_by_cp = {cp_id: rec.get("tariff_per_kwh") for cp_id, rec in registry.items() if rec.get("tariff_per_kwh")}
    sessions = aggregate_sessions_metrics(sessions, payload["borne_logs"], tariff_by_cp)
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

    billing = {
        "tariff_per_kwh": BILLING_TARIFF_PER_KWH,
        "currency": "dt",
        "total_energy_kwh": total_kwh,
        "estimated_revenue": round(total_kwh * BILLING_TARIFF_PER_KWH, 2),
        "revenue_per_cp": {cp_id: round(kwh * tariff_by_cp.get(cp_id, BILLING_TARIFF_PER_KWH), 2) for cp_id, kwh in kwh_per_cp.items()},
    }
    payload["billing"] = billing

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

    async def upsert_charge_point_record(self, record: dict) -> dict:
        async with self._lock:
            return upsert_charge_point_record(record)

    async def update_boot_notification(self, cp_id: str, boot_payload: dict) -> None:
        async with self._lock:
            normalized = normalize_boot_payload(boot_payload)
            self.boot_notifications_by_cp[cp_id] = {
                "timestamp": utc_now_iso(),
                **normalized,
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
            if action == "MetadataUpdate" and isinstance(event["payload"], dict):
                metadata_payload = event["payload"].get("metadata")
                if isinstance(metadata_payload, dict):
                    metadata_cp_id = str(event["payload"].get("cp_id") or cp_id).strip() or cp_id
                    update_metadata_store(metadata_cp_id, metadata_payload)
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

    async def get_open_transaction(self, cp_id: str) -> dict | None:
        async with self._lock:
            return self.open_transactions_by_cp.get(cp_id)

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


# Auto-start OCPP simulation for CP_TEST_001 on server startup
@app.on_event("startup")
async def startup_event():
    """Optionally start OCPP simulation for CP_TEST_001 when server starts."""
    try:
        if not env_flag("AUTO_START_CP_TEST_001", default=False):
            LOGGER.info("Auto-start simulator disabled; set AUTO_START_CP_TEST_001=1 to enable it")
            return

        ws_url = os.environ.get("AUTO_START_CP_TEST_001_WS_URL", "ws://localhost:5000")
        from auto_simulator import start_auto_simulator

        await start_auto_simulator("CP_TEST_001", ws_url, COLLECTOR)
        LOGGER.info("Auto-started OCPP simulation for CP_TEST_001")
    except Exception as e:
        LOGGER.error(f"Failed to auto-start OCPP simulation: {e}")
cors_origins = [origin.strip() for origin in os.environ.get("CORS_ORIGINS", "*").split(",") if origin.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins if cors_origins != ["*"] else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def protect_dashboard_api(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/cp") and not path.startswith("/api/auth"):
        if get_authenticated_email(request) is None:
            return JSONResponse({"detail": "Not authenticated"}, status_code=status.HTTP_401_UNAUTHORIZED)

    return await call_next(request)

if FRONTEND_DIR.exists():
    app.mount("/frontend", StaticFiles(directory=FRONTEND_DIR), name="frontend")


@app.post("/api/auth/login")
async def login(request: Request):
    content_type = request.headers.get("content-type", "")
    accept_header = request.headers.get("accept", "")
    wants_json = "application/json" in content_type or "application/json" in accept_header

    if "application/json" in content_type:
        data = await request.json()
        payload = LoginPayload(**data)
    else:
        raw_body = (await request.body()).decode("utf-8")
        form_data = parse_qs(raw_body, keep_blank_values=True)
        payload = LoginPayload(
            email=str((form_data.get("email") or [""])[0]),
            password=str((form_data.get("password") or [""])[0]),
        )

    email = payload.email.strip()
    password = payload.password
    redirect_url = request.query_params.get("next") or "/cityos"

    user = authenticate_user(email, password)
    if user is None:
        if wants_json:
            return JSONResponse({"detail": "Invalid email or password"}, status_code=status.HTTP_401_UNAUTHORIZED)

        return RedirectResponse(url="/login?login=failed", status_code=status.HTTP_303_SEE_OTHER)

    return build_auth_response(user["email"], wants_json, redirect_url=redirect_url)


@app.post("/api/auth/logout")
def logout():
    response = JSONResponse({"status": "ok"})
    response.delete_cookie(AUTH_COOKIE_NAME, path="/")
    return response


# ========== OCPP SIMULATOR ENDPOINTS (No Auth Required) ==========

@app.post("/api/ocpp/simulate")
async def start_ocpp_simulation(request: Request):
    """Start OCPP simulation without authentication - uses real-time auto simulator"""
    try:
        data = await request.json()
        cp_id = data.get("cp_id")
        ws_url = data.get("ws_url", "ws://localhost:5000")
        
        if not cp_id:
            raise HTTPException(status_code=400, detail="cp_id is required")
        
        # Import and start the auto simulator
        from auto_simulator import start_auto_simulator, simulation_state as auto_sim_state
        
        result = await start_auto_simulator(cp_id, ws_url, COLLECTOR)
        
        if not result and cp_id in auto_sim_state:
            return JSONResponse({
                "result": "simulation_already_running",
                "cp_id": cp_id,
                "message": "Simulation already running for this CP"
            })
        
        # Log the simulation start
        await COLLECTOR.record_action("SYSTEM", "SimulationStart", {
            "cp_id": cp_id,
            "ws_url": ws_url
        })
        
        return JSONResponse({
            "result": "simulation_started",
            "cp_id": cp_id,
            "ws_url": ws_url,
            "message": "OCPP simulation started. Logs are being written to cpms_data/borne_logs/"
        })
    except Exception as e:
        LOGGER.error(f"Error starting OCPP simulation: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/ocpp/simulate/{cp_id}/stop")
async def stop_ocpp_simulation(cp_id: str):
    """Stop OCPP simulation for a specific charge point"""
    from auto_simulator import stop_auto_simulator
    
    await stop_auto_simulator(cp_id)
    await COLLECTOR.record_action("SYSTEM", "SimulationStop", {
        "cp_id": cp_id,
        "stop_time": utc_now_iso()
    })
    return JSONResponse({"result": "simulation_stopped", "cp_id": cp_id})


@app.get("/api/ocpp/simulate/{cp_id}/messages")
async def get_simulation_messages(cp_id: str):
    """Get OCPP simulation messages for a charge point"""
    from auto_simulator import simulation_state as sim_state
    if cp_id in sim_state:
        return JSONResponse({
            "cp_id": cp_id,
            "messages": sim_state[cp_id].get("messages", []),
            "active": sim_state[cp_id].get("running", False),
            "start_time": sim_state[cp_id].get("start_time", ""),
            "ws_url": sim_state[cp_id].get("ws_url", "")
        })
    else:
        raise HTTPException(status_code=404, detail=f"No simulation found for CP ID: {cp_id}")

@app.post("/api/ocpp/simulate/message")
async def send_ocpp_simulation_message(request: Request):
    """Send a custom OCPP message in simulation"""
    try:
        data = await request.json()
        cp_id = data.get("cp_id")
        message_type = data.get("type")
        payload = data.get("payload", {})
        direction = data.get("direction", "sent")
        
        if not cp_id or not message_type:
            raise HTTPException(status_code=400, detail="cp_id and type are required")
            

            raise HTTPException(status_code=404, detail=f"No active simulation for CP ID: {cp_id}")
            
        # Create message record
        message_record = {
            "timestamp": utc_now_iso(),
            "type": message_type,
            "direction": direction,
            "payload": payload
        }
        
        # Add to simulation state
        sim_state[cp_id].get("messages", []).append(message_record)
        
        # Log to collector
        await COLLECTOR.record_action(cp_id, f"Simulation_{message_type}", {
            "direction": direction,
            "payload": payload
        })
        
        return JSONResponse({
            "result": "message_logged",
            "message": message_record
        })
    except Exception as e:
        LOGGER.error(f"Error sending OCPP simulation message: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/ocpp/simulate")
async def list_active_simulations():
    """List all active OCPP simulations"""
    from auto_simulator import simulation_state as sim_state

    active_sims = {}
    for cp_id, state in sim_state.items():
        if state.get("running", False):
            active_sims[cp_id] = {
                "ws_url": state["ws_url"],
                "start_time": state["start_time"],
                "message_count": len(state.get("messages", []))
            }
    
    return JSONResponse({
        "active_simulations": active_sims,
        "count": len(active_sims)
    })


# Global simulation state (in production, use Redis or database)

@app.get("/api/auth/me")
def auth_me(request: Request):
    user = require_authenticated_user(request)
    return JSONResponse({"authenticated": True, **public_user(user)})


@app.get("/health")
def health():
    return JSONResponse(
        {
            "status": "ok",
            "service": "CityOs backend",
            "api": "/api/cp",
        }
    )


@app.get("/")
def root(request: Request):
    return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/login")
def login_page(request: Request):
    if get_authenticated_user(request) is not None:
        return RedirectResponse(url="/cityos", status_code=status.HTTP_303_SEE_OTHER)

    login_failed = request.query_params.get("login") == "failed"
    error_message = "Invalid email or password" if login_failed else ""
    html = f"""<!doctype html>
<html lang="en">
    <head>
        <meta charset="UTF-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1.0" />
        <title>CityOs Login</title>
        <meta name="description" content="CityOs login page" />
        <link rel="canonical" href="/login" />
        <link rel="stylesheet" href="/frontend/styles.css" />
    </head>
    <body>
        <div class="bg-orb bg-orb-a"></div>
        <div class="bg-orb bg-orb-b"></div>
        <section class="login-screen shell">
            <div class="login-card single-column">
                <div class="login-copy">
                    <p class="eyebrow">CityOs</p>
                    <h1>Sign in to CityOs</h1>
                    <p class="lede">Access the live OCPP dashboard, remote actions, and session activity from a single secure entry point.</p>
                </div>
                <form id="login-form" class="login-form" action="/api/auth/login?next=/cityos" method="post">
                    <label>Email <input name="email" type="email" autocomplete="email" value="{AUTH_EMAIL}" required /></label>
                    <label>Password <input name="password" type="password" autocomplete="current-password" placeholder="Enter your password" required /></label>
                    <p class="login-error" aria-live="polite">{error_message}</p>
                    <button class="btn btn-primary login-submit" type="submit">Sign in</button>
                    <p class="note login-note">Use the provided credentials to continue.</p>
                </form>
            </div>
        </section>
    </body>
</html>"""
    return HTMLResponse(html)


@app.get("/cityos")
def cityos_dashboard(request: Request):
    if get_authenticated_user(request) is None:
        return RedirectResponse(url="/login?next=/cityos", status_code=status.HTTP_303_SEE_OTHER)

    if FRONTEND_DASHBOARD_FILE.exists():
        return HTMLResponse(FRONTEND_DASHBOARD_FILE.read_text(encoding="utf-8"))

    return HTMLResponse(FRONTEND_INDEX_FILE.read_text(encoding="utf-8"))


@app.get("/ocpp-simulator")
async def ocpp_simulator_frontend():
    """Serve the OCPP simulator frontend"""
    try:
        with open("templates/index.html", "r", encoding="utf-8") as f:
            html_content = f.read()
        return HTMLResponse(content=html_content)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Simulator frontend not found")


def serve_dashboard(request: Request):
    email = request.query_params.get("email")
    password = request.query_params.get("password")
    if email is not None and password is not None:
        user = authenticate_user(email.strip(), password)
        if user is not None:
            return build_auth_response(user["email"], wants_json=False)

    if get_authenticated_user(request) is None:
        return RedirectResponse(url="/login?next=/cityos", status_code=status.HTTP_303_SEE_OTHER)

    if FRONTEND_INDEX_FILE.exists():
        return HTMLResponse(FRONTEND_INDEX_FILE.read_text(encoding="utf-8"))

    return JSONResponse(
        {
            "status": "ok",
            "service": "CityOs backend",
            "api": "/api/cp",
        }
    )


async def read_request_payload(request: Request) -> dict:
    payload = dict(request.query_params)
    body = await request.body()
    if not body:
        return payload

    text_body = body.decode("utf-8", errors="ignore").strip()
    if not text_body:
        return payload

    content_type = (request.headers.get("content-type") or "").lower()
    if "application/json" in content_type:
        try:
            json_body = json.loads(text_body)
        except json.JSONDecodeError:
            json_body = None
        if isinstance(json_body, dict):
            payload.update(json_body)
        return payload

    form_data = parse_qs(text_body, keep_blank_values=True)
    payload.update({key: values[0] if len(values) == 1 else values for key, values in form_data.items()})
    return payload


def normalize_metadata_payload(payload: dict) -> dict:
    normalized = dict(payload or {})
    alias_map = {
        "manufacturer": ["manufacturer", "vendor", "charge_point_vendor", "chargePointVendor"],
        "model": ["model", "charge_point_model", "chargePointModel"],
        "serialNumber": ["serialNumber", "serial_number", "charge_point_serial_number", "chargePointSerialNumber"],
        "firmwareVersion": ["firmwareVersion", "firmware_version", "firmware"],
        "ipAddress": ["ipAddress", "ip_address"],
        "iccid": ["iccid", "ICCID"],
        "imsi": ["imsi", "IMSI"],
        "commissioningDate": ["commissioningDate", "commissioning_date"],
        "uptime": ["uptime"],
        "rebootLogs": ["rebootLogs", "reboot_logs"],
    }

    for target, candidates in alias_map.items():
        if normalized.get(target) is not None:
            continue
        for candidate in candidates:
            if normalized.get(candidate) is not None:
                normalized[target] = normalized.get(candidate)
                break

    return normalized


def update_metadata_store(cp_id: str, payload: dict) -> dict:
    metadata = read_meta()
    existing = metadata.get(cp_id, {}) if isinstance(metadata, dict) else {}
    normalized = normalize_metadata_payload(payload)
    updated = {**existing, **normalized, "cp_id": cp_id}
    if not updated.get("created_at"):
        updated["created_at"] = existing.get("created_at") or utc_now_iso()
    updated["updated_at"] = utc_now_iso()
    metadata[cp_id] = updated
    write_meta(metadata)
    return updated


async def command_response(cp_id: str, action: str, payload: dict | None = None, *, status_after: str | None = None, transaction_id: int | None = None) -> JSONResponse:
    details = payload or {}
    await COLLECTOR.record_action(cp_id, action, details)
    if status_after:
        await COLLECTOR.update_status(cp_id, {"status": status_after})

    response_payload: dict = {
        "result": "sent",
        "response_status": "Accepted",
        "cp_id": cp_id,
        "action": action,
    }
    if transaction_id is not None:
        response_payload["transaction_id"] = transaction_id
    response_payload.update(details)
    return JSONResponse(response_payload)
@app.get("/api/cp/{cp_id}/meta")
async def get_cp_meta(cp_id: str):
    metadata = read_meta()
    record = metadata.get(cp_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No metadata found for CP ID: {cp_id}")

    return JSONResponse({"cp_id": cp_id, "metadata": record})


@app.post("/api/cp/{cp_id}/meta")
async def post_cp_meta(cp_id: str, request: Request):
    require_role(request, FULL_MANAGEMENT_ROLES)
    payload = await read_request_payload(request)
    updated = update_metadata_store(cp_id, payload)
    await COLLECTOR.record_action(cp_id, "MetadataUpdate", {"metadata": updated})
    return JSONResponse({"status": "ok", "cp_id": cp_id, "metadata": updated})


@app.post("/api/charge-points")
async def create_charge_point(request: Request):
    require_role(request, FULL_MANAGEMENT_ROLES)
    payload = await read_request_payload(request)
    charge_point = upsert_charge_point_record(payload)
    await COLLECTOR.record_action(charge_point["cp_id"], "ChargePointUpsert", {"charge_point": charge_point})
    return JSONResponse({"status": "ok", "charge_point": charge_point})


@app.get('/api/stations')
async def list_stations(request: Request):
    require_authenticated_user(request)
    stations = read_stations()
    return JSONResponse({"stations": stations})


@app.post('/api/stations')
async def create_station(request: Request):
    require_role(request, FULL_MANAGEMENT_ROLES)
    payload = await read_request_payload(request)
    station = upsert_station(payload)
    await COLLECTOR.record_action('SYSTEM', 'StationUpsert', {'station_id': station['id'], 'station': station})
    return JSONResponse({'status': 'ok', 'station': station})


@app.put('/api/stations/{station_id}')
async def update_station(station_id: str, request: Request):
    require_role(request, FULL_MANAGEMENT_ROLES)
    payload = await read_request_payload(request)
    payload['id'] = station_id
    station = upsert_station(payload)
    await COLLECTOR.record_action('SYSTEM', 'StationUpdate', {'station_id': station_id})
    return JSONResponse({'status': 'ok', 'station': station})


@app.delete('/api/stations/{station_id}')
async def delete_station(station_id: str, request: Request):
    require_role(request, FULL_MANAGEMENT_ROLES)
    stations = read_stations()
    new_list = [s for s in stations if str(s.get('id')) != station_id]
    write_stations(new_list)
    await COLLECTOR.record_action('SYSTEM', 'StationDelete', {'station_id': station_id})
    return JSONResponse({'status': 'ok', 'deleted': station_id})


@app.get('/api/stations/{station_id}/chargers')
async def list_station_chargers(station_id: str, request: Request):
    require_authenticated_user(request)
    registry = read_charge_point_registry()
    chargers = [cp for cp in registry if str(cp.get('station_id') or '') == station_id]
    return JSONResponse({'station_id': station_id, 'chargers': chargers})


@app.post('/api/chargers/{cp_id}/status')
async def set_charger_status(cp_id: str, request: Request):
    require_role(request, COMMAND_ROLES)
    payload = await read_request_payload(request)
    status_value = str(payload.get('status') or payload.get('state') or '').strip()
    if not status_value:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='status is required')
    # normalize and validate status
    normalized = status_value.strip().lower()
    allowed = {'available', 'charging', 'faulted', 'maintenance', 'offline'}
    if normalized not in allowed:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f'status must be one of: {sorted(list(allowed))}')

    # fetch existing record and update
    existing = get_charge_point_record(cp_id) or {}
    existing['status'] = normalized
    existing['station_id'] = payload.get('station_id') or existing.get('station_id')
    updated = upsert_charge_point_record(existing)
    await COLLECTOR.update_status(cp_id, {'status': normalized})
    await COLLECTOR.record_action(cp_id, 'StatusSet', {'status': normalized})
    return JSONResponse({'status': 'ok', 'charge_point': updated})



@app.post('/api/chargers/{cp_id}/assign')
async def assign_charger(cp_id: str, request: Request):
    # allow operators and admins to assign
    require_role(request, COMMAND_ROLES)
    payload = await read_request_payload(request)
    user_email = str(payload.get('user_email') or payload.get('email') or '').strip().lower()
    if not user_email:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='user_email is required')

    user = get_user_by_email(user_email)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='User not found')

    existing = get_charge_point_record(cp_id) or {}
    existing['assigned_user'] = user_email
    updated = upsert_charge_point_record(existing)
    await COLLECTOR.record_action(cp_id, 'AssignUser', {'user_email': user_email})
    return JSONResponse({'status': 'ok', 'charge_point': updated})


@app.post('/api/chargers/{cp_id}/unassign')
async def unassign_charger(cp_id: str, request: Request):
    require_role(request, COMMAND_ROLES)
    existing = get_charge_point_record(cp_id) or {}
    existing.pop('assigned_user', None)
    updated = upsert_charge_point_record(existing)
    await COLLECTOR.record_action(cp_id, 'UnassignUser', {})
    return JSONResponse({'status': 'ok', 'charge_point': updated})


@app.post("/api/charge-points/{cp_id}/tariff")
async def set_charge_point_tariff(cp_id: str, request: Request):
    require_role(request, FULL_MANAGEMENT_ROLES)
    payload = await read_request_payload(request)
    tariff_value = payload.get("tariff_per_kwh", payload.get("tariff"))
    if tariff_value is None or str(tariff_value).strip() == "":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="tariff_per_kwh is required")

    updated = update_charge_point_tariff(cp_id, float(tariff_value))
    await COLLECTOR.record_action(cp_id, "TariffUpdate", {"tariff_per_kwh": updated["tariff_per_kwh"]})
    return JSONResponse({"status": "ok", "charge_point": updated})


@app.post("/api/users")
async def create_user(request: Request):
    require_role(request, FULL_MANAGEMENT_ROLES)
    payload = await read_request_payload(request)
    email = str(payload.get("email") or "").strip().lower()
    password = str(payload.get("password") or "")
    if not email:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="email is required")

    role = str(payload.get("role") or "operator").strip().lower()
    if role not in {"admin", "institution", "operator"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid role")

    users = read_users()
    existing_user = None
    for user in users:
        if str(user.get("email", "")).strip().lower() == email:
            existing_user = user
            break

    if existing_user is None and not password:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="password is required for new users")

    user_record = {
        "id": (existing_user or {}).get("id") or f"user-{email.replace('@', '-at-').replace('.', '-')}",
        "email": email,
        "password_hash": (existing_user or {}).get("password_hash") or hash_password(password),
        "role": role,
        "name": str(payload.get("name") or payload.get("full_name") or email).strip(),
        "active": bool(payload.get("active", True)),
        "created_at": (existing_user or {}).get("created_at") or utc_now_iso(),
    }
    if password:
        user_record["password_hash"] = hash_password(password)

    if existing_user is None:
        users.append(user_record)
    else:
        existing_user.update(user_record)

    write_users(users)
    await COLLECTOR.record_action("SYSTEM", "UserUpsert", {"email": email, "role": role})
    return JSONResponse({"status": "ok", "user": public_user(user_record)})


@app.post("/api/cp/{cp_id}/force_start")
async def force_start(cp_id: str, request: Request):
    require_role(request, COMMAND_ROLES)
    payload = await read_request_payload(request)
    connector_id = int(payload.get("connector_id") or 1)
    meter_start = float(payload.get("meter_start") or 0)
    transaction_id = await COLLECTOR.start_transaction(cp_id, {"connector_id": connector_id, "meter_start": meter_start, "mode": "force_start"})
    await COLLECTOR.update_status(cp_id, {"status": "Charging"})
    await COLLECTOR.record_action(cp_id, "ForceStart", {"connector_id": connector_id, "meter_start": meter_start, "transaction_id": transaction_id})
    return JSONResponse({"result": "sent", "response_status": "Accepted", "cp_id": cp_id, "action": "ForceStart", "transaction_id": transaction_id})


@app.post("/api/cp/{cp_id}/force_stop")
async def force_stop(cp_id: str, request: Request):
    require_role(request, COMMAND_ROLES)
    payload = await read_request_payload(request)
    transaction_id = normalize_transaction_id(payload.get("transaction_id"))
    if transaction_id is None:
        open_tx = await COLLECTOR.get_open_transaction(cp_id)
        transaction_id = (open_tx or {}).get("transaction_id")

    await COLLECTOR.stop_transaction(cp_id, {"transaction_id": transaction_id, "meter_stop": payload.get("meter_stop"), "mode": "force_stop"})
    await COLLECTOR.update_status(cp_id, {"status": "Available"})
    await COLLECTOR.record_action(cp_id, "ForceStop", {"transaction_id": transaction_id, "meter_stop": payload.get("meter_stop")})
    return JSONResponse({"result": "sent", "response_status": "Accepted", "cp_id": cp_id, "action": "ForceStop", "transaction_id": transaction_id})


@app.post("/api/cp/{cp_id}/remote_start")
async def remote_start(cp_id: str, request: Request):
    require_role(request, COMMAND_ROLES)
    payload = await read_request_payload(request)
    connector_id = int(payload.get("connector_id") or 1)
    id_tag = str(payload.get("id_tag") or f"REMOTE_{cp_id}")
    transaction_id = await COLLECTOR.start_transaction(cp_id, {"connector_id": connector_id, "id_tag": id_tag, "mode": "remote_start"})
    await COLLECTOR.update_status(cp_id, {"status": "Charging"})
    await COLLECTOR.record_action(cp_id, "RemoteStartTransaction", {"connector_id": connector_id, "id_tag": id_tag, "transaction_id": transaction_id})
    return JSONResponse({"result": "sent", "response_status": "Accepted", "cp_id": cp_id, "action": "RemoteStartTransaction", "transaction_id": transaction_id})


@app.post("/api/cp/{cp_id}/remote_stop")
async def remote_stop(cp_id: str, request: Request):
    require_role(request, COMMAND_ROLES)
    payload = await read_request_payload(request)
    transaction_id = normalize_transaction_id(payload.get("transaction_id"))
    if transaction_id is None:
        open_tx = await COLLECTOR.get_open_transaction(cp_id)
        transaction_id = (open_tx or {}).get("transaction_id")

    await COLLECTOR.stop_transaction(cp_id, {"transaction_id": transaction_id, "mode": "remote_stop"})
    await COLLECTOR.update_status(cp_id, {"status": "Available"})
    await COLLECTOR.record_action(cp_id, "RemoteStopTransaction", {"transaction_id": transaction_id})
    return JSONResponse({"result": "sent", "response_status": "Accepted", "cp_id": cp_id, "action": "RemoteStopTransaction", "transaction_id": transaction_id})


@app.post("/api/cp/{cp_id}/remote_reboot")
async def remote_reboot(cp_id: str, request: Request):
    require_role(request, COMMAND_ROLES)
    payload = await read_request_payload(request)
    reset_type = str(payload.get("reset_type") or "Soft")
    metadata = update_metadata_store(cp_id, {"rebootLogs": list(read_meta().get(cp_id, {}).get("rebootLogs", [])) + [{"timestamp": utc_now_iso(), "reset_type": reset_type}]})
    await COLLECTOR.record_action(cp_id, "Reset", {"reset_type": reset_type})
    await COLLECTOR.update_status(cp_id, {"status": "Available"})
    return JSONResponse({"result": "sent", "response_status": "Accepted", "cp_id": cp_id, "action": "Reset", "reset_type": reset_type, "metadata": metadata})


@app.post("/api/cp/{cp_id}/unlock_connector")
async def unlock_connector(cp_id: str, request: Request):
    require_role(request, COMMAND_ROLES)
    payload = await read_request_payload(request)
    connector_id = int(payload.get("connector_id") or 1)
    await COLLECTOR.record_action(cp_id, "UnlockConnector", {"connector_id": connector_id})
    return JSONResponse({"result": "sent", "response_status": "Accepted", "cp_id": cp_id, "action": "UnlockConnector", "connector_id": connector_id})



@app.get("/api/cp")
async def cp_data(request: Request):
    """Get CPMS data for dashboard"""
    # support optional filtering by query params: user_email, station_id, cp_id
    payload = build_cp_payload()
    q = dict(request.query_params)
    user_email = q.get('user_email') or q.get('email')
    station_id = q.get('station_id')
    cp_id = q.get('cp_id')

    # filter by cp_id exact
    if cp_id:
        # restrict charge_points and sessions to this cp_id
        payload['charge_points'] = [cp for cp in payload.get('charge_points', []) if cp.get('cp_id') == cp_id]
        payload['sessions'] = {k: v for k, v in payload.get('sessions', {}).items() if k == cp_id}
        return JSONResponse(payload)

    # determine cp_ids from station filter
    cp_ids_by_station = None
    if station_id:
        registry = {str(rec.get('cp_id')): rec for rec in read_charge_point_registry()}
        cp_ids_by_station = {cp_id for cp_id, rec in registry.items() if str(rec.get('station_id') or '') == str(station_id)}

    # filter sessions by user_email or station
    if user_email or cp_ids_by_station is not None:
        sessions = payload.get('sessions', {})
        new_sessions = {}
        for cpkey, sess_list in sessions.items():
            # station filter: skip sessions not in station
            if cp_ids_by_station is not None and cpkey not in cp_ids_by_station:
                continue

            filtered = []
            for s in sess_list:
                keep = True
                if user_email:
                    ue = str(user_email).strip().lower()
                    su = str(s.get('user') or '').strip().lower()
                    if ue not in {su, str(s.get('id_tag') or '').strip().lower()}:
                        keep = False
                if keep:
                    filtered.append(s)

            if filtered:
                new_sessions[cpkey] = filtered

        payload['sessions'] = new_sessions

        # also filter charge_points list to those with sessions or assigned user
        cps = payload.get('charge_points', [])
        if user_email:
            ue = str(user_email).strip().lower()
            cps = [c for c in cps if (str(c.get('assigned_user') or '').strip().lower() == ue) or (c.get('cp_id') in payload['sessions'])]
        elif cp_ids_by_station is not None:
            cps = [c for c in cps if c.get('cp_id') in cp_ids_by_station]

        payload['charge_points'] = cps

    return JSONResponse(payload)


@app.get('/api/institution/metrics')
async def institution_metrics(request: Request):
    """Aggregate simple institution-level metrics by zone."""
    require_role(request, {'admin', 'institution'})

    payload = build_cp_payload()
    stations = read_stations()
    registry = {str(rec.get('cp_id')): rec for rec in read_charge_point_registry()}

    # Map station id -> zone
    station_zone = {s.get('id'): s.get('zone') or 'unknown' for s in stations}

    zones: dict[str, dict] = {}
    # initialize zones from stations
    for s in stations:
        z = s.get('zone') or 'unknown'
        zones.setdefault(z, {'stations': 0, 'chargers': 0, 'sessions': 0, 'energy_kwh': 0.0})
        zones[z]['stations'] += 1

    # count chargers per zone
    for cp in payload.get('registered_charge_points', []) + payload.get('charge_points', []):
        cp_id = str(cp.get('cp_id'))
        station_id = str(cp.get('station_id') or '')
        zone = station_zone.get(station_id) or 'unknown'
        zones.setdefault(zone, {'stations': 0, 'chargers': 0, 'sessions': 0, 'energy_kwh': 0.0})
        zones[zone]['chargers'] += 1

    # aggregate sessions energy per cp and assign to zone
    sessions = payload.get('sessions', {})
    for cp_id, sess_list in sessions.items():
        cp_rec = registry.get(cp_id, {})
        station_id = str(cp_rec.get('station_id') or '')
        zone = station_zone.get(station_id) or 'unknown'
        for s in sess_list:
            energy = s.get('energy_kwh')
            if energy is None:
                continue
            zones.setdefault(zone, {'stations': 0, 'chargers': 0, 'sessions': 0, 'energy_kwh': 0.0})
            zones[zone]['sessions'] += 1
            try:
                zones[zone]['energy_kwh'] += float(energy)
            except Exception:
                pass

    # compute derived metrics
    for z, metrics in zones.items():
        sessions_n = metrics.get('sessions') or 0
        metrics['avg_kwh_per_session'] = round((metrics['energy_kwh'] / sessions_n) if sessions_n else 0.0, 3)

    # totals
    totals = {'stations': 0, 'chargers': 0, 'sessions': 0, 'energy_kwh': 0.0}
    for m in zones.values():
        totals['stations'] += int(m.get('stations') or 0)
        totals['chargers'] += int(m.get('chargers') or 0)
        totals['sessions'] += int(m.get('sessions') or 0)
        totals['energy_kwh'] += float(m.get('energy_kwh') or 0.0)

    return JSONResponse({'zones': zones, 'totals': totals})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", os.environ.get("APP_PORT", "5000")))
    port = resolve_server_port(port)
    uvicorn.run("app:app", host="0.0.0.0", port=port, log_level="info")
