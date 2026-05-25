import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

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


async def _send_ocpp_message(cp_id: str, action: str, payload: dict, collector=None) -> None:
    event = {
        "event_type": "ocpp_message",
        "timestamp": utc_now_iso(),
        "cp_id": cp_id,
        "action": action,
        "payload": payload,
    }
    _append_jsonl(EVENTS_FILE, event)
    _append_borne_json(cp_id, event)
    if cp_id in simulation_state:
        simulation_state[cp_id].setdefault("messages", []).append(event)
    # Send to backend collector if available
    if collector is not None:
        await collector.record_action(cp_id, action, payload)


class AutoChargePointSimulator:
    def __init__(self, cp_id: str, ws_url: str, collector=None):
        self.cp_id = cp_id
        self.ws_url = ws_url.rstrip("/")
        self.ws = None
        self.running = False
        self.started_at = None
        self.heartbeat_interval = 10  # Changed from 30 to 10 seconds
        self.meter_start = 0
        self.transaction_id = None
        self.id_tag = f"SIM_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        self.connector_id = 1
        self._tasks = []
        self.collector = collector
        # Enhanced device information
        self.device_info = {
            "charge_point_model": "CP_TEST_001",
            "charge_point_vendor": "TestVendor",
            "firmware_version": "1.0.0",
            "serial_number": f"{self.cp_id}_SN_001",
            "ip_address": "192.168.1.100",
            "iccid": "8930000000000000000",
            "imsi": "208000000000000",
            "commissioning_date": "2026-01-15",
            "error_code": "NoError",
            "assigned_user": "operator@rback.local",
            "meter_type": "Electronic",
            "meter_serial_number": f"{self.cp_id}_MSN_001",
        }

    def _format_uptime(self) -> str:
        if self.started_at is None:
            return "0 days 00:00:00"
        elapsed = datetime.now(timezone.utc) - self.started_at
        total_seconds = int(elapsed.total_seconds())
        days, remainder = divmod(total_seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{days} days {hours:02d}:{minutes:02d}:{seconds:02d}"

    def _build_fake_metadata(self) -> dict:
        return {
            "manufacturer": self.device_info["charge_point_vendor"],
            "model": self.device_info["charge_point_model"],
            "serialNumber": self.device_info["serial_number"],
            "firmwareVersion": self.device_info["firmware_version"],
            "ipAddress": self.device_info["ip_address"],
            "iccid": self.device_info["iccid"],
            "imsi": self.device_info["imsi"],
            "commissioningDate": self.device_info["commissioning_date"],
            "assigned_user": self.device_info["assigned_user"],
            "uptime": self._format_uptime(),
        }

    async def connect(self):
        if not OCPP_AVAILABLE:
            LOGGER.warning(f"[{self.cp_id}] OCPP libraries not available")
            return False
        url = f"{self.ws_url}/{self.cp_id}"
        LOGGER.info(f"[{self.cp_id}] Connecting to {url}")
        try:
            self.ws = await websockets.connect(url, subprotocols=["ocpp1.6"])
            LOGGER.info(f"[{self.cp_id}] Connected")
            # Register connection with collector
            if self.collector is not None:
                await self.collector.register_connection(self.cp_id, self.ws.remote_address, self.ws.path)
            return True
        except Exception as e:
            LOGGER.error(f"[{self.cp_id}] Connection failed: {e}")
            return False

    async def send_boot_notification(self):
        payload = {
            "charge_point_model": self.device_info["charge_point_model"],
            "charge_point_vendor": self.device_info["charge_point_vendor"],
            "firmware_version": self.device_info["firmware_version"],
            "serial_number": self.device_info["serial_number"],
            "ipAddress": self.device_info["ip_address"],
            "iccid": self.device_info["iccid"],
            "imsi": self.device_info["imsi"],
            "commissioningDate": self.device_info["commissioning_date"],
            "error_code": self.device_info["error_code"],
            "errorCode": self.device_info["error_code"],
            "assigned_user": self.device_info["assigned_user"],
            "uptime": self._format_uptime(),
        }
        await _send_ocpp_message(self.cp_id, "BootNotification", payload)
        # Update boot notification in collector
        if self.collector is not None:
            await self.collector.update_boot_notification(self.cp_id, payload)
        LOGGER.info(f"[{self.cp_id}] BootNotification")

    async def send_heartbeat(self):
        while self.running:
            if self.collector is not None:
                await self.collector.record_action(self.cp_id, "MetadataUpdate", {"metadata": self._build_fake_metadata()})
            await _send_ocpp_message(self.cp_id, "Heartbeat", {})
            LOGGER.info(f"[{self.cp_id}] Heartbeat")
            await asyncio.sleep(self.heartbeat_interval)

    async def send_status_notification(self, status: str):
        await _send_ocpp_message(self.cp_id, "StatusNotification", {
            "connector_id": 1,
            "status": status,
            "error_code": self.device_info["error_code"],
            "errorCode": self.device_info["error_code"],
        })
        # Update status in collector
        if self.collector is not None:
            await self.collector.update_status(self.cp_id, {"status": status, "error_code": self.device_info["error_code"]})
        LOGGER.info(f"[{self.cp_id}] Status: {status}")

    async def start_transaction(self):
        await _send_ocpp_message(self.cp_id, "StartTransaction", {"id_tag": self.id_tag})
        # Start transaction in collector
        if self.collector is not None:
            tx_id = await self.collector.start_transaction(self.cp_id, {"id_tag": self.id_tag})
            self.transaction_id = tx_id
        else:
            self.transaction_id = int(datetime.now().timestamp())
        LOGGER.info(f"[{self.cp_id}] StartTransaction")
        return self.transaction_id

    async def send_meter_values(self, energy_wh: int):
        await _send_ocpp_message(self.cp_id, "MeterValues", {
            "connector_id": 1,
            "energy_wh": energy_wh,
            "last_meter_value": energy_wh,
        })
        if self.collector is not None:
            await self.collector.update_meter_values(self.cp_id, {"energy_wh": energy_wh, "last_meter_value": energy_wh})
        LOGGER.info(f"[{self.cp_id}] MeterValues: {energy_wh}Wh")

    async def stop_transaction(self):
        if self.transaction_id:
            await _send_ocpp_message(self.cp_id, "StopTransaction", {"transaction_id": self.transaction_id})
            # Stop transaction in collector
            if self.collector is not None:
                await self.collector.stop_transaction(self.cp_id, {"transaction_id": self.transaction_id})
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
        self.started_at = datetime.now(timezone.utc)
        await self.connect()
        await self.send_boot_notification()
        # Register charge point and set metadata for complete General Info display
        if self.collector is not None:
            # Register the charge point with basic info
            await self.collector.upsert_charge_point_record({
                "cp_id": self.cp_id,
                "label": self.device_info["charge_point_model"],
                "vendor": self.device_info["charge_point_vendor"],
                "model": self.device_info["charge_point_model"],
                "serial_number": self.device_info.get("serial_number", f"{self.cp_id}_SN_001"),
                "firmware_version": self.device_info["firmware_version"],
                "site": "Test Site",
                "connector_count": 1,
                "tariff_per_kwh": 0.35,  # Default tariff
            })
            # Set additional metadata
            await self.collector.record_action("SYSTEM", "MetadataUpdate", {
                "cp_id": self.cp_id,
                "metadata": self._build_fake_metadata(),
            })
        LOGGER.info(f"[{self.cp_id}] Registered charge point and set metadata")
        # Start with Preparing status, then change to Available after 3 minutes
        await self.send_status_notification("Preparing")
        await self.send_meter_values(0)
        LOGGER.info(f"[{self.cp_id}] Starting in Preparing state, will go Available in 3 minutes")
        await asyncio.sleep(180)  # Wait 3 minutes (180 seconds)
        if self.running:  # Check if still running after sleep
            await self.send_status_notification("Available")
        asyncio.create_task(self.send_heartbeat())
        while self.running:
            await asyncio.sleep(5)
            if self.running:
                await self.run_charging_cycle()

    def stop(self):
        self.running = False
        # Notify collector of disconnection
        if self.collector is not None and self.ws is not None:
            # Create a synchronous task to notify collector (since we're in a sync method)
            import asyncio
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.create_task(self.collector.register_disconnection(self.cp_id, "simulator_stopped"))
            except RuntimeError:
                # No event loop, skip async notification
                pass


simulation_state = {}


async def start_auto_simulator(cp_id: str, ws_url: str = "ws://localhost:5000", collector=None):
    global simulation_state
    if cp_id in simulation_state and simulation_state[cp_id].get("running"):
        return False
    sim = AutoChargePointSimulator(cp_id, ws_url, collector)
    simulation_state[cp_id] = {"simulator": sim, "running": True, "start_time": utc_now_iso(), "ws_url": ws_url, "messages": []}
    asyncio.create_task(sim.run())
    return True


async def stop_auto_simulator(cp_id: str):
    global simulation_state
    if cp_id in simulation_state:
        simulation_state[cp_id]["simulator"].stop()
        del simulation_state[cp_id]

# Re-export simulation_state for use in app.py
__all__ = ["start_auto_simulator", "stop_auto_simulator", "simulation_state"]
