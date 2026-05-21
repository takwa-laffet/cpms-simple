from pathlib import Path
from typing import Dict, Any
from .utils import load_json_safe, atomic_write
import json

META_FILE = Path("cpms_data") / "borne_meta.json"


def read_meta() -> Dict[str, Any]:
    return load_json_safe(META_FILE, {})


def write_meta(meta: Dict[str, Any]) -> None:
    atomic_write(META_FILE, json.dumps(meta, indent=2, ensure_ascii=False))


def sync_meta_from_boot(cp_id: str, boot_payload: Dict[str, Any]) -> None:
    meta = read_meta()
    entry = meta.get(cp_id, {})
    # merge some fields
    for k in ("serial_number", "firmware_version", "charge_point_model", "charge_point_vendor"):
        if boot_payload.get(k) and not entry.get(k):
            entry[k] = boot_payload.get(k)
    meta[cp_id] = entry
    write_meta(meta)


def get_serial(cp_id: str) -> str | None:
    meta = read_meta()
    entry = meta.get(cp_id, {})
    return entry.get("serial_number") or entry.get("serialNumber")


def enrich_cp_metadata(cp_id: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    """Placeholder for enrichment (geolocation, vendor DB).

    Returns enriched metadata dict.
    """
    # For now return unchanged
    return meta
