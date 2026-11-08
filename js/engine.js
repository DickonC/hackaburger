/* =========================================================================
   CAELUM SHARED ENGINE  (js/engine.js)
   -------------------------------------------------------------------------
   Plain script, no build step, NO DOM access. Exposes a single global:
     window.Caelum

   Used by:
     admin.html  -> role 'admin'  : authority. Runs the simulation clock,
                                    demo modes, and broadcasts state.
     index.html  -> role 'room'   : follower. Renders admin broadcasts; if no
                                    admin is found within ~3 s it runs its
                                    own local simulation (standalone).

   Sync: BroadcastChannel('caelum'). Admin -> {type:'state', state:{...}}.
         Room  -> {type:'hello'} and {type:'cmd', action:..., ...}.
   ========================================================================= */
(function (root) {
  'use strict';

  var CHANNEL_NAME = 'caelum';
  var TICK_MS = 2000;            // background tick every 2 s (real time; paused 8-hour day: 1 sim-minute)
  var FLUSH_TARGET_PPM = 800;    // "recovered" level used to size a window flush
  var STUFFY_PPM = 1000;         // threshold used for the countdown / alerts
  var GUIDED_ROOM_ID = 'forum';  // guided demo runs in the meeting room preset
  var LINK_CONNECT_MS = 3000;    // room view waits this long for an admin
  var LINK_LOST_MS = 6500;       // ... and falls back to standalone after this

  /* =====================================================================
     0. PRESET ROOMS + LOCAL STORAGE ZONE PERSISTENCE (all guarded)
     ===================================================================== */
  var DEFAULT_ROOMS = [
    {
      id: "appleton",
      name: "Appleton Tower — Study Pod 4.02",
      shortName: "Study Pod 4.02",
      volume: 65.0,
      width: 5.0,            // m, wall from board 1 to board 2
      length: 5.2,           // m, wall from board 1 to board 4
      height: 2.5,
      occupancy: 8,
      scheduledCount: 12,
      capacity: 12,
      co2: 1280.0,
      windows: [
        { id: 1, name: "Win 01 (North)", area: 1.0, isOpen: false },
        { id: 2, name: "Win 02 (North)", area: 1.0, isOpen: false }
      ],
      isCustom: false
    },
    {
      id: "forum",
      name: "Informatics Forum — Meeting Room 2.14",
      shortName: "Meeting Room 2.14",
      volume: 140.0,
      width: 8.0,
      length: 7.0,
      height: 2.5,
      occupancy: 15,
      scheduledCount: 20,
      capacity: 20,
      co2: 1410.0,
      windows: [
        { id: 1, name: "North Win 1", area: 1.2, isOpen: false },
        { id: 2, name: "North Win 2", area: 1.2, isOpen: false },
        { id: 3, name: "East Win 1", area: 1.0, isOpen: false },
        { id: 4, name: "East Win 2", area: 1.0, isOpen: false }
      ],
      isCustom: false
    },
    {
      id: "personal",
      name: "Personal Study Mode — Dorm / Bedroom",
      shortName: "Personal Study",
      volume: 32.0,
      width: 4.0,
      length: 3.2,
      height: 2.5,
      occupancy: 1,
      scheduledCount: 1,
      capacity: 2,
      co2: 980.0,
      windows: [
        { id: 1, name: "Bedroom Sash Win", area: 0.8, isOpen: false }
      ],
      isCustom: false
    },
    {
      id: "live",
      name: "Live Room",
      shortName: "Live Room",
      volume: 120.0,
      width: 8.0,
      length: 6.0,
      height: 2.5,
      occupancy: 0,
      scheduledCount: 0,
      capacity: 24,
      co2: 480.0,
      windows: [
        { id: 1, name: "Window 1", area: 1.2, isOpen: false },
        { id: 2, name: "Window 2", area: 1.2, isOpen: false }
      ],
      isCustom: false
    }
  ];
  var LIVE_ROOM_ID = "live";

  function lsGet(key) {
    try { return root.localStorage ? root.localStorage.getItem(key) : null; }
    catch (e) { return null; }
  }
  function lsSet(key, val) {
    try { if (root.localStorage) root.localStorage.setItem(key, val); }
    catch (e) { /* storage blocked / full: ignore */ }
  }

  function getCustomRooms() {
    try {
      var raw = lsGet('caelum_custom_rooms');
      if (!raw) return [];
      var parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? parsed : [];
    } catch (e) {
      return [];
    }
  }

  function saveCustomRooms(rooms) {
    lsSet('caelum_custom_rooms', JSON.stringify(rooms));
  }

  function getAllRooms() {
    var customs = (root.Caelum && root.Caelum.getCustomRooms) ? root.Caelum.getCustomRooms() : getCustomRooms();
    return DEFAULT_ROOMS.concat(customs);
  }

  function roomShortName(room) {
    if (room.shortName) return room.shortName;
    var parts = String(room.name || '').split('—');
    return (parts[parts.length - 1] || room.name || 'Room').trim();
  }

  function roomCapacity(room) {
    if (room.capacity) return room.capacity;
    return Math.max(2, Math.round((room.volume || 60) / 5.5), room.scheduledCount || 0);
  }

  function round1(n) {
    return Math.round(n * 10) / 10;
  }

  function deriveSize(volume) {
    var height = 2.5;
    var footprint = Math.max(1, (volume || 65) / height);
    var width = Math.sqrt(footprint * 1.25);
    var length = footprint / width;
    return { width: round1(width), length: round1(length), height: height };
  }

  var ROOM_SIZES_KEY = 'caelum_room_sizes';
  function loadRoomSizes() {
    try {
      var raw = lsGet(ROOM_SIZES_KEY);
      if (!raw) return {};
      var parsed = JSON.parse(raw);
      return parsed && typeof parsed === 'object' ? parsed : {};
    } catch (e) { return {}; }
  }
  function persistRoomSizes() {
    lsSet(ROOM_SIZES_KEY, JSON.stringify(roomSizes));
  }
  var roomSizes = loadRoomSizes();
  var baseSizes = {};
  DEFAULT_ROOMS.forEach(function (r) {
    baseSizes[r.id] = { width: r.width, length: r.length, height: r.height, volume: r.volume };
  });

  function sizeForRoom(room) {
    if (room && roomSizes[room.id]) return roomSizes[room.id];
    if (room && isFinite(room.width) && isFinite(room.length) && isFinite(room.height)) {
      return { width: +room.width, length: +room.length, height: +room.height };
    }
    if (room && baseSizes[room.id]) {
      return { width: baseSizes[room.id].width, length: baseSizes[room.id].length, height: baseSizes[room.id].height };
    }
    return deriveSize(room && room.volume);
  }

  /* =====================================================================
     1. CORE SYSTEM STATE (identical shape to the original dashboard)
     ===================================================================== */
  var STATE = {
    currentRoomId: "appleton",
    roomName: DEFAULT_ROOMS[0].name,
    roomVolume: 65.0,          // V in m³ (width × length × height)
    roomWidth: 5.0,            // m, wall from board 1 to board 2
    roomLength: 5.2,           // m, wall from board 1 to board 4
    roomHeight: 2.5,           // m
    capacity: 12,              // seats (for the room-view crowd gauge)
    occupancy: 8,              // N(t) live filtered BLE humans
    scheduledCount: 12,        // Room calendar scheduled headcount
    co2GenPerPerson: 16.0,     // L/h per person (16 sedentary, 24 presenting)
    cOutdoor: 420.0,           // Outdoor baseline ppm
    infiltrationACH: 0.25,     // Windows closed baseline air changes/hr
    kFlow: 350.0,              // Empirical open window flow constant m³/(h·m²)
    arduinoBleInterval: 2.4,   // Admin-adjustable BLE packet interval (seconds)

    // Dynamic array of windows with individual size (m²) and open status
    windows: [
      { id: 1, name: "Win 01 (North)", area: 1.0, isOpen: false },
      { id: 2, name: "Win 02 (North)", area: 1.0, isOpen: false }
    ],

    // Reactive helpers for backward compatibility & easy querying
    get isWindowOpen() {
      return Boolean(this.windows && this.windows.some(function (w) { return w.isOpen; }));
    },
    set isWindowOpen(val) {
      if (!this.windows) return;
      if (val) {
        if (!this.windows.some(function (w) { return w.isOpen; })) {
          var count = Math.min(2, this.windows.length);
          for (var i = 0; i < this.windows.length; i++) {
            this.windows[i].isOpen = (i < count);
          }
        }
      } else {
        this.windows.forEach(function (w) { w.isOpen = false; });
      }
    },
    get windowCount() {
      return this.windows ? this.windows.length : 2;
    },
    set windowCount(count) {
      if (!this.windows) this.windows = [];
      var target = Math.max(1, Math.min(8, parseInt(count, 10) || 1));
      while (this.windows.length < target) {
        var idx = this.windows.length + 1;
        this.windows.push({ id: idx, name: 'Win 0' + idx, area: 1.0, isOpen: false });
      }
      while (this.windows.length > target) {
        this.windows.pop();
      }
    },
    get windowAreaPerWin() {
      return this.windows && this.windows.length > 0 ? (this.totalWindowArea / this.windows.length) : 1.0;
    },
    get openWindowCount() {
      return this.windows ? this.windows.filter(function (w) { return w.isOpen; }).length : 0;
    },
    get totalOpenArea() {
      return this.windows ? this.windows.filter(function (w) { return w.isOpen; })
        .reduce(function (sum, w) { return sum + (w.area || 1.0); }, 0) : 0;
    },
    get totalWindowArea() {
      return this.windows ? this.windows.reduce(function (sum, w) { return sum + (w.area || 1.0); }, 0) : 0;
    },

    // Dynamic Telemetry
    co2: 1280.0,
    rateOfChange: 16.4,
    simMinutesFrom9AM: 315,    // minutes since 09:00; set to the real time on init (demos use their own clock)
    isTimeWarpRunning: false,
    warpTimer: null
  };

  (function applyStoredSize() {
    var boot = roomSizes[STATE.currentRoomId];
    if (!boot || !isFinite(boot.width) || !isFinite(boot.length) || !isFinite(boot.height)) return;
    STATE.roomWidth = round1(boot.width);
    STATE.roomLength = round1(boot.length);
    STATE.roomHeight = round1(boot.height);
    STATE.roomVolume = round1(STATE.roomWidth * STATE.roomLength * STATE.roomHeight);
  })();

  // Flush stopwatch. Measured in SIM seconds so it agrees with the model clock.
  var FLUSH = { running: false, elapsedSec: 0, startCo2: 1280, last: null };

  /* =====================================================================
     2. MATHEMATICAL MODELS (TRANSIENT MASS-BALANCE & HARVARD COGFX)
        -- math is unchanged from the original dashboard --
     ===================================================================== */
  function getAirflowQ(isOpenOrAssumeAll) {
    var qClosed = STATE.infiltrationACH * STATE.roomVolume;
    if (!isOpenOrAssumeAll) return qClosed;
    var openArea = STATE.totalOpenArea > 0 ? STATE.totalOpenArea : STATE.totalWindowArea;
    if (openArea <= 0) return qClosed;
    return qClosed + (STATE.kFlow * openArea);
  }

  function getCurrentAirflowQ() {
    var qClosed = STATE.infiltrationACH * STATE.roomVolume;
    if (!STATE.isWindowOpen || STATE.totalOpenArea <= 0) return qClosed;
    return qClosed + (STATE.kFlow * STATE.totalOpenArea);
  }

  function stepMassBalanceCO2(c0, nPeople, vM3, qM3h, dtHours, gPerson) {
    var gTotal = nPeople * gPerson;
    var qSafe = Math.max(qM3h, 0.1);
    var decay = Math.exp(-(qSafe / vM3) * dtHours);
    return STATE.cOutdoor + (gTotal / qSafe) * 1000.0 * (1.0 - decay) + (c0 - STATE.cOutdoor) * decay;
  }

  function evaluateHarvardCOGfx(co2Ppm) {
    var delta = Math.max(0.0, co2Ppm - 550.0);
    var overall = Math.max(12.0, Math.min(100.0, +(100.0 * Math.exp(-0.00081 * delta)).toFixed(1)));
    var clampDomain = function (k) { return Math.max(10, Math.min(100, Math.round(100.0 - k * delta))); };

    return {
      overall: overall,
      routine: clampDomain(0.005),
      focused: clampDomain(0.035),
      info: clampDomain(0.052),
      crisis: clampDomain(0.064),
      strategic: clampDomain(0.068),
      reactionPenalty: +(-1.6 * (Math.max(0.0, co2Ppm - 420.0) / 500.0)).toFixed(2)
    };
  }

  /* ---- Forecast arrays for the admin multi-horizon chart (unchanged) ---- */
  function buildForecastArrays() {
    var cNow = Math.round(STATE.co2);
    var past = [
      490, 540, 630, 740,
      Math.round(cNow * 0.72),
      Math.round(cNow * 0.81),
      Math.round(cNow * 0.89),
      Math.round(cNow * 0.95),
      cNow
    ];
    // Real-time sample room: the last 2 hours as they happened
    var day = daySeries([-120, -105, -90, -75, -60, -45, -30, -15, 0]);
    if (day && day.co2.every(function (v) { return v != null; })) past = day.co2;

    var dtSteps = [0.25, 0.25, 0.25, 0.25, 0.5, 0.5, 0.5, 0.5, 1.0];
    var noIntervention = [cNow];
    var upperSigma = [cNow];
    var lowerSigma = [cNow];
    var optimalFlush = [cNow];

    var cUnmitigated = cNow;
    var cUpper = cNow;
    var cLower = cNow;
    var cOpt = cNow;

    var qClosed = getAirflowQ(false);
    var qOpen = getAirflowQ(true);

    for (var i = 0; i < dtSteps.length; i++) {
      var dt = dtSteps[i];
      cUnmitigated = stepMassBalanceCO2(cUnmitigated, STATE.occupancy, STATE.roomVolume, qClosed, dt, STATE.co2GenPerPerson);
      cUpper = stepMassBalanceCO2(cUpper, STATE.occupancy * 1.22 + 1, STATE.roomVolume, qClosed * 0.85, dt, STATE.co2GenPerPerson);
      cLower = stepMassBalanceCO2(cLower, Math.max(0, STATE.occupancy * 0.8 - 0.5), STATE.roomVolume, qClosed * 1.15, dt, STATE.co2GenPerPerson);

      var useFlush = STATE.isWindowOpen || i === 0 || (cOpt > 1100 && i === 4);
      var qStep = useFlush ? qOpen : qClosed * 1.3;
      cOpt = stepMassBalanceCO2(cOpt, STATE.occupancy, STATE.roomVolume, qStep, dt, STATE.co2GenPerPerson);

      noIntervention.push(Math.round(cUnmitigated));
      upperSigma.push(Math.round(cUpper));
      lowerSigma.push(Math.round(cLower));
      optimalFlush.push(Math.round(cOpt));
    }

    return {
      pastSeries: past.concat(Array(9).fill(null)),
      forecastSeries: Array(8).fill(null).concat(noIntervention),
      upperSeries: Array(8).fill(null).concat(upperSigma),
      lowerSeries: Array(8).fill(null).concat(lowerSigma),
      optimalSeries: Array(8).fill(null).concat(optimalFlush)
    };
  }

  /* ---- Look-ahead helpers (model stepped forward) ---------------------- */

  // Minutes until c0 crosses `threshold` for fixed occupancy/airflow. null = never.
  function projectMinutesUntil(c0, nPeople, vM3, qM3h, gPerson, threshold, maxMin) {
    if (c0 >= threshold) return 0;
    var qSafe = Math.max(qM3h, 0.1);
    var cEq = STATE.cOutdoor + (nPeople * gPerson / qSafe) * 1000.0;
    if (cEq <= threshold) return null;          // steady state never reaches it
    var limit = maxMin || 480;
    var c = c0;
    for (var m = 1; m <= limit; m++) {
      var next = stepMassBalanceCO2(c, nPeople, vM3, qM3h, 1.0 / 60.0, gPerson);
      if (next >= threshold) {
        var frac = (threshold - c) / (next - c);
        return +(m - 1 + frac).toFixed(2);
      }
      c = next;
    }
    return null;
  }

  // Public: windows closed (infiltration only), current occupancy.
  function minutesUntil(thresholdPpm) {
    return projectMinutesUntil(
      STATE.co2, STATE.occupancy, STATE.roomVolume,
      getAirflowQ(false), STATE.co2GenPerPerson,
      thresholdPpm == null ? STUFFY_PPM : thresholdPpm
    );
  }

  function windowsAreaFirst(n) {
    var area = 0;
    for (var i = 0; i < Math.min(n, STATE.windows.length); i++) area += (STATE.windows[i].area || 1.0);
    return area;
  }

  function flushAirflowQ(nWin) {
    return STATE.infiltrationACH * STATE.roomVolume + STATE.kFlow * windowsAreaFirst(nWin);
  }

  // Minutes of open windows (nWin of them) to bring the room from `fromPpm`
  // down to the flush target. Stepped in 0.5-min model steps; clamped 3..15.
  function flushMinutesFor(nWin, nPeople, fromPpm) {
    var n = Math.max(1, Math.min(STATE.windows.length, nWin || 2));
    var q = flushAirflowQ(n);
    var cEq = STATE.cOutdoor + (nPeople * STATE.co2GenPerPerson / Math.max(q, 0.1)) * 1000.0;
    var target = Math.max(FLUSH_TARGET_PPM, cEq + 60);
    var c = fromPpm;
    var minutes = 0;
    while (c > target && minutes < 60) {
      c = stepMassBalanceCO2(c, nPeople, STATE.roomVolume, q, 0.5 / 60.0, STATE.co2GenPerPerson);
      minutes += 0.5;
    }
    return Math.max(3, Math.min(15, Math.ceil(minutes)));
  }

  // "Open N windows for M min": sized for the worse of now / the 1,000 ppm line.
  function flushMinutes(nWin) {
    return flushMinutesFor(nWin, STATE.occupancy, Math.max(STATE.co2, STUFFY_PPM));
  }

  function recommendation() {
    var n = Math.min(2, STATE.windows.length);
    var over = STATE.co2 >= STUFFY_PPM;
    var mins = over ? 0 : minutesUntil(STUFFY_PPM);
    return {
      thresholdPpm: STUFFY_PPM,
      over: over,
      minutesUntilStuffy: mins,                 // null => no breach forecast
      windows: n,
      flushMinutes: flushMinutes(n),
      co2: STATE.co2
    };
  }

  /* =====================================================================
     2B. SCHEDULE (shared by both views) + SCHEDULE-AWARE FORECAST
     ===================================================================== */
  // Fixed 13:00–16:30 calendar used only by the 8-hour and guided demos
  // (their scripts and captions depend on these exact times).
  var DEMO_SCHEDULE = [
    {
      id: 'b1', type: 'booking', start: 13 * 60, end: 14 * 60,
      title: 'Quantitative Strategy Sync', scheduled: 10, expected: 7
    },
    {
      id: 'buf', type: 'buffer', start: 14 * 60 + 40, end: 14 * 60 + 50,
      title: '[AUTO-BUFFER] 10 min room air flush', windows: 2
    },
    {
      id: 'b2', type: 'booking', start: 15 * 60, end: 16 * 60 + 30,
      title: 'Architecture Review Board', scheduled: 14, expected: 11
    }
  ];

  /* =====================================================================
     2C. TYPICAL WEEKDAY for the sample rooms (normal mode, real clock)
     ---------------------------------------------------------------------
     Outside the demos the clock is the real time of day and each sample
     room follows a typical weekday: bookings (with no-shows and one ghost
     booking), drop-ins between bookings, and Caelum's 10-min fresh-air
     breaks. Weekends use the same weekday plan, labelled "Sample weekday",
     because an empty Sunday office shows nothing. The Live Room has no
     plan: its people come from the boards.
     ===================================================================== */
  function hm(h, m) { return h * 60 + (m || 0); }
  function booking(start, end, title, scheduled, expected) {
    return { type: 'booking', start: start, end: end, title: title, scheduled: scheduled, expected: expected };
  }
  function flushBreak(start) {
    return { type: 'buffer', start: start, end: start + 10, title: '[AUTO-BUFFER] 10 min room air flush', windows: 2 };
  }
  function withIds(roomId, events) {
    return events.map(function (e, i) { e.id = roomId + '-' + i; return e; });
  }

  // background: [startMin, endMin, people] for time outside bookings.
  // emptyAch: air changes/h while the room is empty (door left open).
  var DAY_PLANS = {
    // Meeting room, 20 seats, 140 m³: typical office meeting day.
    forum: {
      emptyAch: 1.5,
      background: [],
      events: withIds('forum', [
        booking(hm(9, 30), hm(9, 45), 'Daily stand-up', 9, 8),
        booking(hm(10), hm(11), 'Sprint planning', 12, 9),
        booking(hm(11, 15), hm(11, 45), 'Supplier call (no-show)', 6, 0),
        booking(hm(12), hm(12, 50), 'Lunch & learn', 18, 14),
        flushBreak(hm(12, 50)),
        booking(hm(13), hm(14), 'Quantitative Strategy Sync', 10, 7),
        flushBreak(hm(14, 40)),
        booking(hm(15), hm(16, 30), 'Architecture Review Board', 14, 11),
        booking(hm(16, 45), hm(17, 15), 'Weekly wrap-up', 8, 6)
      ])
    },
    // Bookable student study pod, 12 seats, 65 m³: busy afternoons and evenings.
    appleton: {
      emptyAch: 1.0,
      background: [[hm(8, 30), hm(21), 2]],
      events: withIds('appleton', [
        booking(hm(9), hm(10, 30), 'Group project: ML coursework', 5, 4),
        booking(hm(11), hm(12), 'Tutorial prep', 8, 6),
        booking(hm(13), hm(14, 30), 'Exam revision group', 9, 7),
        flushBreak(hm(14, 30)),
        booking(hm(14, 45), hm(16, 15), 'Hackathon team sync', 6, 5),
        booking(hm(17), hm(19), 'Evening study session', 10, 7),
        flushBreak(hm(19)),
        booking(hm(19, 30), hm(21), 'Society committee', 8, 5)
      ])
    },
    // One student's bedroom, 32 m³: study blocks, out at lectures, asleep at night.
    personal: {
      emptyAch: 0.5,
      background: [[0, hm(7, 30), 1], [hm(23), 1440, 1]],
      events: withIds('personal', [
        booking(hm(9, 30), hm(12), 'Morning study block', 1, 1),
        booking(hm(14), hm(17), 'Afternoon study block', 1, 1),
        flushBreak(hm(17)),
        booking(hm(19), hm(22, 30), 'Evening revision', 1, 1)
      ])
    }
  };

  // Custom rooms: the meeting-room day scaled to the room's seats.
  var scaledPlans = {};
  function planFor(roomId) {
    if (!roomId || roomId === LIVE_ROOM_ID) return null;
    if (DAY_PLANS[roomId]) return DAY_PLANS[roomId];
    var room = getAllRooms().find(function (r) { return r.id === roomId; });
    if (!room) return null;
    var cap = roomCapacity(room);
    var key = roomId + ':' + cap;
    if (!scaledPlans[key]) {
      var k = cap / 20;
      var base = DAY_PLANS.forum;
      scaledPlans[key] = {
        emptyAch: base.emptyAch,
        background: [],
        events: base.events.map(function (e) {
          var c = {};
          for (var p in e) if (Object.prototype.hasOwnProperty.call(e, p)) c[p] = e[p];
          c.id = roomId + '-' + e.id;
          if (c.type === 'booking') {
            c.scheduled = Math.max(1, Math.round(e.scheduled * k));
            c.expected = e.expected === 0 ? 0 : Math.max(1, Math.round(e.expected * k));
          }
          return c;
        })
      };
    }
    return scaledPlans[key];
  }

  function eventAt(events, tod) {
    for (var i = 0; i < events.length; i++) {
      if (tod >= events[i].start && tod < events[i].end) return events[i];
    }
    return null;
  }
  function backgroundAt(plan, tod) {
    for (var i = 0; i < plan.background.length; i++) {
      var b = plan.background[i];
      if (tod >= b[0] && tod < b[1]) return b[2];
    }
    return 0;
  }
  // Deterministic -1 / 0 / +1 headcount wobble that changes every 10 minutes.
  function wobble(tod) {
    var h = (Math.imul(Math.floor(tod / 10) + 1, 2654435761) >>> 0) % 4;
    return [0, 1, 0, -1][h];
  }
  // People in the room at minute-of-day `tod`. People arrive over the first 5 minutes.
  function plannedOccupancy(plan, tod, smooth) {
    tod = ((Math.floor(tod) % 1440) + 1440) % 1440;
    var ev = eventAt(plan.events, tod);
    var bg = backgroundAt(plan, tod);
    if (!ev) return bg;
    if (ev.type === 'buffer') return 0;
    var n = ev.expected;
    var into = tod - ev.start;
    if (into < 5) n = Math.round(bg + (n - bg) * (into + 1) / 5);
    if (!smooth && ev.expected >= 4 && into >= 5) n += wobble(tod);
    return Math.max(0, n);
  }
  function plannedScheduled(plan, tod) {
    tod = ((Math.floor(tod) % 1440) + 1440) % 1440;
    var ev = eventAt(plan.events, tod);
    return ev && ev.type === 'booking' ? ev.scheduled : 0;
  }
  // Airflow for the plan: an empty room has its door open.
  function planAirflowQ(plan, occ, qWindows) {
    if (occ > 0 || !plan) return qWindows;
    return Math.max(qWindows, plan.emptyAch * STATE.roomVolume);
  }

  // true  => clock is the real time of day and sample rooms follow DAY_PLANS
  // false => a demo clock (8-hour day / guided demo, or a paused / finished day)
  var realTime = true;

  function realMinutesOfDay() {
    var d = new Date();
    return d.getHours() * 60 + d.getMinutes() + d.getSeconds() / 60;
  }
  function syncClockToReal() {
    STATE.simMinutesFrom9AM = realMinutesOfDay() - 9 * 60;
  }
  function isWeekend() {
    var day = new Date().getDay();
    return day === 0 || day === 6;
  }

  function clockMinutesOfDay() {
    return (((9 * 60 + Math.floor(STATE.simMinutesFrom9AM + 1e-9)) % 1440) + 1440) % 1440;
  }
  function formatClockHHMM(minOfDay) {
    var m = ((Math.round(minOfDay) % 1440) + 1440) % 1440;
    return String(Math.floor(m / 60)).padStart(2, '0') + ':' + String(m % 60).padStart(2, '0');
  }
  function clockString() {
    var x = STATE.simMinutesFrom9AM;
    var secs = Math.floor((x - Math.floor(x + 1e-9)) * 60 + 1e-6);
    if (secs < 0 || secs > 59) secs = 0;
    return formatClockHHMM(clockMinutesOfDay()) + ':' + String(secs).padStart(2, '0');
  }

  // The calendar shown and forecast: the demo calendar while a demo clock runs,
  // otherwise the current room's typical weekday (none for the Live Room).
  function activePlan() {
    return realTime ? planFor(STATE.currentRoomId) : null;
  }
  function getSchedule() {
    if (!realTime) return DEMO_SCHEDULE;
    var plan = planFor(STATE.currentRoomId);
    return plan ? plan.events : [];
  }
  // The part of the day worth listing: the last finished item, then what's on now and next.
  function upcomingSchedule(max) {
    var all = getSchedule();
    if (!realTime) return all.slice();
    var now = clockMinutesOfDay();
    var firstOpen = all.findIndex(function (e) { return e.end > now; });
    if (firstOpen === -1) return all.slice(-1);
    return all.slice(Math.max(0, firstOpen - 1), Math.max(0, firstOpen - 1) + (max || 5));
  }
  function formatMMSS(totalSecs) {
    var s = Math.max(0, Math.floor(totalSecs));
    return String(Math.floor(s / 60)).padStart(2, '0') + 'm ' + String(s % 60).padStart(2, '0') + 's';
  }

  // Projected CO2 for the next `horizonMin` minutes using the calendar:
  // inside a live booking the BLE headcount holds; later bookings use the
  // expected attendance; the auto-buffer empties the room and opens windows.
  function forecastTimeline(horizonMin, stepMin) {
    horizonMin = horizonMin || 240;
    stepMin = stepMin || 5;
    var nowTod = clockMinutesOfDay();
    var nWin = Math.min(2, STATE.windows.length);
    var qClosed = getAirflowQ(false);
    var qFlush = flushAirflowQ(nWin);
    var qNow = getCurrentAirflowQ();
    var c = STATE.co2;
    var out = [{ t: 0, tod: nowTod, co2: c, occ: STATE.occupancy, flushing: STATE.isWindowOpen }];
    var schedule = getSchedule();
    var plan = activePlan();
    var nowEv = eventAt(schedule, nowTod);

    for (var t = stepMin; t <= horizonMin; t += stepMin) {
      var tod = (nowTod + t - stepMin) % 1440;
      var occ = STATE.occupancy;
      var q = qClosed;
      var flushing = false;

      var ev = eventAt(schedule, tod);
      if (ev && ev.type === 'buffer') {
        occ = 0; q = qFlush; flushing = true;
      } else if (plan) {
        // the current booking keeps today's headcount; later times follow the plan
        occ = (ev && ev === nowEv) ? STATE.occupancy : plannedOccupancy(plan, tod, true);
        q = planAirflowQ(plan, occ, qClosed);
      } else if (ev) {
        occ = (nowTod >= ev.start && nowTod < ev.end) ? STATE.occupancy : ev.expected;
      } else {
        var ended = schedule.some(function (e) { return e.end <= tod && e.end > nowTod; });
        occ = ended ? Math.min(2, STATE.occupancy) : STATE.occupancy;
      }
      // Windows that are open right now stay open until recovered (max 20 min)
      if (STATE.isWindowOpen && (t - stepMin) < 20 && c > FLUSH_TARGET_PPM && !flushing) {
        q = qNow; flushing = true;
      }
      c = stepMassBalanceCO2(c, occ, STATE.roomVolume, q, stepMin / 60.0, STATE.co2GenPerPerson);
      out.push({ t: t, tod: (nowTod + t) % 1440, co2: c, occ: occ, flushing: flushing });
    }
    return out;
  }

  // Freshest point in the forecast that is bookable: not inside a booking and
  // not inside the (empty, windows-open) flush buffer. t=0 is always eligible.
  function bestTimeFromTimeline(tl) {
    var best = null;
    for (var i = 0; i < tl.length; i++) {
      var p = tl[i];
      var blocked = p.t > 0 && getSchedule().some(function (e) {
        return p.tod >= e.start && p.tod < e.end;
      });
      if (blocked) continue;
      if (!best || p.co2 < best.co2 - 0.5) best = p;
    }
    if (!best) best = tl[0];
    return { t: best.t, tod: best.tod, co2: best.co2, isNow: best.t === 0 };
  }

  /* =====================================================================
     3. FLUSH STOPWATCH (sim-seconds) + WINDOW ACTIONS
     ===================================================================== */
  function syncFlush() {
    if (STATE.isWindowOpen && !FLUSH.running) {
      FLUSH.running = true;
      FLUSH.elapsedSec = 0;
      FLUSH.startCo2 = Math.round(STATE.co2);
    } else if (!STATE.isWindowOpen && FLUSH.running) {
      if (FLUSH.elapsedSec > 0) {
        var endCo2 = Math.round(STATE.co2);
        FLUSH.last = {
          durationStr: formatMMSS(FLUSH.elapsedSec),
          seconds: FLUSH.elapsedSec,
          startCo2: FLUSH.startCo2,
          endCo2: endCo2,
          drop: Math.max(0, FLUSH.startCo2 - endCo2),
          time: clockString().slice(0, 5)
        };
      }
      FLUSH.running = false;
      FLUSH.elapsedSec = 0;
    }
  }

  function setOpenInternal(count) {
    var target = Math.max(0, Math.min(STATE.windows.length, parseInt(count, 10) || 0));
    STATE.windows.forEach(function (w, idx) { w.isOpen = (idx < target); });
    syncFlush();
  }

  /* =====================================================================
     4. SEEDED RANDOMNESS (BLE packet generator) so demo runs look identical
     ===================================================================== */
  function mulberry32(a) {
    return function () {
      a |= 0; a = (a + 0x6D2B79F5) | 0;
      var t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }
  var DEFAULT_SEED = 0xCAE1;
  var rng = mulberry32(DEFAULT_SEED);
  function random() { return rng(); }
  function reseed(seed) {
    rng = mulberry32(seed == null ? DEFAULT_SEED : seed);
    emit('reseed');
  }

  /* =====================================================================
     5. SUBSCRIPTIONS, SNAPSHOTS, BROADCAST CHANNEL
     ===================================================================== */
  var role = null;            // 'admin' | 'room'
  var authority = false;      // true => this page advances the simulation
  var link = 'connecting';    // room view only: 'connecting' | 'admin' | 'standalone'
  var subscribers = [];
  var channel = null;
  var lastAdminMsg = 0;
  var loadedAt = Date.now();
  var seq = 0;
  var bgTimer = null;
  var lastTickAt = 0;
  var heartbeatTimer = null;
  var linkTimer = null;

  function subscribe(fn) {
    subscribers.push(fn);
    return function unsubscribe() {
      subscribers = subscribers.filter(function (f) { return f !== fn; });
    };
  }

  function notify(reason) {
    for (var i = 0; i < subscribers.length; i++) {
      try { subscribers[i](STATE, reason); }
      catch (e) { if (root.console) root.console.error('Caelum subscriber error', e); }
    }
  }

  function emit(reason) {
    if (role === 'admin') broadcast();
    notify(reason || 'change');
  }

  function copyWindows(ws) {
    return ws.map(function (w, i) {
      return { id: w.id || i + 1, name: w.name || ('Win 0' + (i + 1)), area: w.area || 1.0, isOpen: !!w.isOpen };
    });
  }

  function roomList() {
    return getAllRooms().map(function (r) {
      return { id: r.id, name: r.name, short: roomShortName(r), volume: r.volume, windows: r.windows.length, capacity: roomCapacity(r) };
    });
  }

  // Everything a follower needs, JSON-serialisable.
  function snapshot() {
    return {
      v: 1,
      roomId: STATE.currentRoomId,
      roomName: STATE.roomName,
      rooms: roomList(),
      roomVolume: STATE.roomVolume,
      roomWidth: STATE.roomWidth,
      roomLength: STATE.roomLength,
      roomHeight: STATE.roomHeight,
      capacity: STATE.capacity,
      occupancy: STATE.occupancy,
      scheduledCount: STATE.scheduledCount,
      gPerson: STATE.co2GenPerPerson,
      cOutdoor: STATE.cOutdoor,
      infiltrationACH: STATE.infiltrationACH,
      kFlow: STATE.kFlow,
      co2: STATE.co2,
      simMinutes: STATE.simMinutesFrom9AM,
      clock: clockString(),
      realTime: realTime,
      windows: copyWindows(STATE.windows),
      flush: { running: FLUSH.running, elapsedSec: FLUSH.elapsedSec, startCo2: FLUSH.startCo2, last: FLUSH.last },
      demo: demoPublic()
    };
  }

  var remoteRooms = null;     // follower: room list as last broadcast
  var remoteDemo = null;      // follower: demo block as last broadcast

  function applySnapshot(s) {
    if (!s || typeof s !== 'object') return;
    STATE.currentRoomId = s.roomId;
    STATE.roomName = s.roomName;
    STATE.roomVolume = s.roomVolume;
    if (isFinite(s.roomWidth) && isFinite(s.roomLength) && isFinite(s.roomHeight)) {
      STATE.roomWidth = s.roomWidth;
      STATE.roomLength = s.roomLength;
      STATE.roomHeight = s.roomHeight;
    } else {
      var derived = deriveSize(s.roomVolume);
      STATE.roomWidth = derived.width;
      STATE.roomLength = derived.length;
      STATE.roomHeight = derived.height;
    }
    STATE.capacity = s.capacity;
    STATE.occupancy = s.occupancy;
    STATE.scheduledCount = s.scheduledCount;
    STATE.co2GenPerPerson = s.gPerson;
    STATE.cOutdoor = s.cOutdoor;
    STATE.infiltrationACH = s.infiltrationACH;
    STATE.kFlow = s.kFlow;
    STATE.co2 = s.co2;
    STATE.simMinutesFrom9AM = s.simMinutes;
    if (typeof s.realTime === 'boolean') realTime = s.realTime;
    STATE.windows = copyWindows(s.windows || []);
    if (s.flush) {
      FLUSH.running = !!s.flush.running;
      FLUSH.elapsedSec = s.flush.elapsedSec || 0;
      FLUSH.startCo2 = s.flush.startCo2 || 0;
      FLUSH.last = s.flush.last || null;
    }
    remoteRooms = s.rooms || null;
    remoteDemo = s.demo || null;
  }

  function openChannel() {
    if (typeof root.BroadcastChannel !== 'function') return false;
    try {
      channel = new root.BroadcastChannel(CHANNEL_NAME);
      channel.onmessage = function (e) { onMessage(e.data); };
      return true;
    } catch (e) {
      channel = null;
      return false;
    }
  }

  function post(msg) {
    if (!channel) return;
    try { channel.postMessage(msg); } catch (e) { /* closed / unserialisable */ }
  }

  function broadcast() {
    if (role !== 'admin') return;
    seq++;
    post({ type: 'state', src: 'admin', seq: seq, ts: Date.now(), state: snapshot() });
  }

  function onMessage(m) {
    if (!m || typeof m !== 'object') return;
    if (role === 'admin') {
      if (m.src !== 'room') return;
      if (m.type === 'hello') broadcast();
      else if (m.type === 'cmd') applyCommand(m);
      return;
    }
    if (role === 'room' && m.src === 'admin' && m.type === 'state') {
      lastAdminMsg = Date.now();
      var wasLink = link;
      if (link !== 'admin') {
        link = 'admin';
        authority = false;
        refreshTicker();
      }
      applySnapshot(m.state);
      notify(wasLink !== 'admin' ? 'link' : 'remote');
    }
  }

  // Commands originate in the room view, are applied by whoever is authority.
  function applyCommand(m) {
    switch (m.action) {
      case 'setOpenWindows': setOpenWindowsCount(m.count); break;
      case 'closeWindows': closeAllWindows(); break;
      case 'switchRoom':
        if (!(demoMode === 'guided')) switchRoom(m.roomId);
        break;
      default: break;
    }
  }

  function command(cmd) {
    cmd = cmd || {};
    if (authority) { applyCommand(cmd); return; }
    var msg = { type: 'cmd', src: 'room' };
    for (var k in cmd) if (Object.prototype.hasOwnProperty.call(cmd, k)) msg[k] = cmd[k];
    post(msg);
  }

  function requestOpenWindows(count) { command({ action: 'setOpenWindows', count: count }); }
  function requestSwitchRoom(roomId) { command({ action: 'switchRoom', roomId: roomId }); }

  /* =====================================================================
     6. ROOM ACTIONS (each one emits exactly once)
     ===================================================================== */
  function saveCurrentRoomChangesIfCustom() {
    var customs = (root.Caelum && root.Caelum.getCustomRooms) ? root.Caelum.getCustomRooms() : getCustomRooms();
    var idx = customs.findIndex(function (r) { return r.id === STATE.currentRoomId; });
    if (idx !== -1) {
      customs[idx].volume = STATE.roomVolume;
      customs[idx].width = STATE.roomWidth;
      customs[idx].length = STATE.roomLength;
      customs[idx].height = STATE.roomHeight;
      customs[idx].occupancy = STATE.occupancy;
      customs[idx].scheduledCount = STATE.scheduledCount;
      customs[idx].windows = JSON.parse(JSON.stringify(STATE.windows));
      if (root.Caelum && root.Caelum.saveCustomRooms) root.Caelum.saveCustomRooms(customs);
      else saveCustomRooms(customs);
    }
  }

  function rememberSize(id) {
    var size = { width: STATE.roomWidth, length: STATE.roomLength, height: STATE.roomHeight };
    roomSizes[id] = size;
    persistRoomSizes();
    var room = getAllRooms().find(function (r) { return r.id === id; });
    if (room) {
      room.width = size.width;
      room.length = size.length;
      room.height = size.height;
      room.volume = STATE.roomVolume;
    }
  }

  function restoreBaseSize(id) {
    delete roomSizes[id];
    persistRoomSizes();
    var base = baseSizes[id];
    var room = getAllRooms().find(function (r) { return r.id === id; });
    if (room && base) {
      room.width = base.width;
      room.length = base.length;
      room.height = base.height;
      room.volume = base.volume;
    }
  }

  function applyRoomToState(room) {
    var size = sizeForRoom(room);
    STATE.currentRoomId = room.id;
    STATE.roomName = room.name;
    STATE.roomWidth = size.width;
    STATE.roomLength = size.length;
    STATE.roomHeight = size.height;
    STATE.roomVolume = roomSizes[room.id]
      ? round1(size.width * size.length * size.height)
      : room.volume;
    STATE.capacity = roomCapacity(room);
    STATE.occupancy = room.occupancy;
    STATE.scheduledCount = room.scheduledCount || Math.round(room.occupancy * 1.3);
    STATE.co2 = room.co2 || 1200.0;
    STATE.windows = copyWindows(room.windows).map(function (w) { w.isOpen = false; return w; });
  }

  function switchRoom(roomId) {
    var all = getAllRooms();
    var room = all.find(function (r) { return r.id === roomId; }) || all[0];
    if (!room) return;
    if (demoMode === 'guided') exitGuidedInternal(true);
    FLUSH.running = false; FLUSH.elapsedSec = 0;
    applyRoomToState(room);
    if (!STATE.isTimeWarpRunning) enterRealTime();
    syncFlush();
    emit('room');
  }

  function setOpenWindowsCount(count) {
    setOpenInternal(count);
    emit('windows');
  }

  function closeAllWindows() {
    setOpenInternal(0);
    emit('windows');
  }

  function toggleWindow(idx) {
    if (!STATE.windows[idx]) return;
    STATE.windows[idx].isOpen = !STATE.windows[idx].isOpen;
    syncFlush();
    emit('windows');
  }

  function toggleWindowsDefault() {
    if (STATE.isWindowOpen) closeAllWindows();
    else setOpenWindowsCount(Math.min(2, STATE.windows.length));
  }

  function setWindowArea(idx, areaVal) {
    if (!STATE.windows[idx]) return;
    var num = Math.max(0.2, Math.min(4.0, parseFloat(areaVal) || 1.0));
    STATE.windows[idx].area = +num.toFixed(1);
    saveCurrentRoomChangesIfCustom();
    emit('param');
  }

  function setWindowCount(n) {
    STATE.windowCount = parseInt(n, 10);
    saveCurrentRoomChangesIfCustom();
    syncFlush();
    emit('windows');
  }

  function setOccupancy(n) {
    STATE.occupancy = Math.max(0, parseInt(n, 10) || 0);
    if (realTime) holdOccupancyOverride();
    saveCurrentRoomChangesIfCustom();
    emit('param');
  }

  function clampNum(v, lo, hi, fallback) {
    var n = parseFloat(v);
    if (!isFinite(n)) n = fallback;
    return Math.min(hi, Math.max(lo, n));
  }

  function setVolume(v) {
    var vol = parseFloat(v);
    if (!isFinite(vol)) return;
    var foot = STATE.roomWidth * STATE.roomLength;
    if (foot > 0) {
      STATE.roomHeight = round1(clampNum(vol / foot, 1.5, 15, STATE.roomHeight));
      STATE.roomVolume = round1(STATE.roomWidth * STATE.roomLength * STATE.roomHeight);
    } else {
      STATE.roomVolume = Math.max(1, vol);
    }
    rememberSize(STATE.currentRoomId);
    saveCurrentRoomChangesIfCustom();
    emit('param');
  }

  function setRoomSize(width, length, height) {
    STATE.roomWidth = round1(clampNum(width, 1, 100, STATE.roomWidth));
    STATE.roomLength = round1(clampNum(length, 1, 100, STATE.roomLength));
    STATE.roomHeight = round1(clampNum(height, 1.5, 15, STATE.roomHeight));
    STATE.roomVolume = round1(STATE.roomWidth * STATE.roomLength * STATE.roomHeight);
    rememberSize(STATE.currentRoomId);
    saveCurrentRoomChangesIfCustom();
    emit('param');
  }

  function setGPerson(g) {
    STATE.co2GenPerPerson = parseFloat(g);
    emit('param');
  }

  function resetToBaseline() {
    stopWarpInternal('idle');
    var keepLive = demoMode === 'live';
    if (demoMode === 'guided') exitGuidedInternal(true);   // back to the pre-demo room first
    if (keepLive) demoMode = 'live';
    restoreBaseSize(STATE.currentRoomId);
    var all = getAllRooms();
    var currentRoom = all.find(function (r) { return r.id === STATE.currentRoomId; }) || DEFAULT_ROOMS[0];
    applyRoomToState(currentRoom);
    STATE.scheduledCount = currentRoom.scheduledCount || 12;
    STATE.co2GenPerPerson = 16.0;
    STATE.co2 = currentRoom.co2 || 1280.0;
    FLUSH.running = false; FLUSH.elapsedSec = 0; FLUSH.last = null;
    enterRealTime();
    rng = mulberry32(DEFAULT_SEED);
    refreshTicker();
    emit('reset');
    emit('reseed');
  }

  /* =====================================================================
     7. SIMULATION CLOCK: BACKGROUND TICK
     ===================================================================== */
  // Advance the model by `minutes` sim-minutes (windows/occupancy as-is).
  function advanceModel(minutes) {
    // While the boards are connected, CO₂ comes from their people count.
    var backendOwnsCo2 = root.hackaburgerBackend && root.hackaburgerBackend.mode !== 'sim';
    if (backendOwnsCo2) {
      STATE.simMinutesFrom9AM += minutes;
      if (FLUSH.running) FLUSH.elapsedSec += minutes * 60;
      return;
    }
    var q = getCurrentAirflowQ();
    STATE.co2 = stepMassBalanceCO2(
      STATE.co2, STATE.occupancy, STATE.roomVolume, q, minutes / 60.0, STATE.co2GenPerPerson
    );
    STATE.simMinutesFrom9AM += minutes;
    if (FLUSH.running) FLUSH.elapsedSec += minutes * 60;
  }

  /* ---- 7b. REAL-TIME DAY: the sample room follows its typical weekday ---- */
  var OCCUPANT_OPEN_PPM = 1600;   // nobody in the room acts before this: someone cracks a window
  var OCCUPANT_CLOSE_PPM = 800;   // ... and shuts it again once the air is fresh
  var HISTORY_MIN = 240;
  var history = [];               // [{ abs, co2, occ, sched }] one entry per real minute
  var occOverrideUntil = 0;       // ms: a manual headcount holds until the next calendar change
  var occupantOpened = false;     // the simulated occupants (not the user) opened a window
  var lastPlanEvId = null;

  function backendOwnsRoom() {
    return !!(root.hackaburgerBackend && root.hackaburgerBackend.mode !== 'sim');
  }

  // What the people in the room do at minute `tod`: follow Caelum's fresh-air breaks,
  // and crack a window when the air gets really bad. Returns the open-window count.
  function occupantWindows(plan, tod, prevEvId, co2, openNow, occ, nWin, flags) {
    var ev = eventAt(plan.events, tod);
    var evId = ev ? ev.id : null;
    if (evId !== prevEvId) {
      var prev = prevEvId && plan.events.find(function (e) { return e.id === prevEvId; });
      if (ev && ev.type === 'buffer') { flags.byOccupants = false; return Math.min(ev.windows || 2, nWin); }
      if (prev && prev.type === 'buffer') { flags.byOccupants = false; return 0; }
    }
    if (openNow === 0 && occ > 0 && co2 >= OCCUPANT_OPEN_PPM) { flags.byOccupants = true; return 1; }
    if (openNow > 0 && flags.byOccupants && co2 <= OCCUPANT_CLOSE_PPM) { flags.byOccupants = false; return 0; }
    return openNow;
  }

  function windowsQ(openCount) {
    if (openCount <= 0) return getAirflowQ(false);
    var area = 0;
    for (var i = 0; i < openCount && i < STATE.windows.length; i++) area += STATE.windows[i].area || 1.0;
    return getAirflowQ(false) + STATE.kFlow * area;
  }

  function pushHistory(abs) {
    var last = history[history.length - 1];
    var row = { abs: abs, co2: STATE.co2, occ: STATE.occupancy, sched: STATE.scheduledCount };
    if (last && last.abs === abs) history[history.length - 1] = row;
    else history.push(row);
    while (history.length > HISTORY_MIN + 5) history.shift();
  }

  // Advance the sample room by `minutes` of real time, minute by minute.
  function stepRealDay(minutes) {
    var plan = activePlan();
    var owned = backendOwnsRoom();
    var abs = Math.floor(realMinutesOfDayAbs() - minutes);
    var remaining = minutes;
    while (remaining > 1e-6) {
      var step = Math.min(1, remaining);
      var tod = ((abs % 1440) + 1440) % 1440;
      if (plan && !owned) {
        if (Date.now() >= occOverrideUntil) STATE.occupancy = plannedOccupancy(plan, tod);
        STATE.scheduledCount = plannedScheduled(plan, tod);
        var flags = { byOccupants: occupantOpened };
        var ev = eventAt(plan.events, tod);
        var open = occupantWindows(plan, tod, lastPlanEvId, STATE.co2, STATE.openWindowCount,
          STATE.occupancy, STATE.windows.length, flags);
        lastPlanEvId = ev ? ev.id : null;
        occupantOpened = flags.byOccupants;
        if (open !== STATE.openWindowCount) setOpenInternal(open);
        STATE.co2 = stepMassBalanceCO2(STATE.co2, STATE.occupancy, STATE.roomVolume,
          planAirflowQ(plan, STATE.occupancy, getCurrentAirflowQ()), step / 60.0, STATE.co2GenPerPerson);
      } else if (!owned) {
        STATE.co2 = stepMassBalanceCO2(STATE.co2, STATE.occupancy, STATE.roomVolume,
          getCurrentAirflowQ(), step / 60.0, STATE.co2GenPerPerson);
      }
      if (FLUSH.running) FLUSH.elapsedSec += step * 60;
      remaining -= step;
      abs += 1;
      pushHistory(Math.floor(realMinutesOfDayAbs() - remaining));
    }
    syncClockToReal();
  }

  // Local-time minute counter (abs % 1440 = minute of day), monotonic across midnight.
  function realMinutesOfDayAbs() {
    return Math.floor(Date.now() / 60000) - new Date().getTimezoneOffset();
  }

  // Replay the last 24 h of the typical day so the room opens at today's real
  // CO2 level, with a 4-hour history for the charts.
  function warmUpRealDay() {
    syncClockToReal();
    history = [];
    occOverrideUntil = 0;
    var plan = activePlan();
    if (!plan || backendOwnsRoom()) { lastPlanEvId = null; occupantOpened = false; return; }
    var nowAbs = realMinutesOfDayAbs();
    var nowTod = Math.floor(realMinutesOfDay());
    var c = STATE.cOutdoor + 20;
    var open = 0;
    var flags = { byOccupants: false };
    var prevEvId = null;
    var nWin = STATE.windows.length;
    for (var m = -1440; m < 0; m++) {
      var tod = (((nowTod + m) % 1440) + 1440) % 1440;
      var occ = plannedOccupancy(plan, tod);
      var ev = eventAt(plan.events, tod);
      open = occupantWindows(plan, tod, prevEvId, c, open, occ, nWin, flags);
      prevEvId = ev ? ev.id : null;
      c = stepMassBalanceCO2(c, occ, STATE.roomVolume, planAirflowQ(plan, occ, windowsQ(open)),
        1 / 60.0, STATE.co2GenPerPerson);
      if (m >= -HISTORY_MIN) history.push({ abs: nowAbs + m, co2: c, occ: occ, sched: plannedScheduled(plan, tod) });
    }
    STATE.co2 = c;
    STATE.occupancy = plannedOccupancy(plan, nowTod);
    STATE.scheduledCount = plannedScheduled(plan, nowTod);
    open = occupantWindows(plan, nowTod, prevEvId, c, open, STATE.occupancy, nWin, flags);
    var evNow = eventAt(plan.events, nowTod);
    lastPlanEvId = evNow ? evNow.id : null;
    occupantOpened = flags.byOccupants;
    FLUSH.running = false; FLUSH.elapsedSec = 0;
    setOpenInternal(open);
    pushHistory(nowAbs);
  }

  // Back to the real clock (page load, room switch, reset, end of a demo).
  function enterRealTime() {
    realTime = true;
    if (!STATE.isTimeWarpRunning) warpPhase = 'idle';
    warmUpRealDay();
  }

  // A manual headcount (slider) holds until the next booking starts or ends.
  function holdOccupancyOverride() {
    var plan = activePlan();
    if (!plan) return;
    var nowTod = Math.floor(realMinutesOfDay());
    var next = 1440;
    plan.events.forEach(function (e) {
      [e.start, e.end].forEach(function (b) {
        var d = ((b - nowTod) + 1440) % 1440;
        if (d > 0 && d < next) next = d;
      });
    });
    occOverrideUntil = Date.now() + Math.min(next, 120) * 60000;
  }

  // Past values for the charts: offsets in minutes (negative = ago).
  function historyAt(minAgo) {
    if (!history.length) return null;
    var target = realMinutesOfDayAbs() - minAgo;
    var best = null;
    for (var i = 0; i < history.length; i++) {
      if (!best || Math.abs(history[i].abs - target) < Math.abs(best.abs - target)) best = history[i];
    }
    return best && Math.abs(best.abs - target) <= 2 ? best : null;
  }
  function daySeries(offsetsMin) {
    var plan = activePlan();
    if (!plan || !realTime || backendOwnsRoom() || (role !== 'admin' && link === 'admin')) return null;
    var nowTod = Math.floor(realMinutesOfDay());
    return {
      scheduled: offsetsMin.map(function (o) {
        if (o < 0) { var h = historyAt(-o); if (h) return h.sched; }
        return plannedScheduled(plan, nowTod + o);
      }),
      actual: offsetsMin.map(function (o) {
        if (o > 0) return null;
        if (o === 0) return STATE.occupancy;
        var h = historyAt(-o);
        return h ? h.occ : null;
      }),
      co2: offsetsMin.map(function (o) {
        if (o > 0) return null;
        if (o === 0) return Math.round(STATE.co2);
        var h = historyAt(-o);
        return h ? Math.round(h.co2) : null;
      })
    };
  }

  // Real-time mode: advance by the real time elapsed since the last tick.
  // Demo clock (paused / finished 8-hour day): 1 sim-minute per tick as before.
  // If the browser throttled timers (hidden tab) catch up on missed ticks.
  function tick() {
    var now = Date.now();
    if (realTime) {
      var elapsedMin = lastTickAt ? (now - lastTickAt) / 60000 : TICK_MS / 60000;
      lastTickAt = now;
      if (elapsedMin > 180) enterRealTime();      // asleep for hours: replay the day instead
      else stepRealDay(Math.max(0, elapsedMin));
      syncFlush();
      emit('tick');
      return;
    }
    var missed = lastTickAt ? Math.round((now - lastTickAt) / TICK_MS) : 1;
    lastTickAt = now;
    var n = Math.max(1, Math.min(10, missed));
    for (var i = 0; i < n; i++) advanceModel(1);
    syncFlush();
    emit('tick');
  }

  function shouldTick() {
    return authority && !STATE.isTimeWarpRunning && demoMode !== 'guided';
  }

  function refreshTicker() {
    var should = shouldTick();
    if (should && !bgTimer) {
      lastTickAt = Date.now();
      bgTimer = root.setInterval(tick, TICK_MS);
    } else if (!should && bgTimer) {
      root.clearInterval(bgTimer);
      bgTimer = null;
    }
  }

  /* =====================================================================
     8. DEMO MODE A: 8-HOUR DAY TIME-WARP (+5 sim-min every 130 ms)
     ===================================================================== */
  var demoMode = 'none';      // 'none' | 'live' | 'warp' | 'guided'
  var warpPhase = 'idle';     // 'idle' | 'running' | 'paused' | 'done'

  function warpStep() {
    STATE.simMinutesFrom9AM += 5;
    var totalMins = 9 * 60 + STATE.simMinutesFrom9AM;
    var hh = Math.floor(totalMins / 60) % 24;

    if (hh < 10) { STATE.occupancy = 3; STATE.scheduledCount = 4; }
    else if (hh >= 10 && hh < 12) { STATE.occupancy = 10; STATE.scheduledCount = 12; }
    else if (hh >= 12 && hh < 13) { STATE.occupancy = 2; STATE.scheduledCount = 2; }
    else if (hh >= 13 && hh < 15) { STATE.occupancy = 14; STATE.scheduledCount = 16; }
    else if (hh >= 15 && hh < 17) { STATE.occupancy = 9; STATE.scheduledCount = 12; }
    else { STATE.occupancy = 1; STATE.scheduledCount = 0; }

    var qCurrent = getCurrentAirflowQ();
    STATE.co2 = stepMassBalanceCO2(
      STATE.co2, STATE.occupancy, STATE.roomVolume, qCurrent, 5.0 / 60.0, STATE.co2GenPerPerson
    );
    if (FLUSH.running) FLUSH.elapsedSec += 5 * 60;

    if (STATE.co2 > 1450 && !STATE.isWindowOpen) {
      setOpenInternal(Math.min(2, STATE.windows.length));
    } else if (STATE.co2 < 620 && STATE.isWindowOpen) {
      setOpenInternal(0);
    }
    syncFlush();

    if (totalMins >= 17 * 60) {
      stopWarpInternal('done');
      emit('warp-end');
    } else {
      emit('warp');
    }
  }

  function stopWarpInternal(phase) {
    if (STATE.warpTimer) { root.clearInterval(STATE.warpTimer); STATE.warpTimer = null; }
    STATE.isTimeWarpRunning = false;
    warpPhase = phase || 'paused';
    if (demoMode === 'warp') demoMode = 'none';
    refreshTicker();
  }

  function toggleWarp() {
    if (STATE.isTimeWarpRunning) {                 // pause
      stopWarpInternal('paused');
      emit('warp-pause');
      return;
    }
    if (demoMode === 'guided') exitGuidedInternal(true);
    if (demoMode === 'live') exitLive();
    var resume = (warpPhase === 'paused');
    if (!resume || STATE.simMinutesFrom9AM >= 460) {
      // fresh run (or replay): start the day at 09:00 on a fresh-air baseline
      STATE.simMinutesFrom9AM = 0;
      STATE.co2 = 460.0;
      rng = mulberry32(DEFAULT_SEED);
      FLUSH.last = null;
      setOpenInternal(0);
      emit('reseed');
    }
    realTime = false;
    demoMode = 'warp';
    warpPhase = 'running';
    STATE.isTimeWarpRunning = true;
    refreshTicker();                                // stops the background tick
    STATE.warpTimer = root.setInterval(warpStep, 130);
    emit('warp-start');
  }

  /* =====================================================================
     9. DEMO MODE B: 4-STEP GUIDED DEMO (scripted, deterministic)
     ===================================================================== */
  var GUIDED_TOTAL = 4;
  var GUIDED_TITLES = ['Fresh room', 'Meeting fills the room', 'Prediction fires', 'Intervention'];
  var CFG = {
    freshMin: 0,            // 09:00 (sim minutes since 09:00)
    freshOcc: 3, freshSched: 4, freshCo2: 480,
    meetingMin: 240,        // 13:00
    meetingOcc: 14, meetingSched: 14,
    riseMinutes: 14,        // sim-minutes fast-forwarded in step 2
    riseFrames: 36, flushFrames: 30, frameMs: 80,
    meetingEndMin: 300,     // 14:00
    autoplayMs: 5500
  };
  var G = {
    step: 0, pre: null, scenario: null,
    animTimer: null, animating: false,
    autoplay: false, autoTimer: null, pulse: false
  };

  function buildScenario() {
    var n = Math.min(2, STATE.windows.length);
    var qC = getAirflowQ(false);
    var occ = CFG.meetingOcc, V = STATE.roomVolume, g = STATE.co2GenPerPerson;
    var c2 = stepMassBalanceCO2(CFG.freshCo2, occ, V, qC, CFG.riseMinutes / 60.0, g);
    var untilStuffy = projectMinutesUntil(c2, occ, V, qC, g, STUFFY_PPM);
    var fm = flushMinutesFor(n, occ, Math.max(c2, STUFFY_PPM));
    var qF = flushAirflowQ(n);
    var c4 = stepMassBalanceCO2(c2, occ, V, qF, fm / 60.0, g);
    var remaining = CFG.meetingEndMin - (CFG.meetingMin + CFG.riseMinutes);
    var unmitigatedPeak = stepMassBalanceCO2(c2, occ, V, qC, remaining / 60.0, g);
    var actualPeak = Math.max(c2, c4);
    return {
      windows: n,
      co2Step3: c2,
      co2Step4: c4,
      minutesUntilStuffy: untilStuffy,
      flushMinutes: fm,
      unmitigatedPeak: unmitigatedPeak,
      actualPeak: actualPeak,
      cpiUnmitigated: evaluateHarvardCOGfx(unmitigatedPeak).overall,
      cpiActual: evaluateHarvardCOGfx(c4).overall,
      qFlush: qF
    };
  }

  function fmtPpm(v) { return Math.round(v).toLocaleString('en-GB'); }

  function guidedCaption(step) {
    var sc = G.scenario;
    var clk = formatClockHHMM(9 * 60 + STATE.simMinutesFrom9AM);
    if (!sc) return '';
    switch (step) {
      case 1: return '09:00 — Fresh room: a few early arrivals, CO₂ about ' + CFG.freshCo2 + ' ppm, focus capacity ~100%.';
      case 2: return '13:00 — Meeting fills the room: ' + CFG.meetingOcc + ' people arrive and CO₂ climbs fast with the windows closed.';
      case 3: return clk + ' — Prediction: the model says ' + fmtPpm(STUFFY_PPM) + ' ppm in ~' +
        (sc.minutesUntilStuffy == null ? '?' : Math.max(1, Math.round(sc.minutesUntilStuffy))) +
        ' min. Recommendation: open ' + sc.windows + ' windows for ' + sc.flushMinutes + ' min.';
      case 4: return 'Windows open: CO₂ falls from ' + fmtPpm(sc.co2Step3) + ' to ' + fmtPpm(sc.co2Step4) +
        ' ppm. Unmitigated it would have hit ' + fmtPpm(sc.unmitigatedPeak) + ' ppm by 14:00. A 10-min flush buffer was added to the calendar.';
      default: return '';
    }
  }

  function demoPublic() {
    var d = {
      mode: demoMode,
      warpPhase: warpPhase,
      warpRunning: STATE.isTimeWarpRunning,
      step: 0, total: GUIDED_TOTAL, title: '', caption: '',
      autoplay: false, animating: false, pulse: false,
      prediction: null, avoided: null, buffer: null
    };
    if (demoMode === 'guided' && G.step > 0 && G.scenario) {
      var sc = G.scenario;
      d.step = G.step;
      d.title = GUIDED_TITLES[G.step - 1];
      d.caption = guidedCaption(G.step);
      d.autoplay = G.autoplay;
      d.animating = G.animating;
      d.pulse = G.pulse;
      if (G.step >= 3) {
        d.prediction = {
          thresholdPpm: STUFFY_PPM,
          minutes: sc.minutesUntilStuffy,
          windows: sc.windows,
          flushMinutes: sc.flushMinutes
        };
      }
      if (G.step >= 4) {
        d.avoided = {
          unmitigatedPeak: Math.round(sc.unmitigatedPeak),
          actualPeak: Math.round(sc.actualPeak),
          cpiUnmitigated: sc.cpiUnmitigated,
          cpiActual: sc.cpiActual,
          startCo2: Math.round(sc.co2Step3),
          endCo2: Math.round(sc.co2Step4),
          qFlush: Math.round(sc.qFlush),
          windows: sc.windows
        };
        d.buffer = { start: '14:40', end: '14:50', minutes: 10, windows: sc.windows };
      }
    } else if (demoMode === 'warp') {
      d.caption = 'Day simulation (fast-forward): ' + clockString().slice(0, 5);
    } else if (demoMode === 'live') {
      d.caption = metres(STATE.roomWidth) + ' m wide · ' + metres(STATE.roomLength) + ' m long · ' +
        metres(STATE.roomHeight) + ' m high · ' + Math.round(STATE.roomVolume) + ' m³';
      d.room = {
        width: STATE.roomWidth,
        length: STATE.roomLength,
        height: STATE.roomHeight,
        volume: STATE.roomVolume
      };
    }
    return d;
  }

  function metres(n) {
    var x = round1(n);
    return Math.abs(x - Math.round(x)) < 0.05 ? String(Math.round(x)) : x.toFixed(1);
  }

  function clearGuidedTimers() {
    if (G.animTimer) { root.clearInterval(G.animTimer); G.animTimer = null; }
    if (G.autoTimer) { root.clearTimeout(G.autoTimer); G.autoTimer = null; }
    G.animating = false;
  }

  function setGuidedBase(simMin, occ, sched, co2) {
    STATE.windows.forEach(function (w) { w.isOpen = false; });
    FLUSH.running = false; FLUSH.elapsedSec = 0; FLUSH.last = null;
    STATE.co2GenPerPerson = 16.0;
    STATE.simMinutesFrom9AM = simMin;
    STATE.occupancy = occ;
    STATE.scheduledCount = sched;
    STATE.co2 = co2;
    G.pulse = false;
  }

  function scheduleAutoplay() {
    if (G.autoTimer) { root.clearTimeout(G.autoTimer); G.autoTimer = null; }
    if (!G.autoplay || demoMode !== 'guided' || G.animating) return;
    if (G.step >= GUIDED_TOTAL) { G.autoplay = false; emit('guided-auto'); return; }
    G.autoTimer = root.setTimeout(function () { G.autoTimer = null; guidedNext(); }, CFG.autoplayMs);
  }

  function animateMinutes(totalMin, frames, targetSimMin) {
    var i = 0;
    G.animating = true;
    G.animTimer = root.setInterval(function () {
      advanceModel(totalMin / frames);
      syncFlush();
      i++;
      if (i >= frames) {
        root.clearInterval(G.animTimer); G.animTimer = null;
        STATE.simMinutesFrom9AM = targetSimMin;       // remove float drift
        G.animating = false;
        emit('guided-done');
        scheduleAutoplay();
      } else {
        emit('guided-frame');
      }
    }, CFG.frameMs);
  }

  // instant = jump straight to the end of the step's animation
  function guidedEnter(step, instant) {
    clearGuidedTimers();
    G.step = step;
    var sc = G.scenario;
    var endSim = CFG.meetingMin + CFG.riseMinutes;

    if (step === 1) {
      setGuidedBase(CFG.freshMin, CFG.freshOcc, CFG.freshSched, CFG.freshCo2);
      emit('guided-step');
      scheduleAutoplay();
    } else if (step === 2) {
      setGuidedBase(CFG.meetingMin, CFG.meetingOcc, CFG.meetingSched, CFG.freshCo2);
      if (instant) {
        advanceModel(CFG.riseMinutes);
        STATE.simMinutesFrom9AM = endSim;
        emit('guided-step');
        scheduleAutoplay();
      } else {
        emit('guided-step');
        animateMinutes(CFG.riseMinutes, CFG.riseFrames, endSim);
      }
    } else if (step === 3) {
      setGuidedBase(endSim, CFG.meetingOcc, CFG.meetingSched, sc.co2Step3);
      G.pulse = true;
      emit('guided-step');
      scheduleAutoplay();
    } else if (step === 4) {
      setGuidedBase(endSim, CFG.meetingOcc, CFG.meetingSched, sc.co2Step3);
      setOpenInternal(sc.windows);                    // starts the flush stopwatch
      if (instant) {
        advanceModel(sc.flushMinutes);
        STATE.simMinutesFrom9AM = endSim + sc.flushMinutes;
        emit('guided-step');
        scheduleAutoplay();
      } else {
        emit('guided-step');
        animateMinutes(sc.flushMinutes, CFG.flushFrames, endSim + sc.flushMinutes);
      }
    }
  }

  function guidedStart() {
    if (demoMode === 'guided') return;
    if (demoMode === 'live') exitLive();
    if (STATE.isTimeWarpRunning) stopWarpInternal('idle');
    warpPhase = 'idle';
    G.pre = { snap: snapshot(), warpPhase: warpPhase };
    // run in the meeting room preset (deterministic geometry)
    var room = getAllRooms().find(function (r) { return r.id === GUIDED_ROOM_ID; });
    if (room) applyRoomToState(room);
    realTime = false;
    demoMode = 'guided';
    refreshTicker();                                  // stops the background tick
    rng = mulberry32(DEFAULT_SEED);
    G.scenario = buildScenario();
    G.autoplay = false;
    emit('reseed');
    guidedEnter(1, false);
  }

  function guidedGoto(step) {
    if (demoMode !== 'guided') return;
    step = Math.max(1, Math.min(GUIDED_TOTAL, step));
    guidedEnter(step, false);
  }

  function guidedNext() {
    if (demoMode !== 'guided') { guidedStart(); return; }
    if (G.animating) {                                // first press: finish animation
      guidedEnter(G.step, true);
      return;
    }
    if (G.step >= GUIDED_TOTAL) { guidedExit(); return; }
    guidedEnter(G.step + 1, false);
  }

  function guidedPrev() {
    if (demoMode !== 'guided') return;
    if (G.animating) { guidedEnter(G.step, true); return; }
    if (G.step > 1) guidedEnter(G.step - 1, false);
  }

  function setAutoplay(on) {
    if (demoMode !== 'guided') return;
    G.autoplay = !!on;
    if (G.autoplay) scheduleAutoplay();
    else if (G.autoTimer) { root.clearTimeout(G.autoTimer); G.autoTimer = null; }
    emit('guided-auto');
  }

  function exitGuidedInternal(restore) {
    clearGuidedTimers();
    var pre = G.pre;
    demoMode = 'none';
    G.step = 0; G.pulse = false; G.autoplay = false; G.scenario = null; G.pre = null;
    if (restore && pre && pre.snap) applySnapshot(pre.snap);
    if (realTime) enterRealTime();
    refreshTicker();
  }

  function guidedExit() {
    if (demoMode !== 'guided') return;
    exitGuidedInternal(true);
    emit('demo-exit');
    emit('reseed');
  }

  /* =====================================================================
     9b. LIVE MODE: choose the room, its size, people, and windows
     ===================================================================== */
  var liveReturnId = null;
  function startLive() {
    if (demoMode === 'live') return;
    if (STATE.isTimeWarpRunning) stopWarpInternal('idle');
    if (demoMode === 'guided') exitGuidedInternal(true);
    if (STATE.currentRoomId !== LIVE_ROOM_ID) {
      liveReturnId = STATE.currentRoomId;
      switchRoom(LIVE_ROOM_ID);
    }
    demoMode = 'live';
    refreshTicker();
    emit('live-start');
  }

  function exitLive() {
    if (demoMode !== 'live') return;
    demoMode = 'none';
    var back = liveReturnId;
    liveReturnId = null;
    if (back && back !== STATE.currentRoomId) switchRoom(back);
    refreshTicker();
    emit('live-exit');
  }

  function toggleLive() {
    if (demoMode === 'live') exitLive();
    else startLive();
  }

  // Esc: leave whichever demo is active (guided -> exit, warp -> pause, live -> exit)
  function exitDemos() {
    if (demoMode === 'guided') guidedExit();
    else if (STATE.isTimeWarpRunning) toggleWarp();
    else if (demoMode === 'live') exitLive();
  }

  /* =====================================================================
     10. LIFECYCLE
     ===================================================================== */
  function becomeStandalone() {
    if (role !== 'room' || link === 'standalone') return;
    link = 'standalone';
    authority = true;
    remoteDemo = null;
    enterRealTime();
    refreshTicker();
    notify('link');
  }

  function init(opts) {
    opts = opts || {};
    role = opts.role === 'admin' ? 'admin' : 'room';
    loadedAt = Date.now();
    lastAdminMsg = loadedAt;
    var hasChannel = openChannel();

    if (role === 'admin') {
      authority = true;
      link = 'admin';
      enterRealTime();
      refreshTicker();
      heartbeatTimer = root.setInterval(broadcast, TICK_MS);
      broadcast();
    } else {
      authority = false;
      link = 'connecting';
      if (hasChannel) {
        post({ type: 'hello', src: 'room' });
        linkTimer = root.setInterval(function () {
          var age = Date.now() - lastAdminMsg;
          if (link === 'connecting' && age > LINK_CONNECT_MS) becomeStandalone();
          else if (link === 'admin' && age > LINK_LOST_MS) {
            link = 'connecting';
            becomeStandalone();
          }
        }, 500);
      } else {
        becomeStandalone();                       // no BroadcastChannel: run alone
      }
    }
    notify('init');
  }

  /* =====================================================================
     PUBLIC API
     ===================================================================== */
  root.Caelum = {
    version: '1.0.0',
    // data
    STATE: STATE,
    FLUSH: FLUSH,
    DEFAULT_ROOMS: DEFAULT_ROOMS,
    get SCHEDULE() { return getSchedule(); },
    DEMO_SCHEDULE: DEMO_SCHEDULE,
    DAY_PLANS: DAY_PLANS,
    getSchedule: getSchedule,
    upcomingSchedule: upcomingSchedule,
    daySeries: daySeries,
    isRealTime: function () { return realTime; },
    isSampleWeekday: function () { return realTime && !!activePlan() && isWeekend(); },
    CHANNEL_NAME: CHANNEL_NAME,
    // rooms
    getCustomRooms: getCustomRooms,
    saveCustomRooms: saveCustomRooms,
    getAllRooms: getAllRooms,
    roomList: roomList,
    roomShortName: roomShortName,
    roomCapacity: roomCapacity,
    getRemoteRooms: function () { return remoteRooms || roomList(); },
    // models
    getAirflowQ: getAirflowQ,
    getCurrentAirflowQ: getCurrentAirflowQ,
    stepMassBalanceCO2: stepMassBalanceCO2,
    evaluateHarvardCOGfx: evaluateHarvardCOGfx,
    buildForecastArrays: buildForecastArrays,
    forecastTimeline: forecastTimeline,
    bestTimeFromTimeline: bestTimeFromTimeline,
    minutesUntil: minutesUntil,
    flushMinutes: flushMinutes,
    recommendation: recommendation,
    // clock / format
    clockMinutesOfDay: clockMinutesOfDay,
    clockString: clockString,
    formatClock: formatClockHHMM,
    formatMMSS: formatMMSS,
    // lifecycle + sync
    init: init,
    subscribe: subscribe,
    changed: function (reason) { emit(reason || 'ui'); },
    snapshot: snapshot,
    getLink: function () { return link; },
    getRole: function () { return role; },
    isAuthority: function () { return authority; },
    hasChannel: function () { return !!channel; },
    // actions (authority applies; room view uses request*)
    switchRoom: switchRoom,
    setOpenWindowsCount: setOpenWindowsCount,
    closeAllWindows: closeAllWindows,
    toggleWindow: toggleWindow,
    toggleWindowsDefault: toggleWindowsDefault,
    setWindowArea: setWindowArea,
    setWindowCount: setWindowCount,
    setOccupancy: setOccupancy,
    setVolume: setVolume,
    setRoomSize: setRoomSize,
    setGPerson: setGPerson,
    resetToBaseline: resetToBaseline,
    saveCurrentRoomChangesIfCustom: saveCurrentRoomChangesIfCustom,
    requestOpenWindows: requestOpenWindows,
    requestSwitchRoom: requestSwitchRoom,
    command: command,
    // seeded randomness for the BLE packet generator
    random: random,
    reseed: reseed,
    // demo modes
    demo: {
      get: function () { return demoPublic(); },
      getRemote: function () { return remoteDemo || demoPublic(); },
      mode: function () { return demoMode; },
      liveToggle: toggleLive,
      liveStart: startLive,
      liveExit: exitLive,
      warpToggle: toggleWarp,
      guidedStart: guidedStart,
      guidedNext: guidedNext,
      guidedPrev: guidedPrev,
      guidedGoto: guidedGoto,
      guidedExit: guidedExit,
      setAutoplay: setAutoplay,
      exit: exitDemos,
      GUIDED_TITLES: GUIDED_TITLES,
      GUIDED_TOTAL: GUIDED_TOTAL,
      CFG: CFG
    }
  };
})(typeof globalThis !== 'undefined' ? globalThis : (typeof window !== 'undefined' ? window : this));
