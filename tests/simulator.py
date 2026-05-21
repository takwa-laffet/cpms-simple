"""Simple simulator scaffold to drive end-to-end tests.

This module is a starting point for writing a websocket-based simulator
that performs BootNotification, StartTransaction, MeterValues and StopTransaction.
"""
import asyncio
import json
from typing import List


async def simulate_sequence(ws_uri: str, cp_id: str, sequence: List[dict], delay: float = 1.0):
    """Connect to a WebSocket and send JSON messages from sequence with delay."""
    try:
        import websockets
    except Exception:
        raise RuntimeError("websockets package required for simulator")

    async with websockets.connect(f"{ws_uri}/{cp_id}") as ws:
        for msg in sequence:
            await ws.send(json.dumps(msg))
            await asyncio.sleep(delay)


def example_sequence():
    return [
        {"action": "BootNotification", "payload": {"charge_point_model": "CP-1000", "charge_point_vendor": "ACME"}},
        {"action": "Heartbeat", "payload": {}},
    ]
