import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO)
LOGGER = logging.getLogger("auto_simulator")

try:
    import websockets
    from ocpp.v16 import call as ocpp_call
    OCPP_AVAILABLE = True
except ImportError:
    websockets = None
    ocpp_call = None
    OCPP_AVAILABLE = False

DATA_DIR = Path("cpms_data")
DATA_DIR.mkdir(exist_ok=True)
EVENTS_FILE = DATA_DIR / "events.jsonl"
BORNE_LOGS_DIR = DATA_DIR / "borne_logs"
BORNE_LOGS_DIR.mkdir(exist_ok=True)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_jsonl(file_path: Path, item: dict) -> None:
    with file_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")


def _append_borne_json(cp_id: str, item: dict) -> None:
    safe_cp_id = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in cp_id)
    cp_file = BORNE_LOGS_DIR / f"{safe_cp_id}.json"
    if cp_file.exists():
        try:
            existing = json.loads(cp_file.read_text(encoding="utf-8"))
            if not isinstance(existing, list):
                existing = []
        except Exception:
            existing = []
    else:
        existing = []
    existing.append(item)
    cp_file.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")


async def _send_ocpp_message(cp_id: str, action: str, payload: dict) -> None:
    event = {
        "event_type": "ocpp_message",
        "timestamp": utc_now_iso(),
        "cp_id": cp_id,
        "action": action,
        "payload": payload,
    }
    _append_jsonl(EVENTS_FILE, event)
    _append_borne_json(cp_id, event)


class AutoChargePointSimulator:
    def __init__(self, cp_id: str, ws_url: str):
        self.cp_id = cp_id
        self.ws_url = ws_url.rstrip("/")
        self.ws = None
        self.running = False
        self.heartbeat_interval = 30
        self.meter_start = 0
        self.transaction_id = None
        self.id_tag = f"SIM_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        self.connector_id = 1
        self._tasks = []

    async def connect(self):
        if not OCPP_AVAILABLE:
            LOGGER.warning(f"[{self.cp_id}] OCPP libraries not available")
            return False
        url = f"{self.ws_url}/{self.cp_id}"
        LOGGER.info(f"[{self.cp_id}] Connecting to {url}")
        try:
            self.ws = await websockets.connect(url, subprotocols=["ocpp1.6"])
            LOGGER.info(f"[{self.cp_id}] Connected")
            return True
        except Exception as e:
            LOGGER.error(f"[{self.cp_id}] Connection failed: {e}")
            return False

    async def send_boot_notification(self):
        payload = {"charge_point_model": "Auto-Sim", "charge_point_vendor": "CityOs", "firmware_version": "1.0"}
        await _send_ocpp_message(self.cp_id, "BootNotification", payload)
        LOGGER.info(f"[{self.cp_id}] BootNotification")

    async def send_heartbeat(self):
        while self.running:
            await _send_ocpp_message(self.cp_id, "Heartbeat", {})
            LOGGER.info(f"[{self.cp_id}] Heartbeat")
            await asyncio.sleep(self.heartbeat_interval)

    async def send_status_notification(self, status: str):
        await _send_ocpp_message(self.cp_id, "StatusNotification", {"connector_id": 1, "status": status})
        LOGGER.info(f"[{self.cp_id}] Status: {status}")

    async def start_transaction(self):
        await _send_ocpp_message(self.cp_id, "StartTransaction", {"id_tag": self.id_tag})
        LOGGER.info(f"[{self.cp_id}] StartTransaction")
        self.transaction_id = int(datetime.now().timestamp())
        return self.transaction_id

    async def send_meter_values(self, energy_wh: int):
        await _send_ocpp_message(self.cp_id, "MeterValues", {"connector_id": 1, "energy_wh": energy_wh})
        LOGGER.info(f"[{self.cp_id}] MeterValues: {energy_wh}Wh")

    async def stop_transaction(self):
        if self.transaction_id:
            await _send_ocpp_message(self.cp_id, "StopTransaction", {"transaction_id": self.transaction_id})
            LOGGER.info(f"[{self.cp_id}] StopTransaction")
            self.transaction_id = None

    async def run_charging_cycle(self):
        await self.send_status_notification("Preparing")
        await asyncio.sleep(1)
        await self.send_status_notification("Charging")
        await self.start_transaction()
        for i in range(3):
            if not self.running:
                break
            await self.send_meter_values((i + 1) * 1000)
            await asyncio.sleep(2)
        await self.stop_transaction()
        await self.send_status_notification("Available")

    async def run(self):
        self.running = True
        await self.connect()
        await self.send_boot_notification()
        asyncio.create_task(self.send_heartbeat())
        await self.send_status_notification("Available")
        while self.running:
            await asyncio.sleep(5)
            if self.running:
                await self.run_charging_cycle()

    def stop(self):
        self.running = False


simulation_state = {}


async def start_auto_simulator(cp_id: str, ws_url: str = "ws://localhost:5000"):
    global simulation_state
    if cp_id in simulation_state and simulation_state[cp_id].get("running"):
        return False
    sim = AutoChargePointSimulator(cp_id, ws_url)
    simulation_state[cp_id] = {"simulator": sim, "running": True, "start_time": utc_now_iso(), "ws_url": ws_url}
    asyncio.create_task(sim.run())
    return True


async def stop_auto_simulator(cp_id: str):
    global simulation_state
    if cp_id in simulation_state:
        simulation_state[cp_id]["simulator"].stop()
        del simulation_state[cp_id]

# Re-export simulation_state for use in app.py
__all__ = ["start_auto_simulator", "stop_auto_simulator", "simulation_state"]
