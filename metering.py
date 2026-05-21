from typing import List, Dict, Any
from datetime import datetime
import csv
from io import StringIO


def parse_energy_measurand(sampled: Dict[str, Any]) -> (float, str):
    """Detect energy value and unit from a sampled_value dict.

    Returns (value, unit) or (None, None).
    """
    if sampled is None:
        return (None, None)
    val = sampled.get("value")
    unit = sampled.get("unit") or sampled.get("measurand")
    try:
        return (float(val), unit)
    except Exception:
        return (None, unit)


def rebuild_sessions_from_events(events: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Lightweight session reconstruction from events list.

    Similar to build_sessions in app.py but provided as an importable helper.
    """
    sessions = {}
    for ev in events:
        if ev.get("event_type") != "ocpp_message":
            continue
        cp_id = ev.get("cp_id")
        action = ev.get("action")
        payload = ev.get("payload") or {}
        sessions.setdefault(cp_id, [])
        if action == "StartTransaction":
            tx = payload.get("transaction_id") or payload.get("transactionId")
            sessions[cp_id].append({
                "session_id": tx,
                "start": ev.get("timestamp"),
                "connector_id": payload.get("connector_id"),
                "id_tag": payload.get("id_tag"),
                "meter_start": payload.get("meter_start"),
                "events": [ev],
            })
        elif action == "StopTransaction":
            tx = payload.get("transaction_id") or payload.get("transactionId")
            # attach to last session with same tx if exists
            attached = False
            for s in reversed(sessions[cp_id]):
                if s.get("session_id") == tx:
                    s["end"] = ev.get("timestamp")
                    s.setdefault("events", []).append(ev)
                    attached = True
                    break
            if not attached:
                sessions[cp_id].append({
                    "session_id": tx,
                    "start": None,
                    "end": ev.get("timestamp"),
                    "meter_stop": payload.get("meter_stop"),
                    "events": [ev],
                })

    return sessions


def export_session_csv(session: Dict[str, Any], charge_curve: List[Dict[str, Any]]) -> str:
    """Export session metadata + per-minute charge_curve to CSV string."""
    out = StringIO()
    writer = csv.writer(out)
    writer.writerow(["session_id", session.get("session_id")])
    writer.writerow(["start", session.get("start")])
    writer.writerow(["end", session.get("end")])
    writer.writerow(["duration_s", session.get("duration_s")])
    writer.writerow([])
    writer.writerow(["minute_start", "avg_power_w", "energy_kwh"])
    for b in charge_curve:
        writer.writerow([b.get("minute_start"), b.get("avg_power_w"), b.get("energy_kwh")])
    return out.getvalue()
