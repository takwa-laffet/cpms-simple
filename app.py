import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
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
FRONTEND_DIR = Path("frontend")
FRONTEND_INDEX_FILE = FRONTEND_DIR / "index.html"
FRONTEND_DASHBOARD_FILE = FRONTEND_DIR / "dashboard.html"
AUTH_COOKIE_NAME = "cpms_auth"
AUTH_EMAIL = os.environ.get("CPMS_LOGIN_EMAIL", "mvp@prelabel.tn")
AUTH_PASSWORD = os.environ.get("CPMS_LOGIN_PASSWORD", "Cpms_Secure#48Tz@2026")
AUTH_SECRET_KEY = os.environ.get("AUTH_SECRET_KEY", "cityos-dev-secret")
AUTH_SESSION_TTL_SECONDS = int(os.environ.get("AUTH_SESSION_TTL_SECONDS", "86400"))
BILLING_TARIFF_PER_KWH = float(os.environ.get("CITYOS_TARIFF_PER_KWH", "0.35"))
SUPERVISION_ROLES = {"admin", "steg"}
FULL_MANAGEMENT_ROLES = {"admin"}
USER_MANAGEMENT_ROLES = {"admin"}


class LoginPayload(BaseModel):
    email: str
    password: str


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def current_unix_timestamp() -> int:
    return int(datetime.now(timezone.utc).timestamp())


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
                "name": "Rback Admin",
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
    is_connected = cp_id in COLLECTOR.active_connections
    ocpp_status = status_payload.get("status")
    if is_connected:
        visible_status = ocpp_status or "Available"
    else:
        visible_status = "Offline"

    current_tx = snapshot.get("open_transactions_by_cp", {}).get(cp_id) or {}
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


def aggregate_session_meter_values(session: dict, borne_log_entries: list[dict]) -> dict:
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
        if total_kwh is not None:
            session["price"] = round(float(total_kwh) * BILLING_TARIFF_PER_KWH, 2)
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
    if energy_kwh is not None:
        session["price"] = round(float(energy_kwh) * BILLING_TARIFF_PER_KWH, 2)
        session["currency"] = "dt"

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

    billing = {
        "tariff_per_kwh": BILLING_TARIFF_PER_KWH,
        "currency": "dt",
        "total_energy_kwh": total_kwh,
        "estimated_revenue": round(total_kwh * BILLING_TARIFF_PER_KWH, 2),
        "revenue_per_cp": {cp_id: round(kwh * BILLING_TARIFF_PER_KWH, 2) for cp_id, kwh in kwh_per_cp.items()},
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


@app.get("/api/auth/me")
def auth_me(request: Request):
    user = require_authenticated_user(request)
    return JSONResponse({"authenticated": True, **public_user(user)})


@app.get("/health")
def health():
    return JSONResponse(
        {
            "status": "ok",
            "service": "Rback backend",
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
        <title>Rback Login</title>
        <meta name="description" content="Rback login page" />
        <link rel="canonical" href="/login" />
        <link rel="stylesheet" href="/frontend/styles.css" />
    </head>
    <body>
        <div class="bg-orb bg-orb-a"></div>
        <div class="bg-orb bg-orb-b"></div>
        <section class="login-screen shell">
            <div class="login-card single-column">
                <div class="login-copy">
                    <p class="eyebrow">Rback</p>
                    <h1>Sign in to Rback</h1>
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


def serve_dashboard(request: Request):
    email = request.query_params.get("email")
    password = request.query_params.get("password")
    if email is not None and password is not None:
        user = authenticate_user(email.strip(), password)
        if user is not None:
            return build_auth_response(user["email"], wants_json=False)

        return RedirectResponse(url="/login?login=failed", status_code=status.HTTP_303_SEE_OTHER)

    if get_authenticated_user(request) is None:
        return RedirectResponse(url="/login?next=/cityos", status_code=status.HTTP_303_SEE_OTHER)

    if FRONTEND_INDEX_FILE.exists():
        return HTMLResponse(FRONTEND_INDEX_FILE.read_text(encoding="utf-8"))

    return JSONResponse(
        {
            "status": "ok",
            "service": "Rback backend",
            "api": "/api/cp",
        }
    )


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


@app.get("/api/users")
def list_users(request: Request):
    require_role(request, USER_MANAGEMENT_ROLES)
    return JSONResponse({"users": [public_user(user) for user in read_users()]})


@app.post("/api/users")
def create_user(request: Request, user: dict):
    require_role(request, USER_MANAGEMENT_ROLES)
    email = str(user.get("email") or "").strip().lower()
    password = str(user.get("password") or "").strip()
    role = str(user.get("role") or "operator").strip().lower()

    if not email or not password:
        raise HTTPException(status_code=400, detail="email and password are required")

    if role not in {"admin", "steg", "operator"}:
        raise HTTPException(status_code=400, detail="Invalid role")

    users = read_users()
    if any(str(existing.get("email", "")).strip().lower() == email for existing in users):
        raise HTTPException(status_code=409, detail="User already exists")

    record = {
        "id": f"user-{len(users) + 1}",
        "email": email,
        "password_hash": hash_password(password),
        "role": role,
        "name": user.get("name") or email,
        "active": bool(user.get("active", True)),
        "created_at": utc_now_iso(),
    }
    users.append(record)
    write_users(users)
    return JSONResponse({"result": "ok", "user": public_user(record)})


@app.get("/api/charge-points")
def list_charge_points(request: Request):
    require_authenticated_user(request)
    payload = build_cp_payload()
    return JSONResponse({"charge_points": payload.get("charge_points", [])})


@app.post("/api/charge-points")
def create_charge_point(request: Request, charge_point: dict):
    require_role(request, FULL_MANAGEMENT_ROLES)
    record = upsert_charge_point_record(charge_point)
    return JSONResponse({"result": "ok", "charge_point": record})


@app.post("/api/charge-points/{cp_id}/tariff")
def update_charge_point_tariff_endpoint(request: Request, cp_id: str, payload: dict):
    require_role(request, SUPERVISION_ROLES)
    if not cp_id:
        raise HTTPException(status_code=400, detail="cp_id is required")

    tariff_per_kwh = payload.get("tariff_per_kwh")
    if tariff_per_kwh is None:
        raise HTTPException(status_code=400, detail="tariff_per_kwh is required")

    updated = update_charge_point_tariff(cp_id, float(tariff_per_kwh))
    return JSONResponse({"result": "ok", "charge_point": updated})


@app.get("/api/sessions")
def list_sessions(request: Request):
    require_authenticated_user(request)
    payload = build_cp_payload()
    sessions = []
    for cp_id, sess_list in (payload.get("sessions") or {}).items():
        for session in sess_list:
            sessions.append({"cp_id": cp_id, **session})
    return JSONResponse({"sessions": sessions})


@app.get("/api/billing/summary")
def billing_summary(request: Request):
    require_authenticated_user(request)
    payload = build_cp_payload()
    summary = payload.get("billing") or {}
    return JSONResponse(summary)


@app.post("/api/cp/{cp_id}/force_start")
async def force_start(cp_id: str, connector_id: str = "1", meter_start: str = "0"):
    """Prefer a real remote start, then fall back to server-side tracking."""
    parsed_connector = int(connector_id) if connector_id else 1
    parsed_meter = int(meter_start) if meter_start else 0
    if OCPP_AVAILABLE and call is not None:
        cp_instance = await COLLECTOR.get_charge_point(cp_id)
        if cp_instance is not None:
            remote_response = await remote_start(cp_id, connector_id=str(parsed_connector))
            if getattr(remote_response, "status_code", 200) == 200:
                return remote_response

    tx_id = await COLLECTOR.start_transaction(cp_id, {"connector_id": parsed_connector, "meter_start": parsed_meter})
    await COLLECTOR.record_action(cp_id, "ForceStart", {"transaction_id": tx_id, "connector_id": parsed_connector, "meter_start": parsed_meter})
    return JSONResponse({"result": "started", "transaction_id": tx_id})


@app.post("/api/cp/{cp_id}/force_stop")
async def force_stop(cp_id: str, transaction_id: str = "", meter_stop: str = "0"):
    """Force-stop a transaction server-side without requiring CP StopTransaction."""
    # Parse transaction_id: accept string or int, convert empty to None
    parsed_tx_id = None
    if transaction_id:
        try:
            parsed_tx_id = int(transaction_id)
        except (ValueError, TypeError):
            pass
    parsed_meter = int(meter_stop) if meter_stop else 0
    payload = {"meter_stop": parsed_meter, "transaction_id": parsed_tx_id}
    await COLLECTOR.record_action(cp_id, "ForceStop", payload)
    await COLLECTOR.stop_transaction(cp_id, payload)
    return JSONResponse({"result": "stopped", "transaction_id": parsed_tx_id})


@app.post("/api/cp/{cp_id}/remote_start")
async def remote_start(cp_id: str, connector_id: str = "", id_tag: str = ""):
    """Send a real OCPP RemoteStartTransaction to the connected charger.

    The API caller does not need to provide an id_tag; a server-side default is used.
    """
    parsed_connector = int(connector_id) if connector_id else None
    if not OCPP_AVAILABLE or call is None:
        return JSONResponse({"result": "error", "detail": "OCPP is not available on this server"}, status_code=503)

    cp_instance = await COLLECTOR.get_charge_point(cp_id)
    if cp_instance is None:
        return JSONResponse({"result": "error", "detail": f"Charge point {cp_id} is not connected"}, status_code=404)

    effective_id_tag = id_tag or read_meta().get(cp_id, {}).get("remoteStartIdTag") or read_meta().get(cp_id, {}).get("defaultIdTag") or "REMOTE_START"

    def build_request(include_connector: bool):
        request_kwargs = {"id_tag": effective_id_tag}
        if include_connector and parsed_connector is not None:
            request_kwargs["connector_id"] = parsed_connector
        return call.RemoteStartTransaction(**request_kwargs)

    try:
        request = build_request(include_connector=True)
        response = await cp_instance.call(request)
        response_status = getattr(response, "status", None)

        # Some chargers ignore or reject connector_id; retry once without it.
        if str(response_status).lower() not in ("accepted", "accept", "ok") and parsed_connector is not None:
            fallback_request = build_request(include_connector=False)
            fallback_response = await cp_instance.call(fallback_request)
            fallback_status = getattr(fallback_response, "status", None)
            await COLLECTOR.record_action(
                cp_id,
                "RemoteStartTransaction",
                {
                    "connector_id": parsed_connector,
                    "id_tag": effective_id_tag,
                    "response_status": response_status,
                    "fallback_used": True,
                    "fallback_response_status": fallback_status,
                },
            )
            return JSONResponse({
                "result": "sent",
                "response_status": fallback_status,
                "connector_id": parsed_connector,
                "connector_id_used": None,
                "id_tag_used": effective_id_tag,
                "fallback_used": True,
                "initial_response_status": response_status,
            })

        await COLLECTOR.record_action(
            cp_id,
            "RemoteStartTransaction",
            {
                "connector_id": parsed_connector,
                "id_tag": effective_id_tag,
                "response_status": response_status,
                "fallback_used": False,
            },
        )
        return JSONResponse({
            "result": "sent",
            "response_status": response_status,
            "connector_id": parsed_connector,
            "connector_id_used": parsed_connector,
            "id_tag_used": effective_id_tag,
            "fallback_used": False,
        })
    except Exception as error:
        LOGGER.exception("RemoteStartTransaction failed for %s: %s", cp_id, error)
        return JSONResponse({"result": "error", "detail": str(error)}, status_code=500)


@app.post("/api/cp/{cp_id}/remote_stop")
async def remote_stop(cp_id: str, transaction_id: str = ""):
    """Send a real OCPP RemoteStopTransaction to the connected charger."""
    if not OCPP_AVAILABLE or call is None:
        return JSONResponse({"result": "error", "detail": "OCPP is not available on this server"}, status_code=503)

    cp_instance = await COLLECTOR.get_charge_point(cp_id)
    if cp_instance is None:
        return JSONResponse({"result": "error", "detail": f"Charge point {cp_id} is not connected"}, status_code=404)

    parsed_tx_id = int(transaction_id) if transaction_id else None
    effective_transaction_id = parsed_tx_id
    if effective_transaction_id is None:
        open_transaction = await COLLECTOR.get_open_transaction(cp_id)
        if open_transaction is not None:
            effective_transaction_id = normalize_transaction_id(open_transaction.get("transaction_id"))

    if effective_transaction_id is None:
        return JSONResponse(
            {
                "result": "error",
                "detail": "Transaction ID is required when no transaction is currently open",
            },
            status_code=400,
        )

    request = call.RemoteStopTransaction(transaction_id=effective_transaction_id)
    try:
        response = await cp_instance.call(request)
        await COLLECTOR.record_action(cp_id, "RemoteStopTransaction", {"transaction_id": effective_transaction_id, "response_status": getattr(response, "status", None)})
        return JSONResponse({"result": "sent", "response_status": getattr(response, "status", None), "transaction_id": effective_transaction_id})
    except Exception as error:
        LOGGER.exception("RemoteStopTransaction failed for %s: %s", cp_id, error)
        return JSONResponse({"result": "error", "detail": str(error)}, status_code=500)


@app.post("/api/cp/{cp_id}/remote_reboot")
async def remote_reboot(cp_id: str, reset_type: str = "Soft"):
    """Send a real OCPP Reset command (reboot) to the connected charger."""
    if not OCPP_AVAILABLE or call is None:
        return JSONResponse({"result": "error", "detail": "OCPP is not available on this server"}, status_code=503)

    cp_instance = await COLLECTOR.get_charge_point(cp_id)
    if cp_instance is None:
        return JSONResponse({"result": "error", "detail": f"Charge point {cp_id} is not connected"}, status_code=404)

    normalized_reset = (reset_type or "Soft").strip().lower()
    ocpp_reset_type = ResetType.soft if normalized_reset != "hard" else ResetType.hard

    request = call.Reset(type_=ocpp_reset_type)
    try:
        response = await cp_instance.call(request)
        await COLLECTOR.record_action(cp_id, "Reset", {"reset_type": str(ocpp_reset_type), "response_status": getattr(response, "status", None)})
        return JSONResponse({"result": "sent", "response_status": getattr(response, "status", None), "reset_type": str(ocpp_reset_type)})
    except Exception as error:
        LOGGER.exception("Reset failed for %s: %s", cp_id, error)
        return JSONResponse({"result": "error", "detail": str(error)}, status_code=500)


@app.post("/api/cp/{cp_id}/unlock_connector")
async def unlock_connector(cp_id: str, connector_id: str = "1"):
    """Send a real OCPP UnlockConnector command to the connected charger."""
    if not OCPP_AVAILABLE or call is None:
        return JSONResponse({"result": "error", "detail": "OCPP is not available on this server"}, status_code=503)

    cp_instance = await COLLECTOR.get_charge_point(cp_id)
    if cp_instance is None:
        return JSONResponse({"result": "error", "detail": f"Charge point {cp_id} is not connected"}, status_code=404)

    parsed_connector = int(connector_id) if connector_id else 1
    request = call.UnlockConnector(connector_id=parsed_connector)
    try:
        response = await cp_instance.call(request)
        await COLLECTOR.record_action(cp_id, "UnlockConnector", {"connector_id": parsed_connector, "response_status": getattr(response, "status", None)})
        return JSONResponse({"result": "sent", "response_status": getattr(response, "status", None), "connector_id": parsed_connector})
    except Exception as error:
        LOGGER.exception("UnlockConnector failed for %s: %s", cp_id, error)
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
            payload = normalize_boot_payload({
                "charge_point_model": charge_point_model,
                "charge_point_vendor": charge_point_vendor,
                **kwargs,
            })
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
            transaction_id = await COLLECTOR.start_transaction(self.id, payload)
            await COLLECTOR.record_action(
                self.id,
                "StartTransaction",
                {**payload, "transaction_id": transaction_id},
            )
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
        offered_subprotocols = list(websocket.scope.get("subprotocols") or [])
        selected_subprotocol = None
        for candidate in ("ocpp1.6", "ocpp1.5"):
            if candidate in offered_subprotocols:
                selected_subprotocol = candidate
                break

        await websocket.accept(subprotocol=selected_subprotocol)
        remote = websocket.client
        path = websocket.url.path if hasattr(websocket, "url") else f"/{cp_id}"
        LOGGER.info(
            "New charger connected: %s from %s subprotocol=%s",
            cp_id,
            remote,
            selected_subprotocol,
        )
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