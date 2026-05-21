from pathlib import Path
import json
from datetime import datetime

AUDIT_FILE = Path("cpms_data") / "audit.jsonl"


def log_control_action(user: str | None, cp_id: str, action: str, payload: dict) -> None:
    entry = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "user": user,
        "cp_id": cp_id,
        "action": action,
        "payload": payload,
    }
    with AUDIT_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
