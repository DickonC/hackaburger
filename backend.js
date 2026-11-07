/* =====================================================================
   HACKABURGER BACKEND ADAPTER
   Connects index.html to the backend (docs/backend-api.md) without
   editing the simulation code: it hooks the page's own functions.

   - Rooms come from /rooms (custom rooms are saved there, not localStorage)
   - Occupancy + CO2 + forecast come from the server for rooms with data
     (a live board or history); other rooms keep the original simulation
   - Window toggles are saved with PUT /rooms/{id}/windows
   - "Deploy Config" sends the range threshold + interval to the board
   - The BLE packet log shows the board's real (anonymised) devices

   Settings (localStorage): hackaburger_api_key, hackaburger_api_base,
   hackaburger_weekday ("wed" to show a weekday pattern on weekends).
   ===================================================================== */
(function () {
  'use strict';

  const API_BASE = localStorage.getItem('hackaburger_api_base') || 'https://hackaburger.cch-uk.tech/ingest';
  const POLL_MS = 5000;
  const RANGE_OPTIONS = [
    [-40, 'arm’s length'], [-55, '~1–2 m · one table'], [-65, '~3–5 m · small room'],
    [-75, '~5–10 m · normal room'], [-85, '~10–20 m · large hall'], [-100, 'everything heard'],
  ];
  // Indexes into TIME_LABELS: 8 past slots every 15 min, NOW, then future offsets in minutes
  const FUTURE_MIN = [15, 30, 45, 60, 90, 120, 150, 180, 240];

  const live = {
    enabled: false,      // backend reachable with a valid key
    forceSim: false,     // user switched back to the simulation
    mode: 'sim',         // 'live' | 'model' | 'sim' for the current room
    seedIds: DEFAULT_ROOMS.map(r => r.id),
    customs: [],         // server rooms that aren't presets, in the page's room shape
    serverRooms: {},     // id -> server room
    forecast: null, pastCo2: null, pastOcc: null, board: null, devices: [], deviceCursor: 0,
    configs: {},         // device -> board config
    lastUpdate: 0, error: null, polling: null, badgeTimer: null,
    pendingUpdates: {}, // room id -> debounce timer for PUTs (sliders fire many changes)
  };

  // ------------------------------------------------------------ API

  function apiKey(reset) {
    if (reset) localStorage.removeItem('hackaburger_api_key');
    let key = localStorage.getItem('hackaburger_api_key');
    if (!key) {
      key = prompt('Hackaburger API key (leave empty to use the offline simulation):');
      if (key) localStorage.setItem('hackaburger_api_key', key.trim());
    }
    return key ? key.trim() : null;
  }

  async function api(path, opts = {}) {
    const key = localStorage.getItem('hackaburger_api_key');
    const res = await fetch(API_BASE + path, {
      ...opts,
      headers: { 'X-API-Key': key || '', 'Content-Type': 'application/json', ...(opts.headers || {}) },
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
    });
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch (e) { /* not JSON */ }
      const err = new Error(`${res.status} ${typeof detail === 'string' ? detail : JSON.stringify(detail)}`);
      err.status = res.status;
      throw err;
    }
    return res.json();
  }

  // ------------------------------------------------------------ rooms

  function toPageRoom(r) {
    return {
      id: r.id,
      name: r.name,
      volume: r.volume_m3,
      occupancy: 0,
      scheduledCount: r.scheduled_count,
      co2: r.outdoor_ppm,
      windows: r.windows.map((w, i) => ({ id: i + 1, name: w.name, area: w.area_m2, isOpen: w.is_open })),
      isCustom: !live.seedIds.includes(r.id),
      device: r.device,
    };
  }

  function toServerFields(room) {
    return {
      name: room.name,
      volume_m3: room.volume,
      scheduled_count: Math.max(0, Math.round(room.scheduledCount || 0)),
      windows: (room.windows || []).map((w, i) => ({
        name: (w.name || `Window ${i + 1}`).slice(0, 64), area_m2: Math.max(0.1, Math.min(20, w.area || 1)),
      })),
    };
  }

  function applyServerRooms(rooms) {
    live.serverRooms = Object.fromEntries(rooms.map(r => [r.id, r]));
    const presets = rooms.filter(r => live.seedIds.includes(r.id)).map(toPageRoom);
    DEFAULT_ROOMS.splice(0, DEFAULT_ROOMS.length, ...presets);
    live.customs = rooms.filter(r => !live.seedIds.includes(r.id)).map(toPageRoom);
  }

  async function loadRooms() {
    applyServerRooms(await api('/rooms'));
  }

  // Custom rooms made before the backend existed live in localStorage: upload them once
  async function migrateLocalRooms() {
    let local = [];
    try { local = JSON.parse(localStorage.getItem('caelum_custom_rooms') || '[]'); } catch (e) { local = []; }
    const todo = (Array.isArray(local) ? local : []).filter(r => r && r.id && !live.serverRooms[r.id]);
    for (const r of todo) {
      const id = String(r.id).toLowerCase().replace(/[^a-z0-9_-]/g, '-').slice(0, 64);
      try { await api('/rooms', { method: 'POST', body: { id, ...toServerFields(r) } }); } catch (e) { console.warn('room upload failed', r.id, e); }
    }
    if (todo.length) await loadRooms();
    localStorage.removeItem('caelum_custom_rooms');
  }

  // The page calls getCustomRooms/saveCustomRooms for create, delete and edits of custom rooms
  const origGetCustomRooms = window.getCustomRooms;
  const origSaveCustomRooms = window.saveCustomRooms;
  window.getCustomRooms = function () {
    if (!live.enabled) return origGetCustomRooms();
    return JSON.parse(JSON.stringify(live.customs));
  };
  window.saveCustomRooms = function (rooms) {
    if (!live.enabled) return origSaveCustomRooms(rooms);
    const before = Object.fromEntries(live.customs.map(r => [r.id, r]));
    const after = Object.fromEntries(rooms.map(r => [r.id, r]));
    live.customs = JSON.parse(JSON.stringify(rooms));
    const jobs = [];
    for (const [id, room] of Object.entries(after)) {
      const fields = toServerFields(room);
      if (!before[id]) {
        jobs.push(api('/rooms', { method: 'POST', body: { id, ...fields } }));
      } else if (JSON.stringify(toServerFields(before[id])) !== JSON.stringify(fields)) {
        clearTimeout(live.pendingUpdates[id]);
        live.pendingUpdates[id] = setTimeout(() => {
          api(`/rooms/${encodeURIComponent(id)}`, { method: 'PUT', body: fields })
            .then(loadRooms).catch(e => showError(`Saving room failed: ${e.message}`));
        }, 600);
      }
    }
    for (const id of Object.keys(before)) {
      if (!after[id]) jobs.push(api(`/rooms/${encodeURIComponent(id)}`, { method: 'DELETE' }));
    }
    if (jobs.length) Promise.all(jobs).then(loadRooms).catch(e => showError(`Saving room failed: ${e.message}`));
  };

  // ------------------------------------------------------------ windows

  function syncWindows() {
    if (!live.enabled || live.forceSim || !live.serverRooms[STATE.currentRoomId]) return;
    const open = STATE.windows.map((w, i) => (w.isOpen ? i : -1)).filter(i => i >= 0);
    api(`/rooms/${encodeURIComponent(STATE.currentRoomId)}/windows`, { method: 'PUT', body: { open } })
      .then(() => poll())
      .catch(e => showError(`Saving windows failed: ${e.message}`));
  }

  ['toggleIndividualWindow', 'setOpenWindowsCount', 'closeAllWindowsAndStopTimer'].forEach(name => {
    const orig = window[name];
    window[name] = function (...args) {
      const result = orig.apply(this, args);
      syncWindows();
      return result;
    };
  });

  // Room switch: the page resets windows to closed; restore the server's state and refresh
  const origSwitchRoom = window.switchRoom;
  window.switchRoom = function (roomId) {
    origSwitchRoom(roomId);
    const server = live.serverRooms[STATE.currentRoomId];
    if (live.enabled && server && !live.forceSim) {
      STATE.windows.forEach((w, i) => { w.isOpen = Boolean(server.windows[i] && server.windows[i].is_open); });
      live.forecast = live.pastCo2 = live.pastOcc = live.board = null;
      live.devices = [];
      updateWindowControlsUI();
      poll();
    }
  };

  // ------------------------------------------------------------ live occupancy / CO2

  async function poll() {
    if (!live.enabled) return;
    const roomId = STATE.currentRoomId;
    const room = live.serverRooms[roomId];
    if (!room) { live.mode = 'sim'; renderBadge(); return; }
    const now = Date.now();
    const from = new Date(now - 2 * 3600 * 1000).toISOString();
    const weekday = localStorage.getItem('hackaburger_weekday');
    // as_weekday only works for rooms with enough history for the forecaster
    const useWeekday = weekday && room.history && room.history.enough_for_forecast;
    const fq = `/rooms/${encodeURIComponent(roomId)}/forecast?hours=24${useWeekday ? `&as_weekday=${weekday}` : ''}`;
    try {
      const [forecast, pastCo2, pastOcc, latest] = await Promise.all([
        api(fq),
        api(`/rooms/${encodeURIComponent(roomId)}/co2?from=${from}&step_s=900`),
        api(`/rooms/${encodeURIComponent(roomId)}/occupancy?from=${from}&step_s=900`),
        room.device ? api(`/data?device=${encodeURIComponent(room.device)}&limit=1`) : [],
      ]);
      // /occupancy/latest is 404 for a board that has never sent anything, so only ask once it has
      const board = latest.length ? await api(`/occupancy/latest?device=${encodeURIComponent(room.device)}`) : null;
      if (STATE.currentRoomId !== roomId) return; // switched while loading
      Object.assign(live, { forecast, pastCo2: pastCo2.points, pastOcc: pastOcc.points, board, error: null, lastUpdate: Date.now() });
      const devices = (latest[0] && latest[0].data && latest[0].data.devices) || [];
      if (latest[0] && latest[0].id !== live.lastReadingId) {
        live.lastReadingId = latest[0].id;
        live.devices = devices.slice().sort((a, b) => b.rssi - a.rssi);
        live.deviceCursor = 0;
      }
      const hasBoard = forecast.live_people !== null && board && board.online;
      const hasModel = forecast.model !== 'persistence_no_history';
      live.mode = live.forceSim ? 'sim' : hasBoard ? 'live' : hasModel ? 'model' : 'sim';
      if (live.mode !== 'sim') applyLiveState();
    } catch (e) {
      if (e.status === 401) {
        live.enabled = false;
        if (apiKey(true)) return start();
      }
      live.error = e.message;
      live.mode = 'sim';
    }
    renderBadge();
  }

  function applyLiveState() {
    const f = live.forecast;
    const room = live.serverRooms[STATE.currentRoomId];
    const people = f.live_people !== null ? f.live_people : f.points[0].people;
    STATE.occupancy = Math.max(0, Math.round(people));
    STATE.co2 = f.summary.co2_now;
    STATE.roomVolume = room.volume_m3;
    STATE.scheduledCount = room.scheduled_count;
    STATE.infiltrationACH = room.infiltration_ach;
    STATE.kFlow = room.k_flow;
    STATE.cOutdoor = room.outdoor_ppm;
    const until = f.summary.first_above_1000_at ? (Date.parse(f.summary.first_above_1000_at) - Date.now()) / 1000 : 0;
    STATE.countdownSeconds = Math.max(0, Math.round(until));

    const occSlider = document.getElementById('occSlider');
    const occLabel = document.getElementById('occValLabel');
    if (occSlider) occSlider.value = STATE.occupancy;
    if (occLabel) occLabel.innerText = `${STATE.occupancy} Humans`;
    const volSlider = document.getElementById('volSlider');
    const volLabel = document.getElementById('volValLabel');
    if (volSlider) volSlider.value = STATE.roomVolume;
    if (volLabel) volLabel.innerText = `${STATE.roomVolume} m³`;
    refreshUI();
  }

  // The simulation clock runs at 30x; in live/model mode the server's values are shown instead
  const origStep = window.stepSimulationTick;
  window.stepSimulationTick = function () {
    if (live.mode === 'sim' || STATE.isTimeWarpRunning) return origStep();
    if (live.forecast) STATE.co2 = live.forecast.summary.co2_now;
    if (!STATE.isWindowOpen && STATE.countdownSeconds > 0) STATE.countdownSeconds = Math.max(0, STATE.countdownSeconds - 2);
    updateTimerDisplayUI();
    refreshUI();
  };

  // Time-warp and the occupancy slider are simulation features: they switch this room to simulation
  ['toggleTimeWarpSimulation', 'resetToBaseline'].forEach(name => {
    const orig = window[name];
    window[name] = function (...args) {
      if (name === 'toggleTimeWarpSimulation' && live.mode !== 'sim') { live.forceSim = true; live.mode = 'sim'; renderBadge(); }
      if (name === 'resetToBaseline' && live.forceSim) { live.forceSim = false; setTimeout(poll, 0); }
      return orig.apply(this, args);
    };
  });

  // ------------------------------------------------------------ charts

  const origBuild = window.buildForecastArrays;
  window.buildForecastArrays = function () {
    if (live.mode === 'sim' || !live.forecast || !live.pastCo2) return origBuild();
    const f = live.forecast;
    const pick = key => FUTURE_MIN.map(min => {
      const p = f.points[Math.min(f.points.length - 1, Math.round(min / 5) - 1)];
      return p ? p[key] : null;
    });
    const past = live.pastCo2.slice(-9).map(p => p.co2_ppm);
    while (past.length < 9) past.unshift(null);
    past[8] = f.summary.co2_now;
    const now = f.summary.co2_now;
    const nulls = n => Array(n).fill(null);
    const series = {
      pastSeries: [...past, ...nulls(9)],
      groundTruthSeries: nulls(18), // no CO2 sensor
      forecastSeries: [...nulls(8), now, ...pick('co2_ppm')],
      upperSeries: [...nulls(8), now, ...pick('co2_upper')],
      lowerSeries: [...nulls(8), now, ...pick('co2_lower')],
      optimalSeries: [...nulls(8), now, ...pick('co2_windows_open')],
    };
    if (typeof co2Chart !== 'undefined' && co2Chart) {
      const all = Object.values(series).flat().filter(v => v !== null);
      co2Chart.options.scales.y.max = Math.max(2600, Math.ceil(Math.max(...all) * 1.1 / 100) * 100);
    }
    updateOccupancyChart();
    return series;
  };

  function updateOccupancyChart() {
    if (typeof occChart === 'undefined' || !occChart || !live.pastOcc || !live.forecast) return;
    const past = live.pastOcc.slice(-8).map(p => (p.people === null ? null : Math.round(p.people)));
    while (past.length < 8) past.unshift(null);
    const fut = FUTURE_MIN.map(min => {
      const p = live.forecast.points[Math.min(live.forecast.points.length - 1, Math.round(min / 5) - 1)];
      return p ? Math.round(p.people) : null;
    });
    const data = [...past, STATE.occupancy, ...fut];
    occChart.data.datasets[1].data = data;
    occChart.options.scales.y.max = Math.max(20, Math.ceil(Math.max(...data.filter(v => v !== null), STATE.scheduledCount) * 1.2 / 5) * 5);
  }

  // ------------------------------------------------------------ boards (admin)

  function setupRangeSelect() {
    const sel = document.getElementById('bleStrengthSelect');
    if (!sel) return;
    sel.innerHTML = RANGE_OPTIONS.map(([dbm, label]) => `<option value="${dbm} dBm">${dbm} dBm (${label})</option>`).join('');
  }

  async function loadBoardConfigs() {
    const nodes = getArduinoNodes();
    await Promise.all(nodes.map(async n => {
      try {
        const cfg = await api(`/config/${encodeURIComponent(n.id)}`);
        live.configs[n.id] = cfg;
        n.strength = `${cfg.threshold_dbm} dBm`;
        n.interval = cfg.interval_s;
        const sent = await api(`/data?device=${encodeURIComponent(n.id)}&limit=1`);
        const latest = sent.length ? await api(`/occupancy/latest?device=${encodeURIComponent(n.id)}`) : null;
        n.status = latest && latest.online ? 'ONLINE' : 'OFFLINE';
      } catch (e) { console.warn('board config', n.id, e); }
    }));
    saveArduinoNodes(nodes);
    syncArduinoUI();
  }

  function replaceDeployButton() {
    const btn = document.getElementById('deployBleConfigBtn');
    if (!btn) return;
    const fresh = btn.cloneNode(true); // drops the page's alert() listener
    btn.replaceWith(fresh);
    fresh.addEventListener('click', async () => {
      const node = getActiveArduinoNode();
      const threshold = parseInt(node.strength, 10);
      const body = { interval_s: Math.max(1, Math.min(60, Number(node.interval) || 5)) };
      if (Number.isInteger(threshold) && threshold >= -100 && threshold <= -30) body.threshold_dbm = threshold;
      try {
        const cfg = await api(`/config/${encodeURIComponent(node.id)}`, { method: 'PUT', body });
        live.configs[node.id] = cfg;
        showToast(`Sent to ${node.id}: range ${cfg.threshold_dbm} dBm, every ${cfg.interval_s}s (config v${cfg.config_version}). The board applies it on its next message.`);
      } catch (e) {
        showError(`Deploy failed: ${e.message}`);
      }
    });
  }

  // Packet log: show the board's real anonymised devices when it has sent some
  const origPush = window.pushBlePacketRow;
  window.pushBlePacketRow = function () {
    const node = getActiveArduinoNode();
    const room = live.serverRooms[STATE.currentRoomId];
    if (!live.enabled || !live.devices.length || !room || room.device !== node.id) return origPush();
    const stream = document.getElementById('blePacketStream');
    if (!stream) return;
    const d = live.devices[live.deviceCursor++ % live.devices.length];
    const threshold = (live.configs[node.id] || {}).threshold_dbm ?? -75;
    const accepted = d.rssi >= threshold;
    const row = document.createElement('div');
    row.className = 'px-2.5 py-1.5 flex items-center justify-between text-[11px] font-mono-tabular';
    row.innerHTML = `
      <span class="${accepted ? 'text-inkPrimary' : 'text-inkMuted'} font-mono-tabular">${d.id.slice(0, 8)}·${d.n}×</span>
      <span class="${accepted ? 'text-iceForecast font-medium' : 'text-inkMuted'} font-mono-tabular">${node.id} // ${Math.round(d.rssi)} dBm</span>
      <span class="text-[9px] px-1.5 py-0.2 rounded font-mono-tabular ${accepted
        ? 'bg-tealOptimal/10 text-tealOptimal border border-tealOptimal/25'
        : 'bg-canvas text-inkMuted border border-borderLine'}">${accepted ? 'IN-RANGE' : 'FILTERED'}</span>`;
    stream.prepend(row);
    while (stream.children.length > 14) stream.removeChild(stream.lastChild);
  };

  // ------------------------------------------------------------ status badge

  let badge, toastEl;
  function renderBadge() {
    if (!badge) {
      badge = document.createElement('div');
      badge.style.cssText = 'position:fixed;left:12px;bottom:12px;z-index:9999;max-width:calc(100vw - 24px);'
        + 'font:11px "IBM Plex Mono",monospace;background:#111317;color:#F3F4F6;border:1px solid #22262F;'
        + 'border-radius:6px;padding:6px 10px;display:flex;gap:10px;align-items:center;flex-wrap:wrap;box-shadow:0 4px 18px rgba(0,0,0,.5)';
      document.body.appendChild(badge);
      badge.addEventListener('click', e => {
        const action = e.target.dataset.action;
        if (action === 'toggle') { live.forceSim = !live.forceSim; if (!live.forceSim) STATE.isTimeWarpRunning = false; poll(); }
        if (action === 'weekday') {
          if (localStorage.getItem('hackaburger_weekday')) localStorage.removeItem('hackaburger_weekday');
          else localStorage.setItem('hackaburger_weekday', 'wed');
          poll();
        }
        if (action === 'connect') { apiKey(true) && start(); }
      });
    }
    const room = live.serverRooms[STATE.currentRoomId];
    const colours = { live: '#2DD4BF', model: '#60A5FA', sim: '#FBBF24' };
    let text;
    if (!live.enabled) {
      text = live.error ? `Backend unavailable (${live.error}) · simulation` : 'Offline simulation';
    } else if (live.mode === 'live') {
      text = `LIVE · ${room.device} · ${live.board.devices_in_range} devices in range`;
    } else if (live.mode === 'model') {
      text = `MODEL · ${room && room.device ? `${room.device} offline` : 'no board'} · forecast from history`;
    } else {
      text = live.forceSim ? 'SIMULATION (switched manually)' : live.error ? `SIMULATION · ${live.error}` : 'SIMULATION · no data for this room';
    }
    const ago = live.lastUpdate ? `${Math.round((Date.now() - live.lastUpdate) / 1000)}s ago` : '';
    const btn = (action, label) => `<button data-action="${action}" style="all:unset;cursor:pointer;color:#9CA3AF;text-decoration:underline">${label}</button>`;
    const weekday = localStorage.getItem('hackaburger_weekday');
    badge.innerHTML = `<span style="width:8px;height:8px;border-radius:50%;background:${live.enabled ? colours[live.mode] : '#F87171'}"></span>`
      + `<span>${text}</span>`
      + (live.enabled && live.mode !== 'sim' && ago ? `<span style="color:#6B7280">${ago}</span>` : '')
      + (live.enabled ? btn('toggle', live.forceSim ? 'use server data' : 'simulate') : btn('connect', 'connect'))
      + (live.enabled && live.mode !== 'sim' ? btn('weekday', weekday ? `pattern: ${weekday} (reset)` : 'use weekday pattern') : '');
  }

  function showToast(msg, isError) {
    if (!toastEl) {
      toastEl = document.createElement('div');
      toastEl.style.cssText = 'position:fixed;right:12px;bottom:12px;z-index:9999;max-width:min(420px,calc(100vw - 24px));'
        + 'font:12px "IBM Plex Mono",monospace;background:#111317;border:1px solid #22262F;border-radius:6px;padding:10px 12px;'
        + 'box-shadow:0 4px 18px rgba(0,0,0,.5);transition:opacity .3s';
      document.body.appendChild(toastEl);
    }
    toastEl.style.color = isError ? '#F87171' : '#F3F4F6';
    toastEl.textContent = msg;
    toastEl.style.opacity = '1';
    clearTimeout(toastEl._t);
    toastEl._t = setTimeout(() => { toastEl.style.opacity = '0'; }, 6000);
  }
  const showError = msg => showToast(msg, true);

  // ------------------------------------------------------------ start

  async function start() {
    if (!apiKey(false)) { live.enabled = false; renderBadge(); return; }
    try {
      await loadRooms();
      live.enabled = true;
      live.error = null;
      await migrateLocalRooms();
      setupRangeSelect();
      replaceDeployButton();
      const ids = DEFAULT_ROOMS.concat(live.customs).map(r => r.id);
      const current = ids.includes(STATE.currentRoomId) ? STATE.currentRoomId : ids[0];
      populateRoomSelector(current);
      window.switchRoom(current);
      loadBoardConfigs();
      clearInterval(live.polling);
      live.polling = setInterval(poll, POLL_MS);
      if (!live.badgeTimer) live.badgeTimer = setInterval(renderBadge, 1000);
    } catch (e) {
      live.enabled = false;
      live.error = e.message;
      if (e.status === 401 && apiKey(true)) return start();
      renderBadge();
    }
  }

  window.hackaburgerBackend = live; // for debugging in the console
  window.addEventListener('load', start);
})();
