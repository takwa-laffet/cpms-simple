"""Charge point management utilities for CPMS backend."""
import json
from pathlib import Path

from app_config import (
    CHARGE_POINTS_FILE,
    BILLING_TARIFF_PER_KWH,
)


def read_charge_point_registry() -> list[dict]:
    users = read_json_file(CHARGE_POINTS_FILE, [])
    return users if isinstance(users, list) else []


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


# Import dependencies from other modules
from app_auth import utc_now_iso, read_json_file, hash_password  # noqa: E402