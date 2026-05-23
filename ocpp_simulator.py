import asyncio
import json
import logging
import sys
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urljoin

import websockets
from ocpp.v16 import ChargePoint as CP
from ocpp.v16 import call
from ocpp.v16 import call_result
from ocpp.v16.enums import (
    AuthorizationStatus,
    DataTransferStatus,
    RegistrationStatus,
    ResetType,
)

logging.basicConfig(level=logging.INFO)


class ChargePointSimulator(CP):
    def __init__(self, id, ws):
        super().__init__(id, ws)
        self.connector_id = 1
        self.id_tag = f"SIM_{uuid.uuid4().hex[:8].upper()}"
        self.meter_start = 0
        self.transaction_id = None

    async def send_boot_notification(self):
        """Send BootNotification to the central system."""
        request = call.BootNotificationPayload(
            charge_point_model="OCPP Simulator Model",
            charge_point_vendor="OCPP Simulator Vendor",
            firmware_version="1.0",
        )
        response = await self.call(request)
        if response.status == RegistrationStatus.accepted:
            logging.info(
                f"Connected to central system. Interval: {response.interval}"
            )
            return response.interval
        else:
            logging.error("BootNotification rejected")
            return None

    async def send_heartbeat(self):
        """Send Heartbeat to the central system."""
        request = call.HeartbeatPayload()
        response = await self.call(request)
        logging.info(f"Heartbeat response: {response.current_time}")

    async def send_status_notification(self, status):
        """Send StatusNotification for connector."""
        request = call.StatusNotificationPayload(
            connector_id=self.connector_id,
            error_code="NoError",
            status=status,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        await self.call(request)
        logging.info(f"Sent status notification: {status}")

    async def send_meter_values(self, energy_wh=0):
        """Send MeterValues with energy reading."""
        request = call.MeterValuesPayload(
            connector_id=self.connector_id,
            meter_value=[
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "sampled_value": [
                        {"value": str(energy_wh), "unit": "Wh"}
                    ],
                }
            ],
        )
        await self.call(request)
        logging.info(f"Sent meter values: {energy_wh} Wh")

    async def simulate_charging_session(self):
        """Simulate a simple charging session."""
        # Start transaction
        request = call.StartTransactionPayload(
            connector_id=self.connector_id,
            id_tag=self.id_tag,
            meter_start=self.meter_start,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        response = await self.call(request)
        self.transaction_id = response.transaction_id
        logging.info(
            f"Started transaction {self.transaction_id} with idTag {self.id_tag}"
        )

        # Simulate charging for a few seconds
        for i in range(5):
            await asyncio.sleep(5)
            energy = self.meter_start + (i + 1) * 1000  # Increase by 1kWh each step
            await self.send_meter_values(energy)

        # Stop transaction
        request = call.StopTransactionPayload(
            meter_stop=self.meter_start + 5000,  # 5 kWh total
            timestamp=datetime.now(timezone.utc).isoformat(),
            transaction_id=self.transaction_id,
        )
        response = await self.call(request)
        logging.info(f"Stopped transaction {self.transaction_id}")
        self.transaction_id = None

    async def start(self):
        """Start the simulator: boot, heartbeat, and simulate."""
        interval = await self.send_boot_notification()
        if interval is None:
            return

        # Send initial status
        await self.send_status_notification("Available")

        # Start heartbeat task
        async def heartbeat_task():
            while True:
                await self.send_heartbeat()
                await asyncio.sleep(interval)

        heartbeat = asyncio.create_task(heartbeat_task())

        # Wait a bit then simulate a charging session
        await asyncio.sleep(10)
        await self.send_status_notification("Preparing")
        await asyncio.sleep(2)
        await self.send_status_notification("Charging")
        await self.simulate_charging_session()
        await self.send_status_notification("Finishing")
        await asyncio.sleep(2)
        await self.send_status_notification("Available")

        # Keep sending heartbeats until interrupted
        try:
            while True:
                await asyncio.sleep(3600)  # Sleep long time
        except asyncio.CancelledError:
            pass
        finally:
            heartbeat.cancel()


async def main(cp_id, ws_url):
    """Main function to run the simulator."""
    # Construct the WebSocket URL for this charge point
    url = urljoin(ws_url, cp_id)
    logging.info(f"Connecting to {url}")

    try:
        async with websockets.connect(
            url, subprotocols=["ocpp1.6"]
        ) as ws:
            cp = ChargePointSimulator(cp_id, ws)
            await cp.start()
    except asyncio.CancelledError:
        logging.info("Simulator task cancelled")
        raise
    except Exception as e:
        logging.error(f"Connection failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python ocpp_simulator.py <cp_id> <ws_url>")
        print("Example: python ocpp_simulator.py CP_SIM_001 ws://localhost:5000")
        sys.exit(1)

    cp_id = sys.argv[1]
    ws_url = sys.argv[2]

    asyncio.run(main(cp_id, ws_url))