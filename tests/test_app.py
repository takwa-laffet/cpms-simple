import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import app as app_module
from app import (
    app,
    aggregate_session_meter_values,
    build_sessions,
    normalize_boot_payload,
    parse_meter_payload,
)


AUTH_EMAIL = "mvp@prelabel.tn"
AUTH_PASSWORD = "Cpms_Secure#48Tz@2026"


def login_test_client(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={"email": AUTH_EMAIL, "password": AUTH_PASSWORD},
    )
    assert response.status_code == 200, response.text


class AppSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_root_serves_frontend(self) -> None:
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("CityOs Dashboard", response.text)
        self.assertIn('id="login-form"', response.text)
        self.assertIn('id="login-password"', response.text)

    def test_health_endpoint(self) -> None:
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertEqual(response.json()["api"], "/api/cp")

    def test_login_endpoint_sets_session_cookie(self) -> None:
        response = self.client.post(
            "/api/auth/login",
            json={"email": AUTH_EMAIL, "password": AUTH_PASSWORD},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertIn("cpms_auth=", response.headers.get("set-cookie", ""))

        session_response = self.client.get("/api/auth/me")
        self.assertEqual(session_response.status_code, 200)
        self.assertTrue(session_response.json()["authenticated"])

    def test_login_form_post_redirects_when_not_json(self) -> None:
        response = self.client.post(
            "/api/auth/login",
            data={"email": AUTH_EMAIL, "password": AUTH_PASSWORD},
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers.get("location"), "/")
        self.assertIn("cpms_auth=", response.headers.get("set-cookie", ""))

    def test_frontend_script_is_served(self) -> None:
        response = self.client.get("/frontend/app.js")

        self.assertEqual(response.status_code, 200)
        self.assertIn("const state", response.text)

    def test_api_cp_returns_snapshot(self) -> None:
        login_test_client(self.client)
        response = self.client.get("/api/cp")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("snapshot", payload)
        self.assertIn("events", payload)
        self.assertIn("energy", payload)

    def test_api_cp_requires_login(self) -> None:
        response = self.client.get("/api/cp")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Not authenticated")

    def test_normalize_boot_payload_aliases(self) -> None:
        payload = normalize_boot_payload(
            {
                "manufacturer": "ACME",
                "model": "X2",
                "serialNumber": "SN-0001",
                "firmware": "1.0.12",
            }
        )

        self.assertEqual(payload["charge_point_vendor"], "ACME")
        self.assertEqual(payload["charge_point_model"], "X2")
        self.assertEqual(payload["serial_number"], "SN-0001")
        self.assertEqual(payload["firmware_version"], "1.0.12")


class _FakeRequest:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        for key, value in kwargs.items():
            setattr(self, key, value)


class _FakeCallNamespace:
    class RemoteStartTransaction(_FakeRequest):
        pass

    class RemoteStopTransaction(_FakeRequest):
        pass

    class Reset(_FakeRequest):
        pass

    class UnlockConnector(_FakeRequest):
        pass


class _FakeChargePoint:
    def __init__(self) -> None:
        self.requests = []

    async def call(self, request):
        self.requests.append(request)

        if isinstance(request, _FakeCallNamespace.RemoteStartTransaction):
            if "connector_id" in request.kwargs:
                return SimpleNamespace(status="Rejected")
            return SimpleNamespace(status="Accepted")

        return SimpleNamespace(status="Accepted")


class RemoteCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)
        self.fake_cp = _FakeChargePoint()

        self.call_patch = patch.object(app_module, "call", _FakeCallNamespace)
        self.get_cp_patch = patch.object(app_module.COLLECTOR, "get_charge_point", AsyncMock(return_value=self.fake_cp))
        self.record_action_patch = patch.object(app_module.COLLECTOR, "record_action", AsyncMock())

        self.call_patch.start()
        self.get_cp_patch.start()
        self.record_action_patch.start()

        login_test_client(self.client)

        self.addCleanup(app_module.COLLECTOR.open_transactions_by_cp.pop, "CP001", None)

        self.addCleanup(self.call_patch.stop)
        self.addCleanup(self.get_cp_patch.stop)
        self.addCleanup(self.record_action_patch.stop)

    def test_remote_start_retries_without_connector_id(self) -> None:
        response = self.client.post("/api/cp/CP001/remote_start?connector_id=1")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["fallback_used"])
        self.assertIsNone(body["connector_id_used"])
        self.assertEqual(body["id_tag_used"], "REMOTE_START")
        self.assertEqual(len(self.fake_cp.requests), 2)
        self.assertIn("connector_id", self.fake_cp.requests[0].kwargs)
        self.assertNotIn("connector_id", self.fake_cp.requests[1].kwargs)

    def test_force_start_prefers_remote_start_when_connected(self) -> None:
        with patch.object(app_module.COLLECTOR, "start_transaction", AsyncMock()) as start_transaction_patch:
            response = self.client.post("/api/cp/CP001/force_start?connector_id=1&meter_start=12")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["result"], "sent")
        self.assertEqual(len(self.fake_cp.requests), 2)
        start_transaction_patch.assert_not_called()

    def test_remote_stop_sends_transaction_id(self) -> None:
        response = self.client.post("/api/cp/CP001/remote_stop?transaction_id=99")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.fake_cp.requests), 1)
        self.assertEqual(self.fake_cp.requests[0].transaction_id, 99)

    def test_remote_stop_uses_open_transaction_when_missing(self) -> None:
        app_module.COLLECTOR.open_transactions_by_cp["CP001"] = {
            "transaction_id": 42,
            "opened_at": "2026-05-22T10:00:00+00:00",
        }

        response = self.client.post("/api/cp/CP001/remote_stop")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.fake_cp.requests), 1)
        self.assertEqual(self.fake_cp.requests[0].transaction_id, 42)
        self.assertEqual(response.json()["transaction_id"], 42)

    def test_remote_reboot_uses_requested_reset_type(self) -> None:
        response = self.client.post("/api/cp/CP001/remote_reboot?reset_type=Hard")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.fake_cp.requests), 1)
        self.assertEqual(self.fake_cp.requests[0].type_, app_module.ResetType.hard)

    def test_unlock_connector_sends_connector_id(self) -> None:
        response = self.client.post("/api/cp/CP001/unlock_connector?connector_id=7")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.fake_cp.requests), 1)
        self.assertEqual(self.fake_cp.requests[0].connector_id, 7)


class SessionAndEnergyTests(unittest.TestCase):
    def test_build_sessions_uses_transaction_ids_and_timezone_safe_duration(self) -> None:
        events = [
            {
                "event_type": "ocpp_message",
                "cp_id": "CP001",
                "action": "StartTransaction",
                "timestamp": "2026-05-21T10:00:00+00:00",
                "payload": {
                    "transaction_id": 12,
                    "connector_id": 1,
                    "id_tag": "ABC123",
                    "meter_start": 5000,
                },
            },
            {
                "event_type": "ocpp_message",
                "cp_id": "CP001",
                "action": "StopTransaction",
                "timestamp": "2026-05-21T10:30:00+00:00",
                "payload": {
                    "transaction_id": 12,
                    "meter_stop": 9000,
                },
            },
        ]
        borne_logs = {
            "CP001": [
                {
                    "event_type": "ocpp_message",
                    "action": "MeterValues",
                    "payload": {
                        "transaction_id": 12,
                        "meter_value": [
                            {
                                "timestamp": "2026-05-21T10:05:00+00:00",
                                "sampled_value": [
                                    {
                                        "measurand": "Energy.Active.Import.Register",
                                        "value": "5.0",
                                        "unit": "kWh",
                                    },
                                    {
                                        "measurand": "Power.Active.Import",
                                        "value": "7000",
                                        "unit": "W",
                                    },
                                ],
                            }
                        ],
                    },
                }
            ]
        }

        sessions = build_sessions(events, borne_logs)
        self.assertIn("CP001", sessions)
        self.assertEqual(len(sessions["CP001"]), 1)

        session = sessions["CP001"][0]
        self.assertEqual(session["session_id"], 12)
        self.assertEqual(session["duration_s"], 1800.0)

        readings = parse_meter_payload(borne_logs["CP001"][0]["payload"])
        self.assertEqual(readings[0]["energy_wh"], 5000.0)
        self.assertEqual(readings[0]["power_w"], 7000.0)

        enriched = aggregate_session_meter_values(session, borne_logs["CP001"])
        self.assertEqual(enriched["kpis"]["total_kwh"], 4.0)
        self.assertEqual(enriched["kpis"]["peak_kw"], 7.0)
        self.assertGreater(len(enriched["charge_curve"]), 0)


class _FakeChargePointWebSocket:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    async def start(self) -> None:
        return None


class WebSocketHandshakeTests(unittest.TestCase):
    def test_charge_point_websocket_accepts_ocpp_subprotocol(self) -> None:
        with patch.object(app_module, "ChargePoint", _FakeChargePointWebSocket), patch.object(
            app_module.COLLECTOR,
            "register_connection",
            AsyncMock(),
        ), patch.object(
            app_module.COLLECTOR,
            "register_charge_point",
            AsyncMock(),
        ), patch.object(
            app_module.COLLECTOR,
            "unregister_charge_point",
            AsyncMock(),
        ), patch.object(
            app_module.COLLECTOR,
            "register_disconnection",
            AsyncMock(),
        ):
            with TestClient(app).websocket_connect("/CP001", subprotocols=["ocpp1.6"] ) as websocket:
                self.assertEqual(websocket.accepted_subprotocol, "ocpp1.6")


if __name__ == "__main__":
    unittest.main()