const FETCH_INTERVAL_MS = 10000;

let map;
let deviceMarkers = {};   // ident -> Leaflet marker
let beaconCircles = {};   // beaconKey -> Leaflet circle
let beaconPrevState = {}; // for in/out detection
let beaconLastStatusAt = {}; // key -> last status-notification timestamp (ms)
let notifications = [];
let lastNotifId = 0;
let reports = [];
let unreadCount = 0;
let heatLayer = null;

let currentBeaconNames = {};
let lastDevices = [];
let lastBeaconsAgg = [];
let currentDeviceFilter = '';   // '' = all devices
let deviceColors = {};          // ident -> color from backend

let renameContext = null;       // { type: 'device'|'beacon', id: string }


// Smart status alert thresholds (tuned for FMC data every 5 minutes)
const OFFLINE_THRESHOLD_SECONDS = 20 * 60;   // 20 minutes with 5 min sends (~4 missed updates)
const DISTANCE_ALERT_THRESHOLD_METERS = 5;   // "Far" if beyond 5m

// Smart alert state
let deviceStatus = {};          // ident -> 'online' | 'offline'


// ---- Map init ----
function initMap() {
  map = L.map('map').setView([-13.85, -171.75], 15); // default centre (adjust as needed)

  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; OpenStreetMap'
  }).addTo(map);
}


// ---- Fetch + update loop ----

async function fetchAndUpdateMapData() {
  try {
    const resp = await fetch('/data');
    if (!resp.ok) {
      console.error('Failed to fetch /data', resp.status);
      return;
    }
    const payload = await resp.json();
    const devices = payload.devices || [];
    const beaconNames = payload.beacon_names || {};

    currentBeaconNames = beaconNames;
    lastDevices = devices;

    // Extract daily report if present
    const dailyReport = devices.find(d => d && d.ident === 'DAILY_REPORT');
    if (dailyReport && dailyReport.report) {
      addDailyReport(dailyReport);
    }

    // Build aggregated beacon list (across devices)
    const aggBeacons = aggregateBeacons(devices, beaconNames);
    // in/out detection + smart status alerts
    const nowMs = Date.now();
    const STATUS_INTERVAL_MS = 10 * 60 * 1000; // 10 minutes between "still in/out" status updates per beacon
    const DISTANCE_ALERT_THRESHOLD_M = 5;

    aggBeacons.forEach(b => {
      const key = `${b.deviceIdent || 'unknown'}::${b.id}`;
      const dist = (b.distance != null) ? Number(b.distance) : 9999;

      const nowState = dist <= 3 ? 'in' : 'out';
      const prev = beaconPrevState[key];

      // Status ping every 10 minutes while the beacon stays in the same state

      // First time we see this beacon in this browser session:
      // set the baseline state but do NOT emit IN/LEFT. This prevents refresh/new page from
      // creating fake transition events.
      if (prev == null) {
        beaconPrevState[key] = nowState;
        beaconLastStatusAt[key] = nowMs;
        return;
      }

      // One-time notifications when a beacon actually moves in/out of range
      if (prev !== nowState) {
        beaconPrevState[key] = nowState;
        beaconLastStatusAt[key] = nowMs; // reset status timer on real movement

        if (nowState === 'out') {
          addNotification('left', b.name || b.id, b.last_seen, dist, { beaconId: b.id, deviceIdent: b.deviceIdent });
        } else {
          addNotification('in', b.name || b.id, b.last_seen, dist, { beaconId: b.id, deviceIdent: b.deviceIdent });
        }
        return;
      }

      // Beacon stayed in the same state: send a "still in/out" ping occasionally.
      const lastStatusAt = beaconLastStatusAt[key] || 0;
      if (nowMs - lastStatusAt >= STATUS_INTERVAL_MS) {
        // STILL status is generated server-side by cron_evaluator.
        beaconLastStatusAt[key] = nowMs;
      }
    });

lastBeaconsAgg = aggBeacons;

    // ---- Update summary sidebar ----
    const goodDevices = devices.filter(d => d.ident !== "DAILY_REPORT");
    document.getElementById("summary-devices").textContent = goodDevices.length;
    document.getElementById("summary-beacons").textContent = aggBeacons.length;

    // Device-level offline / online alerts (tuned for 5-minute FMC sends)
    const nowUnixSec = Date.now() / 1000;
    goodDevices.forEach(d => {
      const ident = d.ident;
      const label = d.name || ident;
      const tsRaw = d.timestamp_raw;
      if (tsRaw == null) {
        return;
      }
      const lastTs = Number(tsRaw);
      if (!Number.isFinite(lastTs)) {
        return;
      }
      const ageSec = nowUnixSec - lastTs;
      const isOffline = ageSec > OFFLINE_THRESHOLD_SECONDS;

      // First time we see this device in this browser session: set baseline only.
      // This prevents page refresh/new tab from creating fake OFFLINE/ONLINE transitions.
      if (deviceStatus[ident] == null) {
        deviceStatus[ident] = isOffline ? 'offline' : 'online';
        return;
      }

      const wasOffline = deviceStatus[ident] === 'offline';

      if (!wasOffline && isOffline) {
        deviceStatus[ident] = 'offline';
        addNotification('offline', label, d.timestamp, null, { deviceIdent: ident });
      } else if (wasOffline && !isOffline) {
        deviceStatus[ident] = 'online';
        addNotification('online', label, d.timestamp, null, { deviceIdent: ident });
      }
    });

    updateMap(devices, aggBeacons);
    updateSidebar(devices, beaconNames);
  } catch (e) {
    console.error('Error in fetchAndUpdateMapData', e);
  }
}

function startPolling() {
  fetchAndUpdateMapData();
  fetchRecentNotifications();
  setInterval(() => { fetchAndUpdateMapData(); fetchRecentNotifications(); }, FETCH_INTERVAL_MS);
}


// ---- Aggregate beacons across devices ----

function aggregateBeacons(devices, beaconNames) {
  const result = [];
  devices.forEach(d => {
    if (!d || !d.beacons || d.ident === 'DAILY_REPORT') return;
    (d.beacons || []).forEach(b => {
      const id = b.id;
      const name = beaconNames[id] || b.name || id;
      result.push({
        id,
        name,
        deviceIdent: d.ident,
        deviceName: d.name || d.ident,
        deviceColor: d.color || '#3b82f6',
        distance: b.distance,
        last_seen: b.last_seen,
        rssi: b.rssi,
        lat: d.lat,
        lon: d.lon
      });
    });
  });
  return result;
}


// ---- Map rendering ----

function getDeviceColor(ident, fallback) {
  if (deviceColors[ident]) return deviceColors[ident];
  if (fallback) {
    deviceColors[ident] = fallback;
    return fallback;
  }
  deviceColors[ident] = '#3b82f6';
  return deviceColors[ident];
}


// ---- Notifications ----

function addNotification(type, beaconName, eventTime, distance, options) {
  const opts = options || {};
  const localOnly = !!opts.localOnly;
  const timeStr = eventTime || '-';
  const displayName = opts.displayName || beaconName;
  const msg = {
    type,
    name: displayName,
    time: timeStr,
    distance: distance
  };

  // Helpful identifiers (backend uses these for stable dedupe / throttling)
  if (opts.beaconId) msg.beacon_id = opts.beaconId;
  if (opts.deviceIdent) msg.device_ident = opts.deviceIdent;
  notifications.push(msg);

  // Only persist real events (IN/LEFT, alerts, etc.) to the backend.
  // Local-only status updates stay in memory so history and PDFs stay clean.
  if (!localOnly) {
    try {
      fetch('/api/notifications', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(msg)
      });
    } catch (err) {
      console.error('Failed to POST /api/notifications', err);
    }
  }

  unreadCount += 1;
  updateNotificationBadge();
  renderNotificationsList();

  // pop-up toast
  try { showToast(msg); } catch (e) {}
}


function ensureNotificationsPanelVisible() {
  const panel = document.getElementById('notifications-panel');
  if (!panel) return;
  if (panel.classList.contains('hidden')) {
    panel.classList.remove('hidden');
  }
}

function updateNotificationBadge() {
  const badge = document.getElementById('notifications-badge');
  if (!badge) return;
  badge.textContent = unreadCount > 0 ? String(unreadCount) : '';
}

function renderNotificationsList() {
  const list = document.getElementById('notifications-list');
  if (!list) return;
  list.innerHTML = '';

  notifications.slice().reverse().forEach(n => {
    const item = document.createElement('div');
    item.className = 'notification-item';

    // Map internal type to a CSS class for colour
    let typeClass;
    if (n.type === 'left') typeClass = 'left';
    else if (n.type === 'in') typeClass = 'in';
    else if (n.type === 'offline') typeClass = 'offline';
    else if (n.type === 'still_in' || n.type === 'still_out' || n.type === 'status') typeClass = 'status';
    else if (n.type === 'online') typeClass = 'online';
    else if (n.type === 'distance' || n.type === 'signal') typeClass = 'alert';
    else typeClass = 'in';

    item.innerHTML = `
      <span class="time">${n.time}</span>
      <span class="type-${typeClass}">${n.type.toUpperCase()}</span>
      <span class="name">${n.name}</span>
      ${
        n.distance != null
          ? `<span class="distance">(${n.distance.toFixed(2)} m)</span>`
          : ''
      }
    `;
    list.appendChild(item);
  });
}

function showToast(msg) {
  const cont = document.getElementById('toast-container');
  if (!cont) return;
  const el = document.createElement('div');

  let cls = 'toast';
  if (msg.type === 'left') cls += ' toast-left';
  else if (msg.type === 'in') cls += ' toast-in';
  else if (msg.type === 'offline') cls += ' toast-offline';
  else if (msg.type === 'online') cls += ' toast-online';
  else if (msg.type === 'still_in' || msg.type === 'still_out' || msg.type === 'status') cls += ' toast-status';
  else if (msg.type === 'distance' || msg.type === 'signal') cls += ' toast-alert';

  el.className = cls.trim();

  let text = `${msg.type.toUpperCase()}: ${msg.name}`;
  if ((msg.type === 'distance' || msg.type === 'signal') && msg.distance != null) {
    try {
      text += ` (${msg.distance.toFixed(2)} m)`;
    } catch (_) {
      // ignore formatting issues
    }
  }
  el.textContent = text;

  cont.appendChild(el);
  setTimeout(() => {
    if (el.parentNode === cont) {
      cont.removeChild(el);
    }
  }, 4000);
}




function setupNotificationsUI() {
  const notifButton = document.getElementById('notif-button');
  const panel = document.getElementById('notifications-panel');
  const closeBtn = document.getElementById('notif-close');
  const clearBtn = document.getElementById('notif-clear');

  if (!panel) return;

  const hidePanel = () => {
    panel.classList.add('hidden');
  };

  const openPanel = () => {
    panel.classList.remove('hidden');
    // when user views panel, reset unread counter
    unreadCount = 0;
    updateNotificationBadge();
  };

  const togglePanel = () => {
    if (panel.classList.contains('hidden')) {
      openPanel();
    } else {
      hidePanel();
    }
  };

  if (notifButton) {
    notifButton.addEventListener('click', togglePanel);
  }
  if (closeBtn) {
    closeBtn.addEventListener('click', hidePanel);
  }
  if (clearBtn) {
    clearBtn.addEventListener('click', () => {
      notifications = [];
      renderNotificationsList();
      unreadCount = 0;
      updateNotificationBadge();
    });
  }
}

async function fetchRecentNotifications() {
  try {
    const resp = await fetch(`/api/notifications/recent?since_id=${lastNotifId}`);
    if (!resp.ok) return;
    const data = await resp.json();
    const items = data.items || [];
    if (!items.length) return;

    items.forEach(item => {
      lastNotifId = Math.max(lastNotifId, item.id || 0);
      const displayName = item.beacon_name || item.beacon_id || 'Unknown';
      const msg = {
        type: item.type || 'event',
        name: displayName,
        time: item.event_time || item.created_at || '-',
        distance: item.distance,
        beacon_id: item.beacon_id,
        device_ident: item.device_ident
      };

      notifications.push(msg);
      unreadCount += 1;
      try { showToast(msg); } catch (e) {}
    });

    updateNotificationBadge();
    renderNotificationsList();
  } catch (e) {
    console.error('Failed to fetch recent notifications', e);
  }
}
