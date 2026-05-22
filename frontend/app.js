const state = {
  data: null,
  selectedCpId: new URLSearchParams(window.location.search).get('cp_id') || null,
  refreshTimer: null,
  isAuthenticated: false,
  commandsWired: false,
  currentUser: null,
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
  metricRegisteredCps: document.getElementById('metric-registered-cps'),
  metricEstimatedRevenue: document.getElementById('metric-estimated-revenue'),
  roleBadge: document.getElementById('role-badge'),
  roleDescription: document.getElementById('role-description'),
  administrationPanel: document.getElementById('charge-point-card')?.closest('.card.full-span') || document.querySelector('#charge-point-card')?.parentElement,
  billingPanel: document.getElementById('billing-card'),
  registeredCpList: document.getElementById('registered-cp-list'),
  userList: document.getElementById('user-list'),
  billingSummary: document.getElementById('billing-summary'),
  chargePointForm: document.getElementById('charge-point-form'),
  userForm: document.getElementById('user-form'),
  adminCards: document.querySelectorAll('.admin-card'),
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
    // Format objects as readable key-value pairs
    const entries = Object.entries(value)
      .filter(([_, v]) => v !== null && v !== undefined && v !== '')
      .map(([k, v]) => `${k}: ${typeof v === 'object' ? JSON.stringify(v) : v}`);
    return entries.length ? entries.join(', ') : '—';
  }

  return String(value);
}

function formatMoney(value, currency = 'dt') {
  const amount = Number(value || 0);
  const normalizedCurrency = String(currency || 'dt').trim().toUpperCase();
  const formatCurrency = normalizedCurrency === 'DT' ? 'TND' : normalizedCurrency;
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: formatCurrency,
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(amount);
}

function isManagementRole(role) {
  return ['admin', 'steg'].includes(String(role || '').toLowerCase());
}

function getRoleProfile(role) {
  const normalizedRole = String(role || 'operator').toLowerCase();
  if (normalizedRole === 'admin') {
    return {
      role: 'admin',
      label: 'Admin',
      description: 'Full access: monitoring, remote control, charge-point management, billing, and users.',
      canManageChargePoints: true,
      canEditTariff: true,
      canManageUsers: true,
      canSeeBilling: true,
    };
  }

  if (normalizedRole === 'steg') {
    return {
      role: 'steg',
      label: 'Supervision',
      description: 'Supervision access: monitoring, remote control, charge-point data, billing, and tariff updates.',
      canManageChargePoints: true,
      canEditTariff: true,
      canManageUsers: false,
      canSeeBilling: true,
    };
  }

  return {
    role: 'operator',
    label: 'Operator',
    description: 'Operations access: live monitoring and remote commands only.',
    canManageChargePoints: false,
    canEditTariff: false,
    canManageUsers: false,
    canSeeBilling: false,
  };
}

function setFieldVisibility(form, fieldName, visible) {
  if (!form) return;
  const field = form.querySelector(`[name="${fieldName}"]`);
  if (!field || !field.parentElement) return;
  field.disabled = !visible;
  field.parentElement.hidden = !visible;
}

function setChargePointFormMode(profile) {
  if (!els.chargePointForm) return;

  const title = document.querySelector('#charge-point-card h4');
  const note = document.querySelector('#charge-point-card .note');
  const submitButton = els.chargePointForm.querySelector('button[type="submit"]');

  if (profile.role === 'steg') {
    if (title) title.textContent = 'Update Tariff';
    if (note) note.textContent = 'Read-only charge-point supervision. Only tariff changes are allowed.';
    setFieldVisibility(els.chargePointForm, 'cp_id', true);
    setFieldVisibility(els.chargePointForm, 'tariff_per_kwh', true);
    setFieldVisibility(els.chargePointForm, 'label', false);
    setFieldVisibility(els.chargePointForm, 'site', false);
    setFieldVisibility(els.chargePointForm, 'connector_count', false);
    setFieldVisibility(els.chargePointForm, 'notes', false);
    if (submitButton) submitButton.textContent = 'Update Tariff';
    return;
  }

  if (title) title.textContent = 'Register Charge Point';
  if (note) note.textContent = 'Adds offline or upcoming chargers to the fleet';
  setFieldVisibility(els.chargePointForm, 'cp_id', true);
  setFieldVisibility(els.chargePointForm, 'label', true);
  setFieldVisibility(els.chargePointForm, 'site', true);
  setFieldVisibility(els.chargePointForm, 'connector_count', true);
  setFieldVisibility(els.chargePointForm, 'tariff_per_kwh', true);
  setFieldVisibility(els.chargePointForm, 'notes', true);
  if (submitButton) submitButton.textContent = 'Save Charge Point';
}

function updateAuthView() {
  const loggedIn = state.isAuthenticated;
  if (els.loginScreen) {
    els.loginScreen.hidden = loggedIn;
  }
  if (els.appShell) {
    els.appShell.hidden = !loggedIn;
  }
  document.body.classList.toggle('auth-locked', !loggedIn);

  if (!loggedIn && els.loginError && els.loginPassword) {
    els.loginError.textContent = '';
    els.loginPassword.value = '';
    els.loginPassword.focus();
  }
}

function setAuthenticated(isAuthenticated) {
  state.isAuthenticated = isAuthenticated;
  updateAuthView();
}

function setCurrentUser(user) {
  state.currentUser = user || null;
  const profile = getRoleProfile(state.currentUser?.role);

  document.body.classList.toggle('admin-user', profile.role === 'admin');
  document.body.classList.toggle('operator-user', profile.role === 'operator');
  document.body.classList.toggle('steg-user', profile.role === 'steg');

  if (els.roleBadge) {
    els.roleBadge.textContent = profile.label;
  }

  if (els.roleDescription) {
    els.roleDescription.textContent = profile.description;
  }

  if (els.administrationPanel) {
    els.administrationPanel.hidden = !profile.canManageChargePoints && !profile.canManageUsers;
  }

  if (els.billingPanel) {
    els.billingPanel.hidden = !profile.canSeeBilling;
  }

  if (els.chargePointForm) {
    els.chargePointForm.hidden = !profile.canManageChargePoints;
    setChargePointFormMode(profile);
  }

  if (els.userForm) {
    els.userForm.hidden = !profile.canManageUsers;
  }

  if (els.userList) {
    els.userList.hidden = !profile.canManageUsers;
  }
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
  if (!els.appShell) {
    return;
  }

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
  const response = await fetch('/api/auth/me', { credentials: 'include' });
  if (!response.ok) {
    return null;
  }

  const payload = await response.json();
  if (payload?.authenticated === true) {
    return payload;
  }

  return null;
}

async function signIn(email, password) {
  const response = await fetch('/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
    body: JSON.stringify({ email, password }),
    credentials: 'include'
  });

  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.detail || 'Invalid email or password.');
  }

  if (payload?.authenticated === true || payload?.email) {
    setCurrentUser(payload);
  }
  setAuthenticated(true);
  return payload;
}

async function signOut() {
  await fetch('/api/auth/logout', { method: 'POST', credentials: 'include' });
  stopAutoRefresh();
  state.data = null;
  state.currentUser = null;
  setCurrentUser(null);
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
    data?.charge_points,
    data?.registered_charge_points,
  ];

  sources.forEach((source) => {
    if (!source) return;
    if (Array.isArray(source)) {
      source.forEach((item) => {
        if (item?.cp_id) {
          ids.add(item.cp_id);
        }
      });
      return;
    }

    Object.keys(source).forEach((key) => ids.add(key));
  });

  return [...ids].sort((a, b) => a.localeCompare(b));
}

function getChargePointView(data, cpId) {
  return data?.charge_points?.find((item) => item.cp_id === cpId)
    || data?.registered_charge_points?.find((item) => item.cp_id === cpId)
    || {};
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
  const registeredCps = data?.charge_points?.length ?? 0;
  const estimatedRevenue = data?.billing?.estimated_revenue ?? 0;

  els.metricActiveConnections.textContent = String(activeConnections);
  els.metricTotalMessages.textContent = String(totalMessages);
  els.metricTotalEnergy.textContent = `${Number(totalEnergy).toFixed(2)} kWh`;
  els.metricOpenTransactions.textContent = String(openTransactions);
  els.metricRegisteredCps.textContent = String(registeredCps);
  els.metricEstimatedRevenue.textContent = formatMoney(estimatedRevenue, data?.billing?.currency || 'dt');

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
    const cpView = getChargePointView(data, cpId);
    const status = cpView.status || data?.snapshot?.status_by_cp?.[cpId]?.status || generalInfo?.cpms_connection_status || 'Unknown';
    const isActive = cpId === selectedCpId;
    const connectionState = cpView.connection_state === 'connected' ? 'Connected' : 'Offline';

    button.classList.toggle('active', isActive);
    title.textContent = cpView.label || cpId;
    subtitle.textContent = `${status} · ${cpView.model || generalInfo.model || 'No model'} · ${cpView.serial_number || generalInfo.serial_number || 'No serial'}`;
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
  const cpView = getChargePointView(data, selectedCpId);
  const status = data?.snapshot?.status_by_cp?.[selectedCpId] || {};
  const meter = data?.snapshot?.last_meter_values_by_cp?.[selectedCpId] || {};
  const openTx = data?.snapshot?.open_transactions_by_cp?.[selectedCpId] || {};
  const lastSeen = data?.snapshot?.last_seen?.[selectedCpId] || data?.last_seen?.[selectedCpId] || null;

  els.selectedCpLabel.textContent = selectedCpId || '-';
  els.detailTitle.textContent = selectedCpId ? (cpView.label || `Charge Point ${selectedCpId}`) : 'Choose a charge point';
  els.detailStatus.textContent = selectedCpId
    ? (cpView.connection_state === 'connected' ? 'Connected' : 'Offline')
    : 'Idle';
  els.detailStatus.className = `badge ${cpView.connection_state === 'connected' ? 'badge-success' : 'badge-neutral'}`;

  renderKeyValues(els.generalInfo, [
    ['Label', cpView.label || selectedCpId],
    ['Site', cpView.site],
    ['Manufacturer', info.manufacturer],
    ['Model', info.model],
    ['Serial Number', info.serial_number],
    ['Firmware', info.firmware_version],
    ['Tariff / kWh', cpView.tariff_per_kwh ? `${Number(cpView.tariff_per_kwh).toFixed(2)} dt` : '—'],
    ['IP Address', info.ip_address],
    ['IMSI', info.imsi],
    ['ICCID', info.iccid],
    ['Commissioning Date', info.commissioning_date],
    ['Uptime', info.uptime],
  ]);

  renderKeyValues(els.liveState, [
    ['Connection', cpView.connection_state || info.cpms_connection_status],
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
      `Price: ${session.price !== undefined ? formatMoney(session.price, session.currency || 'dt') : '—'}`,
      `User: ${session.user || session.id_tag || '—'}`,
    ].join(' · ');

    els.sessionList.appendChild(template);
  });
}

function renderEntityList(container, items, emptyMessage, renderItem) {
  container.innerHTML = '';

  if (!items.length) {
    container.innerHTML = `<article class="timeline-item"><p class="timeline-body">${emptyMessage}</p></article>`;
    return;
  }

  items.forEach((item) => {
    const element = document.createElement('article');
    element.className = 'entity-item';
    element.innerHTML = renderItem(item);
    container.appendChild(element);
  });
}

function renderAdministration(data) {
  const chargePoints = data?.charge_points || [];
  const users = data?.users || [];
  const billing = data?.billing || {};
  const profile = getRoleProfile(state.currentUser?.role);

  if (!profile.canManageChargePoints) {
    els.registeredCpList.innerHTML = '<article class="timeline-item"><p class="timeline-body">Charge-point management is not available for this role.</p></article>';
  } else {
    renderEntityList(
      els.registeredCpList,
      chargePoints,
      'No registered charge points yet.',
      (cp) => `
        <div class="entity-item-head">
          <strong>${formatValue(cp.label || cp.cp_id)}</strong>
          <span class="badge ${cp.connection_state === 'connected' ? 'badge-success' : 'badge-neutral'}">${formatValue(cp.status || cp.connection_state)}</span>
        </div>
        <p class="entity-item-body">${formatValue(cp.cp_id)} · ${formatValue(cp.site || 'No site')} · ${formatValue(cp.tariff_per_kwh ? `${Number(cp.tariff_per_kwh).toFixed(2)} dt / kWh` : 'Tariff not set')}</p>
      `,
    );
  }

  if (profile.canManageUsers) {
    renderEntityList(
      els.userList,
      users,
      'No users defined yet.',
      (user) => `
        <div class="entity-item-head">
          <strong>${formatValue(user.email)}</strong>
          <span class="badge ${isManagementRole(user.role) ? 'badge-warning' : 'badge-neutral'}">${user.role === 'steg' ? 'STEG' : formatValue(user.role)}</span>
        </div>
        <p class="entity-item-body">${formatValue(user.name || user.email)} · ${user.active ? 'Active' : 'Disabled'}</p>
      `,
    );
  } else {
    els.userList.innerHTML = '<article class="timeline-item"><p class="timeline-body">User management is not available for this role.</p></article>';
  }

  renderKeyValues(els.billingSummary, [
    ['Tariff per kWh', billing.tariff_per_kwh !== undefined ? `${Number(billing.tariff_per_kwh).toFixed(2)} dt` : '—'],
    ['Currency', billing.currency || 'dt'],
    ['Total Energy', billing.total_energy_kwh !== undefined ? `${Number(billing.total_energy_kwh).toFixed(3)} kWh` : '—'],
    ['Estimated Revenue', billing.estimated_revenue !== undefined ? formatMoney(billing.estimated_revenue, billing.currency || 'dt') : '—'],
  ]);

  if (profile.role === 'steg') {
    const billingTitle = document.querySelector('#billing-card .panel-head h3');
    const billingNote = document.querySelector('#billing-card .panel-head .note');
    if (billingTitle) billingTitle.textContent = 'Supervision';
    if (billingNote) billingNote.textContent = 'Read-only billing summary with tariff supervision.';
  }
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
    
    // Format timestamp to be more readable
    let formattedTime = '—';
    if (event.timestamp) {
      try {
        const date = new Date(event.timestamp);
        // Format as: MM/DD/YYYY HH:MM:SS
        formattedTime = date.toLocaleString('en-US', {
          year: 'numeric',
          month: '2-digit',
          day: '2-digit',
          hour: '2-digit',
          minute: '2-digit',
          second: '2-digit',
          hour12: false
        });
      } catch (e) {
        // Fallback to original timestamp if parsing fails
        formattedTime = event.timestamp;
      }
    }
    time.textContent = formattedTime;
    
    // Create a more readable display of event details
    let details = '';
    if (event.event_type === 'connection_opened') {
      details = `Connected from ${event.remote_address || 'unknown'} on path ${event.path || 'unknown'}`;
    } else if (event.event_type === 'connection_closed') {
      details = `Connection closed: ${event.reason || 'unknown reason'}`;
    } else if (event.event_type === 'ocpp_message') {
      details = `OCPP ${event.action || 'message'}`;
      if (event.payload && Object.keys(event.payload).length > 0) {
        // Show key payload fields depending on action
        if (event.action === 'BootNotification') {
          details += ` - ${event.payload.charge_point_vendor || 'Unknown'} ${event.payload.charge_point_model || 'Unknown'}`;
        } else if (event.action === 'Heartbeat') {
          details += ` - OK`;
        } else if (event.action === 'MeterValues' && event.payload.meter_value) {
          const mv = event.payload.meter_value[0] || {};
          const sampledValue = mv.sampled_value || [];
          const energyValue = sampledValue.find(sv => sv.measurand === 'Energy.Active.Import.Register');
          if (energyValue) {
            details += ` - Energy: ${energyValue.value} ${energyValue.unit}`;
          }
        }
      }
    } else if (event.event_type === 'transaction_closed') {
      details = `Transaction stopped`;
      if (event.payload) {
        if (event.payload.meter_stop !== undefined) {
          details += ` - Final meter: ${event.payload.meter_stop}`;
        }
        if (event.payload.transaction_id !== undefined) {
          details += ` - TX ID: ${event.payload.transaction_id}`;
        }
      }
} else {
        // Format simple event details without JSON
        const eventCopy = {...event};
        delete eventCopy.timestamp;
        delete eventCopy.cp_id;
        const keys = Object.keys(eventCopy);
        if (keys.length > 0) {
          details = keys.map(k => `${k}: ${formatValue(eventCopy[k])}`).join(' | ');
        } else {
          details = event.event_type || 'Event occurred';
        }
      }

      body.textContent = details;
    els.eventList.appendChild(template);
  });
}

function wireAdministrationForms() {
  if (els.chargePointForm) {
    els.chargePointForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      const formData = new FormData(els.chargePointForm);
      const profile = getRoleProfile(state.currentUser?.role);
      const body = {
        cp_id: String(formData.get('cp_id') || '').trim(),
        label: String(formData.get('label') || '').trim(),
        site: String(formData.get('site') || '').trim(),
        connector_count: Number(formData.get('connector_count') || 1),
        tariff_per_kwh: formData.get('tariff_per_kwh') ? Number(formData.get('tariff_per_kwh')) : null,
        notes: String(formData.get('notes') || '').trim(),
      };

      try {
        const endpoint = profile.role === 'steg'
          ? `/api/charge-points/${encodeURIComponent(body.cp_id)}/tariff`
          : '/api/charge-points';
        const requestBody = profile.role === 'steg'
          ? { tariff_per_kwh: body.tariff_per_kwh }
          : body;

        const response = await fetch(endpoint, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(requestBody),
          credentials: 'include'
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(payload.detail || 'Unable to save charge point');
        }
        els.chargePointForm.reset();
        showToast(profile.role === 'steg'
          ? `Tariff updated for ${payload.charge_point.cp_id}`
          : `Charge point ${payload.charge_point.cp_id} saved`, 'success');
        await loadData();
      } catch (error) {
        showToast(error.message, 'error');
      }
    });
  }

  if (els.userForm) {
    els.userForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      const formData = new FormData(els.userForm);
      const body = {
        email: String(formData.get('email') || '').trim(),
        name: String(formData.get('name') || '').trim(),
        password: String(formData.get('password') || ''),
        role: String(formData.get('role') || 'operator').trim(),
      };

      try {
        const response = await fetch('/api/users', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
          credentials: 'include'
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(payload.detail || 'Unable to create user');
        }
        els.userForm.reset();
        showToast(`User ${payload.user.email} created`, 'success');
        await loadData();
      } catch (error) {
        showToast(error.message, 'error');
      }
    });
  }
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
    const options = { method: 'POST', credentials: 'include' };

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
  const response = await fetch(url, { credentials: 'include' });
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
  renderAdministration(data);
  renderSessions(data, selectedCpId);
  renderEvents(data, selectedCpId);
}

async function bootstrap() {
  updateAuthView();

  if (els.loginForm) {
    els.loginForm.addEventListener('submit', async (event) => {
      event.preventDefault();

      const email = els.loginEmail.value.trim();
      const password = els.loginPassword.value;

      els.loginError.textContent = '';
      els.loginEmail.disabled = true;
      els.loginPassword.disabled = true;

      try {
        await signIn(email, password);
        window.location.replace('/cityos');
      } catch (error) {
        setAuthenticated(false);
        els.loginError.textContent = error.message || 'Invalid email or password.';
      } finally {
        els.loginEmail.disabled = false;
        els.loginPassword.disabled = false;
      }
    });
  }

  if (els.logoutBtn) {
    els.logoutBtn.addEventListener('click', async () => {
      await signOut();
      window.location.replace('/login');
    });
  }

  if (!state.commandsWired) {
    wireCommands();
    state.commandsWired = true;
  }

  wireAdministrationForms();

  if (els.refreshBtn) {
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
  }

  try {
    const sessionUser = await checkSession();
    if (sessionUser) {
      setCurrentUser(sessionUser);
      setAuthenticated(true);
      if (els.appShell) {
        await startDashboard();
      } else {
        window.location.replace('/cityos');
      }
    } else {
      setCurrentUser(null);
      setAuthenticated(false);
    }
  } catch (error) {
    console.error(error);
  }
}

bootstrap();
