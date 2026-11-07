# Hackaburger // Design System & Frontend Architecture Guide

> **Official design specification and frontend component guide for the Hackaburger / Caelum project.**  
> Curated from the ETH Hackathon Webdev Tools repository, combining Swiss Grid structure, Built For Mars cognitive UX, Fontshare typography, and a dual-view architecture (Consumer-Facing vs. Admin Console).

---

## 1. Executive Summary & Vision

Hackaburger / Caelum combines physical Arduino UNO Q BLE hardware with real-time occupancy estimation, indoor CO₂ accumulation modeling, and cognitive performance forecasting.

To serve both regular patrons and facility operators, the frontend follows a **Dual-Persona Architecture**:
1. **Consumer-Facing Experience:** Clean, welcoming, low cognitive load, and visually delightful. Tells students, diners, or visitors instantly whether a space is quiet, how fresh the air is, and when to visit or open a window.
2. **Admin & Facility Console:** High-density, precision-engineered telemetry. Enables engineers and operators to calibrate BLE RSSI thresholds, monitor hardware heartbeats, inspect raw anonymized device packets, and analyze multi-horizon Monte Carlo air quality forecasts.

---

## 2. Core UX & Design Principles

### 2.1. The Swiss Grid ([The Swiss Grid](https://swissgrid.posterhouse.org/))
- **Mathematical Layout:** Strict 12-column responsive grid with proportional gutters (16px mobile, 24px desktop).
- **Asymmetric Balance:** Avoid monotonous symmetrical cards; anchor primary metrics in dominant hero containers with secondary stats grouped functionally.
- **Strict Visual Hierarchy:** Every element has a definitive typographic scale (Display $\rightarrow$ Headline $\rightarrow$ Subhead $\rightarrow$ Body $\rightarrow$ Tabular Metric $\rightarrow$ Caption).

### 2.2. Built For Mars Cognitive Friction Standards ([Built For Mars](https://builtformars.com/ux-glossary))
- **The 3-Second Rule:** A user must comprehend room crowdedness and air freshness within 3 seconds of opening the page without reading raw telemetry formulas.
- **Translation of Technical Metrics into Human Language:**
  - `CO₂: 680 ppm` $\rightarrow$ **"🌿 Fresh & Crisp Air"** (Low fatigue risk)
  - `CO₂: 1,280 ppm` $\rightarrow$ **"⚠️ Stuffy"** (Opening window recommended)
  - `Monte Carlo Trajectory` $\rightarrow$ **"Best Time to Visit: 14:30 – 17:00"**
- **Actionable Guidance:** Never present an issue without a concrete, low-effort fix (e.g., *"Open north windows for 4 minutes to restore peak focus"*).

---

## 3. Design Tokens & Aesthetic System

### 3.1. Typographic Hierarchy ([Fontshare](https://www.fontshare.com/))

| Role | Font Family | Weights | Usage |
| :--- | :--- | :--- | :--- |
| **Display & Headlines** | `Cabinet Grotesk` | 700 (Bold), 800 (Extrabold) | Hero titles, primary status headlines, view titles |
| **Body & UI Elements** | `General Sans` | 400 (Regular), 500 (Medium), 600 (Semibold) | Descriptions, buttons, card labels, micro-copy |
| **Telemetry & Numbers** | `JetBrains Mono` / `IBM Plex Mono` | 400, 500, 700 | Counts, percentages, RSSI dBm, timestamps, configs |

```html
<!-- Fontshare CDN Import -->
<link href="https://api.fontshare.com/v2/css?f[]=cabinet-grotesk@500,700,800&f[]=general-sans@400,500,600,700&display=swap" rel="stylesheet">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
```

```css
/* Tabular numbers rule to eliminate horizontal layout jitter */
.font-tabular {
  font-family: 'JetBrains Mono', monospace;
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.01em;
}
```

---

### 3.2. Color Palettes

#### A. Consumer Experience (Warm, Airy, Accessible)
- **Background:** `#F8FAFC` (Slate 50)
- **Cards / Glassmorphism:** `rgba(255, 255, 255, 0.88)` with `backdrop-filter: blur(12px)`
- **Fresh / Low Crowd (Optimal):** `#22C55E` (Emerald 500), Background `#DCFCE7` (Emerald 100)
- **Focus Horizon:** `#0EA5E9` (Sky 500), Background `#E0F2FE` (Sky 100)
- **Window Action / Attention:** `#F59E0B` (Amber 500), Background `#FEF3C7` (Amber 100)
- **Text Primary:** `#0F172A` (Slate 900)
- **Text Muted:** `#64748B` (Slate 500)

#### B. Admin & Engineering Console (Obsidian Precision)
- **Base Canvas:** `#0A0B0D`
- **Surface Panels:** `#111317`
- **Elevated Controls:** `#171A20`
- **Precision Border:** `#22262F` (with `border-top: 1px solid rgba(255, 255, 255, 0.08)`)
- **Telemetry Teal (Optimal):** `#2DD4BF`
- **Telemetry Ice (Forecast):** `#60A5FA`
- **Warning Threshold:** `#FBBF24`
- **Critical Alert / Disconnect:** `#F87171`
- **Text Primary:** `#F3F4F6`
- **Text Code / Muted:** `#9CA3AF`

---

## 4. Component Matrix & Tool Selection

Curated from the [Webdev Tools Notion Catalog](https://app.notion.com/p/le123654/Webdev-Tools-320882c3222380bb96afc935e277f5eb):

| Component | Selected Library / Tool | Description & Implementation Notes |
| :--- | :--- | :--- |
| **Iconography** | [Phosphor Icons](https://phosphoricons.com/) | Consistent icon set. Duotone/Regular for consumer view; bold/regular for admin tools. |
| **UI Primitives** | [shadcn/ui](https://ui.shadcn.com/) & [Bits UI](https://bits-ui.com/) | Accessible, headless primitives for tooltips, dropdowns, and progress bars. |
| **Micro-Interactions** | [Motion](https://motion.dev/) & [GSAP Flip](https://gsap.com/docs/v3/Plugins/Flip/) | Smooth layout springs when switching views, updating sliders, or toggling zones. |
| **Smooth Scrolling** | [Lenis](https://www.lenis.dev/) | Subtle momentum scrolling for dense analytical dashboards and mobile swipe. |
| **Charts & Forecasts** | [Chart.js](https://www.chart.js/) | Dual chart setup: smooth area curves for consumers; multi-line confidence bands for admins. |
| **Mockups & Assets** | [Maneken](https://maneken.app/) & [Shots.so](https://shots.so/) | For presentation slide decks, hackathon pitches, and promotional screenshots. |
| **Voice Alerts (Future)** | [ElevenLabs](https://elevenlabs.io/) | Ambient voice syntheses for window flush alarms (*"Optimal 4-minute ventilation window open"*). |
| **3D Room (Future)** | [Spline](https://spline.design/) / [Unicorn Studio](https://www.unicorn.studio/) | Interactive isometric 3D model of the room showing airflow vectors and occupied seats. |

---

## 5. Detailed Interface Specifications

### 5.1. Consumer-Facing Page (`/` or Consumer Mode)

```
+-----------------------------------------------------------------------------------+
|  [Logo] Hackaburger               [uno-q-1 | uno-q-2]        [Consumer | Admin]   |
+-----------------------------------------------------------------------------------+
|                                                                                   |
|  [ HERO STATUS BANNER ]                                                           |
|  "LIVE ATMOSPHERE: IDEAL FOR DEEP WORK"                                           |
|  Quiet, crisp, and plenty of space.                                               |
|  Current: 8 of 30 seats occupied (27%)  •  Freshness: 680 ppm (Fresh Air)         |
|                                                                                   |
+-----------------------------------+-------------------+---------------------------+
|  CROWD & SPACE GAUGE              |  COGNITIVE HEALTH |  NEXT ACTION              |
|  - Radial Progress Arc (27%)      |  - Freshness Bar  |  - "Window flush in 18m"  |
|  - "22 desks available"           |  - Fatigue: Low   |  - Target: 4 min duration |
|  - Status: Spacious               |  - Score: 94/100  |  - Impact: +18% focus     |
+-----------------------------------+-------------------+---------------------------+
|                                                                                   |
|  TODAY'S CROWD & FOCUS FORECAST (24-Hour Area Chart)                              |
|  [ Morning: Quiet ]   [ Lunch: Moderate ]   [ ★ Golden Focus: 14:30 ]   [ Evening ]|
|                                                                                   |
+-----------------------------------------------------------------------------------+
```

#### Consumer Key Features:
1. **Room Switcher:** Simple pills to switch between Main Study Hall (`uno-q-1`) and Demo Zone (`uno-q-2`).
2. **Radial Capacity Gauge:** SVG-based circular meter with animated CSS stroke dash offsets.
3. **Focus Horizon Line Chart:** Displays predicted occupancy curves over 24 hours so users can plan ahead.
4. **Flush Countdown:** Non-intrusive card indicating exactly when fresh air will keep their cognitive score highest.

---

### 5.2. Admin & Engineering Console (`/admin` or Admin Mode)

```
+-----------------------------------------------------------------------------------+
|  [INGEST_LIVE] https://hackaburger.cch-uk.tech       [Trigger Mock] [Push Config] |
|  Node: uno-q-1  •  Interval: 5s  •  Window: 30s  •  Median Filter Active          |
+-----------------------------------+-----------------------------------------------+
|  01 // ARDUINO UNO Q HARDWARE     |  02 // REAL-TIME INGEST CALIBRATION           |
|  - uno-q-1: 212 heard, 41 in range|  - RSSI Cutoff Slider:   [-75 dBm]            |
|  - uno-q-2: 34 heard, 7 in range  |  - Sliding Window:       [30 s]               |
|  - Online Heartbeat (< 20s check) |  - People Factor Multiplier: [0.70]           |
|                                   |  - Formula Preview: 41 * 0.70 = 29 people     |
+-----------------------------------+-----------------------------------------------+
|  03 // CO₂ MULTI-HORIZON TRAJECTORY               |  04 // LIVE BLE PACKET STREAM |
|  - Forecast line (ppm vs time)                    |  - a3f9c2e1: -52.0 dBm (9 pkts|
|  - Monte Carlo p=0.95 confidence upper & lower    |  - 7c41d09a: -61.5 dBm (4 pkts|
|  - Critical Threshold Line at 1,000 ppm           |  - e09b44c1: -93.0 dBm (drop) |
+---------------------------------------------------+-------------------------------+
```

#### Admin Key Features:
1. **Interactive Signal Tuner:** Sliders that instantly re-compute the in-range devices and people count in real time without refreshing.
2. **Config Versioning:** Displays active `config_version` and pre-formats the `PUT /ingest/config/{device}` payload.
3. **Telemetry & Forecasting:** Chart with historical sensor readings, predicted trajectories, and Monte Carlo confidence intervals.
4. **Anonymized Packet Inspector:** Displays incoming BLE signals with visual RSSI strength and drop indicators for signals under the threshold.

---

## 6. Backend API Mapping & Calculation Standards

The frontend interfaces with the Hackaburger ingest backend (`https://hackaburger.cch-uk.tech`):

### 6.1. Endpoints
- `GET /occupancy/latest?device={device}`: Fetches current count, smoothed people, and active config.
- `GET /occupancy/history?device={device}&from={ts}&to={ts}&step_s=60[&threshold_dbm={val}]`: Time series data for charts.
- `PUT /ingest/config/{device}`: Updates hardware scanning parameters (`threshold_dbm`, `window_s`, `interval_s`, `people_factor`).

### 6.2. Calculation Formulas (Synchronized Client & Server)
```python
# 1. Filter raw devices by signal strength
in_range = [d for d in data.devices if d.rssi >= threshold_dbm]
devices_in_range = len(in_range)

# 2. Estimate actual human count
people_raw = devices_in_range * people_factor
people = round(people_raw)

# 3. Median temporal smoothing for web display (default 30s window)
people_smoothed = round(median(recent_people_raw))
```

---

## 7. Working Reference Implementation

A working, self-contained reference implementation containing both views, interactive sliders, and dual Chart.js instances is available in:
- **`Frontend mark-up`**
- **`index.html`**

To run and preview locally:
```bash
# Using Python
python3 -m http.server 8080

# Using Node / npx
npx serve .
```
Visit `http://localhost:8080` to toggle seamlessly between the Consumer and Admin interfaces.

---

## 8. Future Roadmap for Developers

1. **Persistent URL Routing:** Replace JavaScript DOM switching with clean routing (e.g. `/` for consumer, `/admin` for console) using React Router, Next.js, or HTML5 History API.
2. **WebSockets / Server-Sent Events (SSE):** Stream `POST /ingest/data` events directly into the browser to update counts without polling.
3. **Admin Passkey Authentication:** Protect the Admin Console with an `X-API-Key` modal stored in `sessionStorage`.
4. **PWA & Offline Kiosk Mode:** Add a web app manifest for running the consumer view as an always-on tablet display next to the classroom or meeting room door.
