# Remote Control Test Guide

This guide lists the tests you should run to remotely control the borne from `cpms-simple`.

## Goal

Verify that the CPMS can:

- start charging remotely
- stop charging remotely
- reboot the borne
- unlock a connector
- recover after errors
- expose the borne serial number and live state in `/api/cp`

## Endpoints to test

- `POST /api/cp/{cp_id}/remote_start`
- `POST /api/cp/{cp_id}/remote_stop`
- `POST /api/cp/{cp_id}/remote_reboot`
- `POST /api/cp/{cp_id}/unlock_connector`
- `POST /api/cp/{cp_id}/force_start`
- `POST /api/cp/{cp_id}/force_stop`
- `GET /api/cp`
- `GET /api/cp?cp_id={cp_id}`
- `GET /api/cp/{cp_id}/meta`
- `POST /api/cp/{cp_id}/meta`

## Pre-checks

Before testing remote control, confirm:

- the borne is connected
- `GET /api/cp` shows `active_connections > 0`
- `last_seen[cp_id]` is updating
- `status_by_cp[cp_id]` is present
- the correct `cp_id` is used, for example `CP001`

Example:

```text
/api/cp?cp_id=CP001
```

## 1. Test remote start charging

### Test 1.1 - Remote start with connector id

```powershell
curl.exe -X POST "https://cpms-simple.onrender.com/api/cp/CP001/remote_start?connector_id=1"
```

Expected:

- HTTP 200
- response contains `result: sent`
- response contains `response_status: Accepted` or similar
- borne changes from `Available` or `Preparing` to `Charging`
- `StartTransaction` appears in `events`
- `open_transactions_by_cp.CP001` is filled

### Test 1.2 - Remote start without connector id

```powershell
curl.exe -X POST "https://cpms-simple.onrender.com/api/cp/CP001/remote_start"
```

Expected:

- charger still accepts the remote start if it ignores `connector_id`
- `RemoteStartTransaction` appears in `events`
- `StartTransaction` appears after the start command

### Test 1.3 - Verify the charge state after remote start

Check:

```text
/api/cp?cp_id=CP001
```

Look for:

- `status_by_cp.CP001.status = Charging`
- `open_transactions_by_cp.CP001`
- `last_meter_values_by_cp.CP001` after the meter starts

## 2. Test remote stop charging

### Test 2.1 - Remote stop the active session

Get the current transaction id from `/api/cp`, then stop it:

```powershell
curl.exe -X POST "https://cpms-simple.onrender.com/api/cp/CP001/remote_stop?transaction_id=4"
```

Expected:

- HTTP 200
- response contains `result: sent`
- borne transitions to `Finishing` or `Available`
- `StopTransaction` appears in `events`
- `open_transactions_by_cp.CP001` becomes empty

### Test 2.2 - Force stop server-side only

```powershell
curl.exe -X POST "https://cpms-simple.onrender.com/api/cp/CP001/force_stop?transaction_id=4"
```

Expected:

- session closes in CPMS
- no OCPP message is required from the borne

## 3. Test reboot

### Test 3.1 - Soft reboot

```powershell
curl.exe -X POST "https://cpms-simple.onrender.com/api/cp/CP001/remote_reboot?reset_type=Soft"
```

Expected:

- HTTP 200
- `Reset` command is sent
- borne disconnects and reconnects
- `connection_closed` then `connection_opened` appear in logs
- `BootNotification` appears again after reboot

### Test 3.2 - Hard reboot

```powershell
curl.exe -X POST "https://cpms-simple.onrender.com/api/cp/CP001/remote_reboot?reset_type=Hard"
```

Expected:

- borne restarts more aggressively if supported by hardware
- may disconnect longer than soft reboot

## 4. Test unlock connector

```powershell
curl.exe -X POST "https://cpms-simple.onrender.com/api/cp/CP001/unlock_connector?connector_id=1"
```

Expected:

- connector unlock command is sent
- if the connector was blocked, it should become usable again
- status may change in `StatusNotification`

## 5. Test serial number capture

### Test 5.1 - From BootNotification

Reconnect the borne and check:

```text
/api/cp
```

Expected fields:

- `boot_notifications_by_cp.CP001`
- `serial_number_by_cp.CP001`
- `general_info_by_cp.CP001.serial_number`

### Test 5.2 - Manual metadata fallback

If the borne does not send the serial number, set it manually:

```powershell
Invoke-RestMethod -Method Post -Uri "https://cpms-simple.onrender.com/api/cp/CP001/meta" -ContentType "application/json" -Body '{"serialNumber":"SN-0001"}'
```

Then verify:

```text
/api/cp?cp_id=CP001
```

## 6. Test live telemetry during remote control

During charging, confirm the following appear:

- `Heartbeat`
- `StatusNotification`
- `MeterValues`
- `StartTransaction`
- `StopTransaction`

Expected telemetry in `/api/cp`:

- `status_by_cp`
- `last_meter_values_by_cp`
- `sessions`
- `energy`
- `message_count_by_action`

## 7. Error and recovery tests

### Test 7.1 - Remote start while disconnected

If the borne is offline, remote start should return an error and not crash the API.

### Test 7.2 - Invalid transaction id on remote stop

Try stopping a transaction id that does not exist.

Expected:

- API should return an error or no-op safely
- CPMS should remain stable

### Test 7.3 - Charger rejects the remote start

If the charger rejects the request with `connector_id`, the API retries once without it.

Expected:

- response shows `fallback_used: true` when needed
- if still rejected, the API returns an error status

## 8. Full remote control checklist

- [ ] borne connected
- [ ] `BootNotification` received
- [ ] serial number visible
- [ ] remote start accepted
- [ ] charging state becomes `Charging`
- [ ] meter values arrive
- [ ] remote stop accepted
- [ ] session closes cleanly
- [ ] soft reboot accepted
- [ ] borne reconnects after reboot
- [ ] unlock connector accepted
- [ ] API stable when borne is offline

## 9. Recommended order for real-world testing

1. check `/api/cp`
2. verify serial number
3. try `remote_start`
4. confirm `Charging`
5. read `MeterValues`
6. try `remote_stop`
7. try `remote_reboot`
8. try `unlock_connector`
9. verify logs and session history

## 10. Notes

- `force_start` is only server-side tracking.
- `remote_start` is the real charger command.
- `remote_reboot` is the command for rebooting the borne.
- Some bornes require a vehicle plugged in and may still reject remote start if they are locked or in a protection state.

## 11. Useful URLs

- `https://cpms-simple.onrender.com/api/cp`
- `https://cpms-simple.onrender.com/api/cp?cp_id=CP001`
- `https://cpms-simple.onrender.com/api/cp/CP001/meta`
