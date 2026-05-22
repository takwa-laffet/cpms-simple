const state = {
  data: null,
  selectedCpId: new URLSearchParams(window.location.search).get('cp_id') || null,
  refreshTimer: null,
  isAuthenticated: false,
  commandsWired: false,
};

const els = {
  loginScreen: document.getElementById('login-screen'),
  appShell: document.getElementById('app-shell'),
  loginForm: document.getElementById('login-form'),
  loginEmail: document.getElementById('login-email'),
  loginPassword: document.getElementById('login-password'),
  loginError: document.getElementById('login-error'),
  logoutBtn: document.getElementById('logout-btn'),
  refreshBtn: document.getElementById('refresh-btn'),
  globalStatus: document.getElementById('global-status'),
  globalStatusText: document.getElementById('global-status-text'),
  selectedCpLabel: document.getElementById('selected-cp-label'),
  fleetCount: document.getElementById('fleet-count'),
  cpList: document.getElementById('cp-list'),
  detailTitle: document.getElementById('detail-title'),
  detailStatus: document.getElementById('detail-status'),
  generalInfo: document.getElementById('general-info'),
  liveState: document.getElementById('live-state'),
  sessionList: document.getElementById('session-list'),
  eventList: document.getElementById('event-list'),
  toast: document.getElementById('toast'),
  metricActiveConnections: document.getElementById('metric-active-connections'),
  metricTotalMessages: document.getElementById('metric-total-messages'),
  metricTotalEnergy: document.getElementById('metric-total-energy'),
  metricOpenTransactions: document.getElementById('metric-open-transactions'),
};

const templates = {
  cpItem: document.getElementById('cp-item-template'),
  timelineItem: document.getElementById('timeline-item-template'),
};

function showToast(message, type = 'success') {
  els.toast.textContent = message;
  els.toast.className = `toast show ${type}`;
  window.clearTimeout(showToast._timer);
  showToast._timer = window.setTimeout(() => {
    els.toast.className = 'toast';
  }, 3200);
}

function formatValue(value) {
  if (value === null || value === undefined || value === '') {
    return '—';
  }

  if (typeof value === 'object') {
    return JSON.stringify(value, null, 2);
  }

  return String(value);
}

function updateAuthView() {
  const loggedIn = state.isAuthenticated;
  els.loginScreen.hidden = loggedIn;
  els.appShell.hidden = !loggedIn;
  document.body.classList.toggle('auth-locked', !loggedIn);

  if (!loggedIn) {
    els.loginError.textContent = '';
    els.loginPassword.value = '';
    els.loginPassword.focus();
  }
}

function setAuthenticated(isAuthenticated) {
  state.isAuthenticated = isAuthenticated;
  updateAuthView();
}

function handleSessionExpired(message = 'Session expired. Please sign in again.') {
  stopAutoRefresh();
  state.data = null;
  setAuthenticated(false);
  showToast(message, 'error');
}

function stopAutoRefresh() {
  if (state.refreshTimer) {
    window.clearInterval(state.refreshTimer);
    state.refreshTimer = null;
  }
}

async function startDashboard() {
  if (!state.commandsWired) {
    wireCommands();
    state.commandsWired = true;
  }

  els.refreshBtn.disabled = false;
  try {
    await loadData();
  } catch (error) {
    if (error.message === 'Unauthorized') {
      handleSessionExpired();
      return;
    }

    els.globalStatus.className = 'status-dot status-danger';
    els.globalStatusText.textContent = 'Unable to load backend data';
    showToast(error.message, 'error');
  }

  stopAutoRefresh();
  state.refreshTimer = window.setInterval(async () => {
    try {
      await loadData();
    } catch (error) {
      if (error.message === 'Unauthorized') {
        handleSessionExpired();
        return;
      }

      console.error(error);
    }
  }, 10000);
}

async function checkSession() {
  const response = await fetch('/api/auth/me');
  if (!response.ok) {
    return false;
  }

  const payload = await response.json();
  return payload?.authenticated === true;
}

async function signIn(email, password) {
  const response = await fetch('/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
    body: JSON.stringify({ email, password }),
  });

  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.detail || 'Invalid email or password.');
  }

  setAuthenticated(true);
  return payload;
}

async function signOut() {
  await fetch('/api/auth/logout', { method: 'POST' });
  stopAutoRefresh();
  state.data = null;
  setAuthenticated(false);
}

function toTitle(label) {
  return label
    .replace(/_/g, ' ')
    .replace(/([a-z])([A-Z])/g, '$1 $2')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/^./, (char) => char.toUpperCase());
}

function setQueryParam(cpId) {
  const url = new URL(window.location.href);
  if (cpId) {
    url.searchParams.set('cp_id', cpId);
  } else {
    url.searchParams.delete('cp_id');
  }
  window.history.replaceState({}, '', url);
}

function extractChargePointIds(data) {
  const ids = new Set();
  const sources = [
    data?.general_info_by_cp,
    data?.snapshot?.status_by_cp,
    data?.snapshot?.last_seen,
    data?.snapshot?.message_count_by_cp,
    data?.snapshot?.open_transactions_by_cp,
    data?.sessions,
  ];

  sources.forEach((source) => {
    if (!source) return;
    Object.keys(source).forEach((key) => ids.add(key));
  });

  return [...ids].sort((a, b) => a.localeCompare(b));
}

function getSelectedChargePointId(data) {
  const ids = extractChargePointIds(data);
  if (state.selectedCpId && ids.includes(state.selectedCpId)) {
    return state.selectedCpId;
  }
  return ids[0] || null;
}

function updateGlobalStatus(data) {
  const activeConnections = data?.snapshot?.metrics?.active_connections ?? 0;
  const totalConnections = data?.snapshot?.metrics?.total_connections ?? 0;
  const totalMessages = data?.snapshot?.metrics?.total_messages ?? 0;
  const totalEnergy = data?.energy?.total_kwh ?? 0;
  const openTransactions = Object.keys(data?.snapshot?.open_transactions_by_cp || {}).length;

  els.metricActiveConnections.textContent = String(activeConnections);
  els.metricTotalMessages.textContent = String(totalMessages);
  els.metricTotalEnergy.textContent = `${Number(totalEnergy).toFixed(2)} kWh`;
  els.metricOpenTransactions.textContent = String(openTransactions);

  const hasData = totalConnections > 0 || totalMessages > 0 || Object.keys(data?.events || {}).length > 0;
  els.globalStatus.className = `status-dot ${activeConnections > 0 ? 'status-connected' : hasData ? 'status-warning' : 'status-unknown'}`;
  els.globalStatusText.textContent = activeConnections > 0
    ? `${activeConnections} active connection${activeConnections === 1 ? '' : 's'}`
    : 'No active chargers connected right now';
}

function renderCpList(data, selectedCpId) {
  const ids = extractChargePointIds(data);
  els.fleetCount.textContent = `${ids.length} CP${ids.length === 1 ? '' : 's'}`;
  els.cpList.innerHTML = '';

  if (!ids.length) {
    const empty = document.createElement('div');
    empty.className = 'timeline-item';
    empty.innerHTML = '<p class="timeline-body">No charge points have connected yet.</p>';
    els.cpList.appendChild(empty);
    return;
  }

  ids.forEach((cpId) => {
    const template = templates.cpItem.content.cloneNode(true);
    const button = template.querySelector('.cp-item');
    const title = template.querySelector('.cp-item-id');
    const subtitle = template.querySelector('.cp-item-subtitle');
    const badge = template.querySelector('.cp-item-badge');
    const generalInfo = data?.general_info_by_cp?.[cpId] || {};
    const status = data?.snapshot?.status_by_cp?.[cpId]?.status || generalInfo?.cpms_connection_status || 'Unknown';
    const isActive = cpId === selectedCpId;
    const connectionState = generalInfo?.cpms_connection_status === 'connected' ? 'Connected' : data?.snapshot?.last_seen?.[cpId] ? 'Seen' : 'Idle';

    button.classList.toggle('active', isActive);
    title.textContent = cpId;
    subtitle.textContent = `${status} · ${generalInfo.model || 'No model'} · ${generalInfo.serial_number || 'No serial'}`;
    badge.textContent = connectionState;

    button.addEventListener('click', () => {
      state.selectedCpId = cpId;
      setQueryParam(cpId);
      renderAll(state.data);
    });

    els.cpList.appendChild(template);
  });
}

function renderKeyValues(container, entries) {
  container.innerHTML = '';

  entries.forEach(([label, value]) => {
    const wrapper = document.createElement('div');
    wrapper.className = 'kv-row';
    wrapper.innerHTML = `<dt>${label}</dt><dd>${formatValue(value)}</dd>`;
    container.appendChild(wrapper);
  });
}

function pickEntries(source, keys) {
  return keys.map((key) => [toTitle(key), source?.[key]]);
}

function renderDetail(data, selectedCpId) {
  const info = data?.general_info_by_cp?.[selectedCpId] || {};
  const status = data?.snapshot?.status_by_cp?.[selectedCpId] || {};
  const meter = data?.snapshot?.last_meter_values_by_cp?.[selectedCpId] || {};
  const openTx = data?.snapshot?.open_transactions_by_cp?.[selectedCpId] || {};
  const lastSeen = data?.snapshot?.last_seen?.[selectedCpId] || data?.last_seen?.[selectedCpId] || null;

  els.selectedCpLabel.textContent = selectedCpId || '-';
  els.detailTitle.textContent = selectedCpId ? `Charge Point ${selectedCpId}` : 'Choose a charge point';
  els.detailStatus.textContent = selectedCpId
    ? (info.cpms_connection_status === 'connected' ? 'Connected' : 'Disconnected')
    : 'Idle';
  els.detailStatus.className = `badge ${info.cpms_connection_status === 'connected' ? 'badge-success' : 'badge-neutral'}`;

  renderKeyValues(els.generalInfo, [
    ['Manufacturer', info.manufacturer],
    ['Model', info.model],
    ['Serial Number', info.serial_number],
    ['Firmware', info.firmware_version],
    ['IP Address', info.ip_address],
    ['IMSI', info.imsi],
    ['ICCID', info.iccid],
    ['Commissioning Date', info.commissioning_date],
    ['Uptime', info.uptime],
  ]);

  renderKeyValues(els.liveState, [
    ['Connection', info.cpms_connection_status],
    ['Status', status.status],
    ['Error Code', status.error_code],
    ['Connector ID', status.connector_id],
    ['Last Seen', lastSeen],
    ['Active Transaction', openTx.transaction_id],
    ['Last Meter Value', meter.meter_value],
    ['Boot Notification', info.boot_notification?.chargePointVendor || info.boot_notification?.charge_point_vendor],
  ]);
}

function renderSessions(data, selectedCpId) {
  const sessions = data?.sessions?.[selectedCpId] || [];
  els.sessionList.innerHTML = '';

  if (!selectedCpId) {
    els.sessionList.innerHTML = '<article class="timeline-item"><p class="timeline-body">Select a charge point to see its sessions.</p></article>';
    return;
  }

  if (!sessions.length) {
    els.sessionList.innerHTML = '<article class="timeline-item"><p class="timeline-body">No sessions recorded yet for this charge point.</p></article>';
    return;
  }

  sessions.slice().reverse().slice(0, 8).forEach((session) => {
    const template = templates.timelineItem.content.cloneNode(true);
    const title = template.querySelector('.timeline-title');
    const time = template.querySelector('.timeline-time');
    const body = template.querySelector('.timeline-body');

    title.textContent = `Session ${session.session_id}`;
    time.textContent = session.end || session.start || 'ongoing';
    body.textContent = [
      `Start: ${session.start || '—'}`,
      `End: ${session.end || '—'}`,
      `Duration: ${session.duration_s ? `${Math.round(session.duration_s)} s` : '—'}`,
      `Energy: ${session.energy_kwh ? `${Number(session.energy_kwh).toFixed(3)} kWh` : '—'}`,
      `Peak: ${session.kpis?.peak_kw ? `${Number(session.kpis.peak_kw).toFixed(2)} kW` : '—'}`,
      `Average: ${session.kpis?.avg_kw ? `${Number(session.kpis.avg_kw).toFixed(2)} kW` : '—'}`,
    ].join(' · ');

    els.sessionList.appendChild(template);
  });
}

function renderEvents(data, selectedCpId) {
  const events = (data?.events || [])
    .filter((event) => !selectedCpId || event.cp_id === selectedCpId)
    .slice(-12)
    .reverse();

  els.eventList.innerHTML = '';

  if (!events.length) {
    els.eventList.innerHTML = '<article class="timeline-item"><p class="timeline-body">No events available for this view.</p></article>';
    return;
  }

  events.forEach((event) => {
    const template = templates.timelineItem.content.cloneNode(true);
    const title = template.querySelector('.timeline-title');
    const time = template.querySelector('.timeline-time');
    const body = template.querySelector('.timeline-body');

    title.textContent = `${event.event_type}${event.action ? ` · ${event.action}` : ''}`;
    time.textContent = event.timestamp || '—';
    body.textContent = JSON.stringify(event, null, 2);

    els.eventList.appendChild(template);
  });
}

function wireCommand(formId, buildUrl, bodyBuilder = null) {
  const form = document.getElementById(formId);
  if (!form) return;

  form.addEventListener('submit', async (event) => {
    event.preventDefault();

    const selectedCpId = getSelectedChargePointId(state.data);
    if (!selectedCpId) {
      showToast('No charge point is available yet.', 'error');
      return;
    }

    const formData = new FormData(form);
    const url = buildUrl(selectedCpId, formData);
    const options = { method: 'POST' };

    if (bodyBuilder) {
      const body = bodyBuilder(formData);
      if (body !== null) {
        options.headers = { 'Content-Type': 'application/json' };
        options.body = JSON.stringify(body);
      }
    }

    try {
      const response = await fetch(url, options);
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.detail || payload.error || 'Command failed');
      }
      showToast(payload.result ? `${payload.result} for ${selectedCpId}` : 'Command sent', 'success');
      await loadData();
    } catch (error) {
      showToast(error.message, 'error');
    }
  });
}

function wireCommands() {
  wireCommand('force-start-form', (cpId, formData) => {
    const params = new URLSearchParams({
      connector_id: formData.get('connector_id') || '1',
      meter_start: formData.get('meter_start') || '0',
    });
    return `/api/cp/${cpId}/force_start?${params.toString()}`;
  });

  wireCommand('force-stop-form', (cpId, formData) => {
    const params = new URLSearchParams({
      transaction_id: formData.get('transaction_id') || '',
      meter_stop: formData.get('meter_stop') || '0',
    });
    return `/api/cp/${cpId}/force_stop?${params.toString()}`;
  });

  wireCommand('remote-start-form', (cpId, formData) => {
    const params = new URLSearchParams();
    const connectorId = formData.get('connector_id');
    const idTag = formData.get('id_tag');
    if (connectorId) params.set('connector_id', connectorId);
    if (idTag) params.set('id_tag', idTag);
    return `/api/cp/${cpId}/remote_start${params.toString() ? `?${params.toString()}` : ''}`;
  });

  wireCommand('remote-stop-form', (cpId, formData) => {
    const params = new URLSearchParams();
    const transactionId = formData.get('transaction_id');
    if (transactionId) params.set('transaction_id', transactionId);
    return `/api/cp/${cpId}/remote_stop?${params.toString()}`;
  });

  wireCommand('remote-reboot-form', (cpId, formData) => {
    const params = new URLSearchParams({
      reset_type: formData.get('reset_type') || 'Soft',
    });
    return `/api/cp/${cpId}/remote_reboot?${params.toString()}`;
  });

  wireCommand('unlock-form', (cpId, formData) => {
    const params = new URLSearchParams({
      connector_id: formData.get('connector_id') || '1',
    });
    return `/api/cp/${cpId}/unlock_connector?${params.toString()}`;
  });
}

async function loadData() {
  const url = state.selectedCpId ? `/api/cp?cp_id=${encodeURIComponent(state.selectedCpId)}` : '/api/cp';
  const response = await fetch(url);
  if (response.status === 401) {
    throw new Error('Unauthorized');
  }

  if (!response.ok) {
    throw new Error(`Failed to load dashboard data: ${response.status}`);
  }

  state.data = await response.json();
  const selectedCpId = getSelectedChargePointId(state.data);
  state.selectedCpId = selectedCpId;
  setQueryParam(selectedCpId);
  renderAll(state.data);
}

function renderAll(data) {
  const selectedCpId = getSelectedChargePointId(data);
  updateGlobalStatus(data);
  renderCpList(data, selectedCpId);
  renderDetail(data, selectedCpId);
  renderSessions(data, selectedCpId);
  renderEvents(data, selectedCpId);
}

async function bootstrap() {
  updateAuthView();

  els.loginForm.addEventListener('submit', async (event) => {
    event.preventDefault();

    const email = els.loginEmail.value.trim();
    const password = els.loginPassword.value;

    els.loginError.textContent = '';
    els.loginEmail.disabled = true;
    els.loginPassword.disabled = true;

    try {
      await signIn(email, password);
      await startDashboard();
    } catch (error) {
      setAuthenticated(false);
      els.loginError.textContent = error.message || 'Invalid email or password.';
    } finally {
      els.loginEmail.disabled = false;
      els.loginPassword.disabled = false;
    }
  });

  els.logoutBtn.addEventListener('click', async () => {
    await signOut();
  });

  if (!state.commandsWired) {
    wireCommands();
    state.commandsWired = true;
  }

  els.refreshBtn.addEventListener('click', async () => {
    els.refreshBtn.disabled = true;
    try {
      await loadData();
      showToast('Dashboard refreshed', 'success');
    } catch (error) {
      showToast(error.message, 'error');
    } finally {
      els.refreshBtn.disabled = false;
    }
  });

  try {
    const sessionActive = await checkSession();
    setAuthenticated(sessionActive);
    if (sessionActive) {
      await startDashboard();
    }
  } catch (error) {
    console.error(error);
  }
}

bootstrap();
