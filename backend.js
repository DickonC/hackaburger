/* =====================================================================
   HACKABURGER BACKEND ADAPTER
   Connects index.html and admin.html to the backend (docs/backend-api.md)
   without breaking the simulation engine: it hooks Caelum and the UI.

   - Rooms come from /rooms (custom rooms are saved there, not localStorage)
   - Occupancy + CO2 + forecast come from the server for rooms with data
     (a live board or history); other rooms keep the simulation
   - Window toggles are saved with PUT /rooms/{id}/windows
   - "Deploy Config" sends the range threshold + interval to the board
   - The BLE packet log shows the board's real (anonymised) devices

   API key, without a prompt: open the site once as ?key=<API key> (saved in this browser, then
   removed from the address bar). Never commit the key. Other settings (localStorage): hackaburger_api_base,
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

  function getCaelum() { return window.Caelum; }
  function getState() { return (window.Caelum && window.Caelum.STATE) || window.STATE; }
  function getDefaultRooms() { return (window.Caelum && window.Caelum.DEFAULT_ROOMS) || window.DEFAULT_ROOMS || []; }

  const live = {
    enabled: false,      // backend reachable with a valid key
    forceSim: false,     // user switched back to the simulation
    mode: 'sim',         // 'live' | 'model' | 'sim' for the current room
    seedIds: (getDefaultRooms() || []).map(r => r.id),
    customs: [],         // server rooms that aren't presets, in the page's room shape
    serverRooms: {},     // id -> server room
    forecast: null, pastCo2: null, pastOcc: null, board: null, devices: [], deviceCursor: 0,
    configs: {},         // device -> board config
    heatmap: null,       // GET /heatmap/latest, when the room's device is "heatmap"
    heatmapConfig: null, // GET /heatmap/config
    heatmapBoard: null,  // shared board settings (enabled, display) from uno-q-1
    heatmapDebug: null,  // GET /heatmap/debug, only while the admin devices view is open
    debugOn: false,
    lastUpdate: 0, error: null, polling: null, pollMs: 0, debugTimer: null, badgeTimer: null,
    pendingUpdates: {}, // room id -> debounce timer for PUTs (sliders fire many changes)
  };

  // ------------------------------------------------------------ API

  // No prompt. The key comes from (1) a one-time link ?key=... (saved in this browser, then removed
  // from the address bar), (2) the key saved earlier in this browser, or (3) a config.json next to
  // the page if the server provides one. Without a key the page runs the offline simulation.
  let API_KEY = null;

  async function loadApiKey() {
    const params = new URLSearchParams(window.location.search);
    if (params.get('key')) {
      localStorage.setItem('hackaburger_api_key', params.get('key').trim());
      params.delete('key');
      const rest = params.toString();
      history.replaceState(null, '', window.location.pathname + (rest ? `?${rest}` : '') + window.location.hash);
    }
    const saved = localStorage.getItem('hackaburger_api_key');
    if (saved) return saved.trim();
    try {
      const res = await fetch('config.json', { cache: 'no-store' });
      if (res.ok) {
        const cfg = await res.json();
        if (cfg && cfg.apiKey) return String(cfg.apiKey).trim();
      }
    } catch (e) { /* opened from a file or another host */ }
    return null;
  }

  async function api(path, opts = {}) {
    const res = await fetch(API_BASE + path, {
      ...opts,
      headers: { 'X-API-Key': API_KEY || '', 'Content-Type': 'application/json', ...(opts.headers || {}) },
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

  // Preset = the page's own default rooms, or a room loaded from a dataset (not user-made, no delete button)
  const isPreset = r => live.seedIds.includes(r.id) || ((r.history && r.history.sources) || []).some(s => s.startsWith('aalborg'));

  function toPageRoom(r) {
    const seed = isPreset(r);
    return {
      id: r.id,
      name: r.name,
      shortName: (r.name || '').split('—').pop().trim() || r.name,
      volume: r.volume_m3,
      occupancy: 0,
      scheduledCount: r.scheduled_count,
      capacity: Math.max(2, Math.round((r.volume_m3 || 60) / 5.5), r.scheduled_count || 0),
      co2: r.outdoor_ppm || 420.0,
      windows: (r.windows || []).map((w, i) => ({ id: i + 1, name: w.name, area: w.area_m2, isOpen: Boolean(w.is_open) })),
      isCustom: !seed,
      device: r.device,
    };
  }

  function toServerFields(room) {
    return {
      name: room.name,
      volume_m3: room.volume,
      scheduled_count: Math.max(0, Math.round(room.scheduledCount || 0)),
      windows: (room.windows || []).map((w, i) => ({
        name: (w.name || `Window ${i + 1}`).slice(0, 64),
        area_m2: Math.max(0.1, Math.min(20, w.area || 1)),
      })),
    };
  }

  function applyServerRooms(rooms) {
    live.serverRooms = Object.fromEntries(rooms.map(r => [r.id, r]));
    const presets = rooms.filter(isPreset).map(toPageRoom);
    const defRooms = getDefaultRooms();
    if (defRooms && presets.length) {
      defRooms.splice(0, defRooms.length, ...presets);
    }
    live.customs = rooms.filter(r => !isPreset(r)).map(toPageRoom);
    const Caelum = getCaelum();
    if (Caelum && typeof Caelum.changed === 'function') {
      Caelum.changed('rooms');
    }
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

  // Hook getCustomRooms and saveCustomRooms on Caelum and window
  const Caelum = getCaelum();
  const origGetCustomRooms = (Caelum && Caelum.getCustomRooms) || window.getCustomRooms;
  const origSaveCustomRooms = (Caelum && Caelum.saveCustomRooms) || window.saveCustomRooms;

  function getCustomRoomsImpl() {
    if (!live.enabled) return origGetCustomRooms ? origGetCustomRooms() : [];
    return JSON.parse(JSON.stringify(live.customs));
  }

  function saveCustomRoomsImpl(rooms) {
    if (!live.enabled) {
      if (origSaveCustomRooms) origSaveCustomRooms(rooms);
      return;
    }
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
  }

  if (Caelum) {
    Caelum.getCustomRooms = getCustomRoomsImpl;
    Caelum.saveCustomRooms = saveCustomRoomsImpl;
  }
  window.getCustomRooms = getCustomRoomsImpl;
  window.saveCustomRooms = saveCustomRoomsImpl;

  // ------------------------------------------------------------ windows

  function syncWindows() {
    const STATE = getState();
    if (!live.enabled || live.forceSim || !STATE || !live.serverRooms[STATE.currentRoomId]) return;
    const open = (STATE.windows || []).map((w, i) => (w.isOpen ? i : -1)).filter(i => i >= 0);
    api(`/rooms/${encodeURIComponent(STATE.currentRoomId)}/windows`, { method: 'PUT', body: { open } })
      .then(() => poll())
      .catch(e => showError(`Saving windows failed: ${e.message}`));
  }

  if (Caelum) {
    ['setOpenWindowsCount', 'closeAllWindows', 'toggleWindow', 'toggleWindowsDefault', 'requestOpenWindows'].forEach(name => {
      const orig = Caelum[name];
      if (typeof orig === 'function') {
        Caelum[name] = function (...args) {
          const res = orig.apply(this, args);
          syncWindows();
          return res;
        };
      }
    });
  }

  ['toggleIndividualWindow', 'setOpenWindowsCount', 'closeAllWindowsAndStopTimer'].forEach(name => {
    if (typeof window[name] === 'function') {
      const orig = window[name];
      window[name] = function (...args) {
        const result = orig.apply(this, args);
        syncWindows();
        return result;
      };
    }
  });

  // Room switch: restore the server's state and refresh
  function onRoomSwitched(roomId) {
    const STATE = getState();
    if (!STATE) return;
    const server = live.serverRooms[STATE.currentRoomId];
    if (live.enabled && server && !live.forceSim) {
      if (STATE.windows) {
        STATE.windows.forEach((w, i) => { w.isOpen = Boolean(server.windows[i] && server.windows[i].is_open); });
      }
      live.forecast = live.pastCo2 = live.pastOcc = live.board = null;
      live.devices = [];
      const updateWin = window.updateWindowControlsUI || (typeof updateWindowControlsUI === 'function' ? updateWindowControlsUI : null);
      if (updateWin) updateWin();
      poll();
    }
  }

  if (Caelum && typeof Caelum.switchRoom === 'function') {
    const origCaelumSwitch = Caelum.switchRoom;
    Caelum.switchRoom = function (roomId) {
      origCaelumSwitch.call(Caelum, roomId);
      onRoomSwitched(roomId);
    };
  }
  if (typeof window.switchRoom === 'function') {
    const origWinSwitch = window.switchRoom;
    window.switchRoom = function (roomId) {
      origWinSwitch(roomId);
      onRoomSwitched(roomId);
    };
  } else {
    window.switchRoom = function (roomId) {
      if (Caelum && typeof Caelum.switchRoom === 'function') Caelum.switchRoom(roomId);
      else onRoomSwitched(roomId);
    };
  }

  // ------------------------------------------------------------ live occupancy / CO2

  function ensurePoll(ms) {
    if (live.polling && live.pollMs === ms) return;
    live.pollMs = ms;
    clearInterval(live.polling);
    live.polling = setInterval(poll, ms);
  }

  async function pollDebug() {
    if (!live.debugOn || !live.enabled) return;
    try {
      live.heatmapDebug = await api('/heatmap/debug');
      live.debugError = null;
    } catch (e) {
      live.debugError = e.message;
    }
  }

  function setHeatmapDebug(on) {
    on = !!on;
    if (on === live.debugOn) return;
    live.debugOn = on;
    clearInterval(live.debugTimer);
    live.debugTimer = null;
    if (!on) {
      live.heatmapDebug = null;
      return;
    }
    pollDebug();
    live.debugTimer = setInterval(pollDebug, 5000);
  }

  async function putHeatmap(path, body, method) {
    const result = await api(path, { method: method || 'PUT', body });
    poll();
    return result;
  }

  live.setHeatmapDebug = setHeatmapDebug;
  live.putHeatmapConfig = (body) => putHeatmap('/heatmap/config', body);
  live.putHeatmapMode = (mode) => putHeatmap('/heatmap/mode', { mode });
  live.putHeatmapBoards = (body) => putHeatmap('/heatmap/boards', body);
  live.calibrateHeadcount = (people) => putHeatmap('/heatmap/calibrate/headcount', { people }, 'POST');

  async function poll() {
    if (!live.enabled) return;
    const Caelum = getCaelum();
    // In room view, if linked to the admin console, let the admin broadcast drive the view
    if (Caelum && typeof Caelum.getRole === 'function' && Caelum.getRole() === 'room' && Caelum.getLink() === 'admin') {
      renderBadge();
      return;
    }

    const STATE = getState();
    if (!STATE) return;
    const roomId = STATE.currentRoomId;
    const room = live.serverRooms[roomId];
    if (!room) { live.mode = 'sim'; renderBadge(); return; }
    const now = Date.now();
    const from = new Date(now - 2 * 3600 * 1000).toISOString();
    const heatRoom = room.device === 'heatmap';
    try {
      // Live rooms have no server forecast (forecasting runs on the dataset rooms, /dataset/...):
      // the "now" values come from the CO2 estimate, and the future comes from the page's own engine.
      // A room with device "heatmap" takes its people from the four corner boards.
      const [pastCo2, pastOcc, third, heatCfg, heatBoard] = await Promise.all([
        api(`/rooms/${encodeURIComponent(roomId)}/co2?from=${from}&step_s=900`),
        api(`/rooms/${encodeURIComponent(roomId)}/occupancy?from=${from}&step_s=900`),
        heatRoom ? api('/heatmap/latest')
          : room.device ? api(`/data?device=${encodeURIComponent(room.device)}&limit=1`)
            : [],
        heatRoom ? api('/heatmap/config') : null,
        heatRoom ? api('/config/uno-q-1').catch(() => null) : null,
      ]);
      const latest = heatRoom ? [] : third;
      const board = !heatRoom && latest.length
        ? await api(`/occupancy/latest?device=${encodeURIComponent(room.device)}`)
        : null;
      const forecast = { live_people: pastCo2.now.people, model: heatRoom ? 'heatmap' : 'live_board', points: [],
                         summary: { co2_now: pastCo2.now.co2_ppm, first_above_1000_at: null } };
      if (STATE.currentRoomId !== roomId) return; // switched while loading
      Object.assign(live, {
        forecast, pastCo2: pastCo2.points, pastOcc: pastOcc.points, board, error: null, lastUpdate: Date.now(),
        heatmap: heatRoom ? third : null,
        heatmapConfig: heatRoom ? heatCfg : null,
        heatmapBoard: heatRoom && heatBoard ? heatBoard : live.heatmapBoard,
      });
      if (!heatRoom) {
        const devices = (latest[0] && latest[0].data && latest[0].data.devices) || [];
        if (latest[0] && latest[0].id !== live.lastReadingId) {
          live.lastReadingId = latest[0].id;
          live.devices = devices.slice().sort((a, b) => b.rssi - a.rssi);
          live.deviceCursor = 0;
        }
      } else {
        live.devices = [];
        if (live.debugOn) pollDebug();
      }
      const hasBoard = heatRoom || (forecast.live_people !== null && board && board.online);
      live.mode = live.forceSim ? 'sim' : hasBoard ? 'live' : 'sim';
      if (live.mode !== 'sim') applyLiveState();
      ensurePoll(heatRoom && !live.forceSim && live.heatmap && live.heatmap.mode === 'dev' ? 60000 : POLL_MS);
    } catch (e) {
      if (e.status === 401) live.enabled = false;
      live.error = e.status === 401 ? 'API key rejected' : e.message;
      live.mode = 'sim';
    }
    renderBadge();
  }

  function applyLiveState() {
    const STATE = getState();
    const f = live.forecast;
    if (!STATE || !f) return;
    const room = live.serverRooms[STATE.currentRoomId];
    if (!room) return;
    const heatPeople = live.heatmap && live.heatmap.people_smoothed;
    const people = heatPeople != null ? heatPeople : (f.live_people !== null ? f.live_people : (f.points && f.points[0] ? f.points[0].people : 0));
    STATE.occupancy = Math.max(0, Math.round(people));
    STATE.co2 = f.summary.co2_now;
    STATE.roomVolume = room.volume_m3;
    STATE.scheduledCount = room.scheduled_count;
    if (room.capacity) STATE.capacity = room.capacity;
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

    const Caelum = getCaelum();
    if (Caelum && typeof Caelum.changed === 'function') {
      Caelum.changed('backend');
    }
    const refresh = window.refreshUI || (typeof refreshUI === 'function' ? refreshUI : null);
    if (refresh) refresh();
  }

  // Simulation step hook for backward compatibility
  const origStep = window.stepSimulationTick;
  if (typeof origStep === 'function') {
    window.stepSimulationTick = function () {
      const STATE = getState();
      if (live.mode === 'sim' || (STATE && STATE.isTimeWarpRunning)) return origStep();
      if (live.forecast && STATE) STATE.co2 = live.forecast.summary.co2_now;
      if (STATE && !STATE.isWindowOpen && STATE.countdownSeconds > 0) STATE.countdownSeconds = Math.max(0, STATE.countdownSeconds - 2);
      const updateTimer = window.updateTimerDisplayUI || (typeof updateTimerDisplayUI === 'function' ? updateTimerDisplayUI : null);
      if (updateTimer) updateTimer();
      const refresh = window.refreshUI || (typeof refreshUI === 'function' ? refreshUI : null);
      if (refresh) refresh();
    };
  }

  // Simulation demo / time-warp modes switch to sim
  if (Caelum && Caelum.demo) {
    const origGuidedStart = Caelum.demo.guidedStart;
    if (typeof origGuidedStart === 'function') {
      Caelum.demo.guidedStart = function (...args) {
        live.forceSim = true;
        live.mode = 'sim';
        renderBadge();
        return origGuidedStart.apply(this, args);
      };
    }
    const origWarpToggle = Caelum.demo.warpToggle;
    if (typeof origWarpToggle === 'function') {
      Caelum.demo.warpToggle = function (...args) {
        live.forceSim = true;
        live.mode = 'sim';
        renderBadge();
        return origWarpToggle.apply(this, args);
      };
    }
    const origReset = Caelum.resetToBaseline;
    if (typeof origReset === 'function') {
      Caelum.resetToBaseline = function (...args) {
        if (live.forceSim) {
          live.forceSim = false;
          setTimeout(poll, 0);
        }
        return origReset.apply(this, args);
      };
    }
  }

  ['toggleTimeWarpSimulation', 'resetToBaseline'].forEach(name => {
    if (typeof window[name] === 'function') {
      const orig = window[name];
      window[name] = function (...args) {
        if (name === 'toggleTimeWarpSimulation' && live.mode !== 'sim') { live.forceSim = true; live.mode = 'sim'; renderBadge(); }
        if (name === 'resetToBaseline' && live.forceSim) { live.forceSim = false; setTimeout(poll, 0); }
        return orig.apply(this, args);
      };
    }
  });

  // ------------------------------------------------------------ charts

  function buildBackendForecastArrays() {
    const Caelum = getCaelum();
    if (live.mode === 'sim' || !live.forecast || !live.pastCo2) {
      return origBuild ? origBuild() : (origCaelumBuild ? origCaelumBuild.call(Caelum) : null);
    }
    const f = live.forecast;
    if (!f.points.length) {
      // No server forecast: the engine projects forward from the live state; show the server's past
      const series = origBuild ? origBuild() : (origCaelumBuild ? origCaelumBuild.call(Caelum) : null);
      if (series) {
        const past = live.pastCo2.slice(-9).map(p => p.co2_ppm);
        while (past.length < 9) past.unshift(null);
        past[8] = f.summary.co2_now;
        series.pastSeries = [...past, ...Array(9).fill(null)];
      }
      updateOccupancyChart();
      return series;
    }
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
      forecastSeries: [...nulls(8), now, ...pick('co2_ppm')],
      upperSeries: [...nulls(8), now, ...pick('co2_upper')],
      lowerSeries: [...nulls(8), now, ...pick('co2_lower')],
      optimalSeries: [...nulls(8), now, ...pick('co2_windows_open')],
    };
    const chart = window.co2Chart || (typeof co2Chart !== 'undefined' ? co2Chart : null);
    if (chart && chart.options && chart.options.scales && chart.options.scales.y) {
      const all = Object.values(series).flat().filter(v => v !== null);
      chart.options.scales.y.max = Math.max(2600, Math.ceil(Math.max(...all) * 1.1 / 100) * 100);
    }
    updateOccupancyChart();
    return series;
  }

  const origBuild = window.buildForecastArrays;
  const origCaelumBuild = Caelum ? Caelum.buildForecastArrays : null;
  if (Caelum) Caelum.buildForecastArrays = buildBackendForecastArrays;
  window.buildForecastArrays = buildBackendForecastArrays;

  function updateOccupancyChart() {
    const occ = window.occChart || (typeof occChart !== 'undefined' ? occChart : null);
    if (!occ || !live.pastOcc || !live.forecast) return;
    const past = live.pastOcc.slice(-8).map(p => (p.people === null ? null : Math.round(p.people)));
    while (past.length < 8) past.unshift(null);
    const fut = FUTURE_MIN.map(min => {
      const p = live.forecast.points[Math.min(live.forecast.points.length - 1, Math.round(min / 5) - 1)];
      return p ? Math.round(p.people) : null;
    });
    const STATE = getState();
    const actual = [...past, STATE.occupancy, ...fut.map(() => null)];
    const predicted = [...past.map(() => null), STATE.occupancy, ...fut];
    if (occ.data && occ.data.datasets && occ.data.datasets[1]) {
      occ.data.datasets[1].data = actual;
      if (occ.data.datasets[2]) occ.data.datasets[2].data = predicted;
      const nums = actual.concat(predicted).filter(v => v !== null);
      const peak = Math.max(...nums, STATE.scheduledCount || 0);
      occ.options.scales.y.max = Math.max(12, Math.ceil((peak + 1) / 5) * 5);
      occ.update('none');
    }
  }

  // Forecast timeline for Room View (index.html)
  if (Caelum && typeof Caelum.forecastTimeline === 'function') {
    const origTimeline = Caelum.forecastTimeline;
    Caelum.forecastTimeline = function (horizonMin, stepMin) {
      if (live.mode === 'sim' || !live.forecast || !live.forecast.points || !live.forecast.points.length) {
        return origTimeline.call(Caelum, horizonMin, stepMin);
      }
      horizonMin = horizonMin || 240;
      stepMin = stepMin || 5;
      const nowTod = Caelum.clockMinutesOfDay();
      const f = live.forecast;
      const points = f.points;
      const STATE = getState();
      const out = [];
      out.push({ t: 0, tod: nowTod, co2: f.summary.co2_now, occ: STATE.occupancy, flushing: STATE.isWindowOpen });
      for (let t = stepMin; t <= horizonMin; t += stepMin) {
        const idx = Math.min(points.length - 1, Math.max(0, Math.round(t / 5) - 1));
        const p = points[idx];
        out.push({
          t: t,
          tod: (nowTod + t) % 1440,
          co2: p ? p.co2_ppm : f.summary.co2_now,
          occ: p ? Math.round(p.people) : STATE.occupancy,
          flushing: p ? (p.co2_windows_open < p.co2_ppm * 0.9) : false
        });
      }
      return out;
    };
  }

  // ------------------------------------------------------------ boards (admin)

  function setupRangeSelect() {
    const sel = document.getElementById('bleStrengthSelect');
    if (!sel) return;
    sel.innerHTML = RANGE_OPTIONS.map(([dbm, label]) => `<option value="${dbm} dBm">${dbm} dBm (${label})</option>`).join('');
  }

  async function loadBoardConfigs() {
    const getNodes = window.getArduinoNodes || (typeof getArduinoNodes === 'function' ? getArduinoNodes : null);
    const saveNodes = window.saveArduinoNodes || (typeof saveArduinoNodes === 'function' ? saveArduinoNodes : null);
    const syncUI = window.syncArduinoUI || (typeof syncArduinoUI === 'function' ? syncArduinoUI : null);
    if (!getNodes) return;
    const nodes = getNodes();
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
    if (saveNodes) saveNodes(nodes);
    if (syncUI) syncUI();
  }

  function replaceDeployButton() {
    const btn = document.getElementById('deployBleConfigBtn');
    if (!btn) return;
    const getNode = window.getActiveArduinoNode || (typeof getActiveArduinoNode === 'function' ? getActiveArduinoNode : null);
    const fresh = btn.cloneNode(true);
    btn.replaceWith(fresh);
    fresh.addEventListener('click', async () => {
      if (!getNode) return;
      const node = getNode();
      if (!node) return;
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
  const origPush = window.pushBlePacketRow || (typeof pushBlePacketRow === 'function' ? pushBlePacketRow : null);
  function pushBackendBlePacketRow() {
    const getNode = window.getActiveArduinoNode || (typeof getActiveArduinoNode === 'function' ? getActiveArduinoNode : null);
    const STATE = getState();
    const node = getNode ? getNode() : null;
    const room = STATE ? live.serverRooms[STATE.currentRoomId] : null;
    if (live.enabled && room && room.device === 'heatmap' && live.mode === 'live') return;
    if (!live.enabled || !live.devices.length || !room || !node || room.device !== node.id) {
      if (origPush) return origPush();
      return;
    }
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
  }
  window.pushBlePacketRow = pushBackendBlePacketRow;

  // ------------------------------------------------------------ status badge

  let badge, toastEl;
  function renderBadge() {
    if (!badge) {
      badge = document.createElement('div');
      badge.style.cssText = 'position:fixed;left:12px;bottom:12px;z-index:40;max-width:calc(100vw - 24px);'
        + 'font:600 12px/1.3 "JetBrains Mono",ui-monospace,monospace;background:rgba(255,255,255,.94);color:#0F172A;border:1px solid #E2E8F0;'
        + 'border-radius:999px;padding:8px 12px;display:flex;gap:10px;align-items:center;flex-wrap:wrap;'
        + 'box-shadow:0 1px 2px rgba(15,23,42,.04),0 8px 24px -12px rgba(15,23,42,.18)';
      document.body.appendChild(badge);
      badge.addEventListener('click', e => {
        const action = e.target.dataset.action;
        if (action === 'toggle') {
          live.forceSim = !live.forceSim;
          const STATE = getState();
          if (STATE && !live.forceSim) STATE.isTimeWarpRunning = false;
          poll();
        }
        if (action === 'weekday') {
          if (localStorage.getItem('hackaburger_weekday')) localStorage.removeItem('hackaburger_weekday');
          else localStorage.setItem('hackaburger_weekday', 'wed');
          poll();
        }
        if (action === 'connect') { API_KEY = null; start(); }
      });
    }
    const STATE = getState();
    const room = STATE ? live.serverRooms[STATE.currentRoomId] : null;
    const colours = { live: '#22C55E', model: '#0EA5E9', sim: '#F59E0B' };
    let text;
    if (!live.enabled) {
      text = live.error ? `Backend unavailable (${live.error}) · simulation` : 'Offline simulation';
    } else if (live.mode === 'live' && room && room.device === 'heatmap') {
      const n = live.heatmap && live.heatmap.people_smoothed;
      const devs = live.heatmap && live.heatmap.devices_in_room;
      text = `LIVE · heatmap · ${n == null ? 'waiting' : n + ' people'}${devs == null ? '' : ' · ' + devs + ' devices'}`;
    } else if (live.mode === 'live') {
      text = `LIVE · ${room && room.device ? room.device : 'board'} · ${live.board ? live.board.devices_in_range : 0} devices in range`;
    } else if (live.mode === 'model') {
      text = `MODEL · ${room && room.device ? `${room.device} offline` : 'no board'} · forecast from history`;
    } else {
      text = live.forceSim ? 'SIMULATION (switched manually)' : live.error ? `SIMULATION · ${live.error}` : 'SIMULATION · no data for this room';
    }
    const ago = live.lastUpdate ? `${Math.round((Date.now() - live.lastUpdate) / 1000)}s ago` : '';
    const btn = (action, label) => `<button data-action="${action}" style="all:unset;cursor:pointer;color:#0369A1;font-weight:600">${label}</button>`;
    const weekday = localStorage.getItem('hackaburger_weekday');
    badge.innerHTML = `<span style="width:8px;height:8px;border-radius:50%;background:${live.enabled ? colours[live.mode] : '#EF4444'}"></span>`
      + `<span>${text}</span>`
      + (live.enabled && live.mode !== 'sim' && ago ? `<span style="color:#64748B">${ago}</span>` : '')
      + (live.enabled ? btn('toggle', live.forceSim ? 'use server data' : 'simulate') : btn('connect', 'connect'))
      + (live.enabled && live.mode !== 'sim' ? btn('weekday', weekday ? `pattern: ${weekday} (reset)` : 'use weekday pattern') : '');

    // Synchronize the Room View header badge if on index.html
    const headerBadge = document.querySelector('.badge-sim');
    if (headerBadge) {
      if (live.enabled && live.mode === 'live') {
        const deviceLabel = room && room.device && room.device !== 'heatmap' ? ` (${room.device.toUpperCase()})` : '';
        headerBadge.innerHTML = `<i style="background:#22C55E"></i>LIVE${deviceLabel}`;
        headerBadge.title = 'Live occupancy from Bluetooth Low Energy scanner';
      } else if (live.enabled && live.mode === 'model') {
        headerBadge.innerHTML = `<i style="background:#0EA5E9"></i>MODEL`;
        headerBadge.title = 'Forecast derived from 7-30 day historical machine learning model';
      } else {
        headerBadge.innerHTML = `<i style="background:#F59E0B"></i>SIMULATED`;
        headerBadge.title = 'Values are produced by the local simulation engine';
      }
    }
  }

  function showToast(msg, isError) {
    if (!toastEl) {
      toastEl = document.createElement('div');
      toastEl.style.cssText = 'position:fixed;right:12px;bottom:12px;z-index:40;max-width:min(420px,calc(100vw - 24px));'
        + 'font:500 13px/1.4 "General Sans",system-ui,sans-serif;background:#fff;border:1px solid #E2E8F0;border-radius:14px;padding:12px 14px;'
        + 'box-shadow:0 8px 24px -12px rgba(15,23,42,.28);transition:opacity .3s';
      document.body.appendChild(toastEl);
    }
    toastEl.style.color = isError ? '#B91C1C' : '#0F172A';
    toastEl.textContent = msg;
    toastEl.style.opacity = '1';
    clearTimeout(toastEl._t);
    toastEl._t = setTimeout(() => { toastEl.style.opacity = '0'; }, 6000);
  }
  const showError = msg => showToast(msg, true);

  // ------------------------------------------------------------ start

  async function start() {
    if (!API_KEY) API_KEY = await loadApiKey();
    if (!API_KEY) { live.enabled = false; live.error = 'no API key'; renderBadge(); return; }
    try {
      await loadRooms();
      live.enabled = true;
      live.error = null;
      await migrateLocalRooms();
      setupRangeSelect();
      replaceDeployButton();
      const STATE = getState();
      const defRooms = getDefaultRooms();
      const ids = defRooms.concat(live.customs).map(r => r.id);
      const current = (STATE && ids.includes(STATE.currentRoomId)) ? STATE.currentRoomId : ids[0];
      const popRoom = window.populateRoomSelector || (typeof populateRoomSelector === 'function' ? populateRoomSelector : null);
      if (popRoom) popRoom(current);
      const Caelum = getCaelum();
      if (Caelum && typeof Caelum.switchRoom === 'function') Caelum.switchRoom(current);
      else if (typeof window.switchRoom === 'function') window.switchRoom(current);
      loadBoardConfigs();
      ensurePoll(POLL_MS);
      if (!live.badgeTimer) live.badgeTimer = setInterval(renderBadge, 1000);
      poll();
    } catch (e) {
      live.enabled = false;
      live.error = e.status === 401 ? 'API key rejected' : e.message;
      renderBadge();
    }
  }

  window.hackaburgerBackend = live; // for debugging in the console
  if (document.readyState === 'loading') {
    window.addEventListener('load', start);
  } else {
    setTimeout(start, 0);
  }
})();
