# Project Context: Virtual CO2 & Cognitive Performance Engine (HackTheBurgh 2026)

## 1. Hackathon Challenge & Core Narrative
- **Main Challenge Prompt:** "Make the invisible visible. Choose something that's hard to observe. Build tools to collect your own data, or dig into public data. Show us what data is hidden, how we can find it, and what we can do with it."
- **Optional Track Added:** **HRT "Best Use of Data for Predictions & Decision Making"** track.
- **The Hook:** Carbon dioxide (CO2) is literally an invisible, odorless gas that builds up rapidly in enclosed study pods, bedrooms, and meeting rooms (climbing from 420 ppm to 1,500–2,500+ ppm in under 90 minutes). Humans have no sensory receptors for CO2; instead, elevated CO2 causes mild hypercapnia and cerebral vasodilation, which people misattribute to "afternoon brain fog," burnout, or caffeine deficiency.
- **The Solution:** A hardware-free **"Virtual CO2 Sensor" & Occupancy Prediction Engine**. By combining Bluetooth Low Energy (BLE) alternative data sniffing with historical machine learning models and a transient mass-balance physics engine, we map real-time and forecasted CO2 buildup without physical gas sensors, translate it into a live **Cognitive Performance Index (%)**, and trigger proactive window-ventilation and calendar-scheduling interventions.

---

## 2. System Architecture & Pipeline (Mapping to HRT Track & Main Brief)

### Stage 1: Alternative Data & Signal Creation (Occupancy Measurement & Prediction)
1. **Live BLE Alternative Data Sniffing:** Scan ambient Bluetooth Low Energy (BLE) advertisements and filter by RSSI signal strength (e.g., > -65 dBm) and device signatures/UUIDs to isolate human-carried mobile devices in the room without optical cameras (100% GDPR/privacy compliant).
2. **Occupancy Forecasting (Nowcasting):** Combine live BLE headcount with 7–30 days of historical occupancy patterns and Room Calendar Schedules (accounting for "ghost meetings" vs. actual attendance) to output a **Predicted Occupancy Time-Series `N(t)`** for the next 4–8 hours.

### Stage 2: Virtual CO2 Physics Engine (Mass-Balance Model)
Feed live and predicted occupancy `N(t)` along with room volume and window geometry into a differential mass-balance equation to calculate real-time and future CO2 concentrations `C(t)`.

### Stage 3: Real-Time ML & Automated Execution (Decisions)
Convert `C(t)` into executive function degradation metrics ("Environmental Cognitive Ceiling"), plot confidence bands ($\pm 1\sigma$), and calculate exact optimization parameters for when to open windows and for how long, alongside automated calendar buffer insertions.

---

## 3. Inputs, Outputs & Business Assumptions

### Inputs
1. **Room Size:** Volume $V$ in $\text{m}^3$ ($\text{Length} \times \text{Width} \times \text{Height}$).
2. **Occupancy:** Live Occupancy (via BLE) & Predicted Occupancy time-series $N(t)$.
3. **Number of Windows:** $N_{\text{win}}$.
4. **Window Size:** Area $A$ in $\text{m}^2$.
5. **Time Elapsed Since Windows Closed:** $t$ (in hours).
6. **Last Window Open Duration:** Used to compute the starting baseline concentration $C_0$.
7. **Historical Occupancy Data:** 7 to 30 days of simulated occupancy logs for ML time-series forecasting.
8. **Room Calendar Schedule:** Scheduled meetings, headcounts, and durations.

### Outputs (What We Make Visible)
1. **Live & Forecasted CO2 Curve (ppm):** Real-time room concentration plus a 4-hour forward projection driven by predicted occupancy (with probabilistic confidence bands).
2. **Cognitive Performance Index (CPI %):** Live translation of ppm into executive function impact (e.g., *"-24% strategic decision-making"*), turning invisible gas into a tangible productivity and payroll-efficiency metric.
3. **Actionable Ventilation & Schedule Alerts:** Exact countdowns for when to open windows and for how many minutes (e.g., *"Open 2 windows at 14:45 for 6 mins"*), plus automated 10-minute room-flush buffers inserted between back-to-back calendar bookings.

### Key Business & Technical Assumptions
1. **Tenant-Controlled Ventilation:** Over 80% of European and UK commercial properties (leased office floors, older university buildings, modular glass huddle pods) either lack per-room Demand-Controlled Ventilation (DCV) or give tenants zero API control over central HVAC systems. Meanwhile, modern energy-efficient glazing seals buildings tightly. Therefore, background air leakage with closed windows is modeled as a steady baseline rate ($0.2 - 0.4\text{ ACH}$), and our optimization targets the two levers teams actually control: **manual window airflow** and **room scheduling**.
2. **Zero-CapEx B2B Deployment:** Commercial tenants leasing office space will not pay thousands in CapEx to wire, mount, and later de-install hardware NDIR CO2 sensors across dozens of rooms. Virtual Sensors provide immediate, zero-hardware scalability, plus a standalone personal mode for individual study rooms/bedrooms.

---

## 4. Core Mathematical Models & Reference Code

### A. Transient Mass-Balance CO2 Equation
Indoor CO2 concentration $C(t)$ (in ppm) at time $t$ (in hours):

$$C(t) = C_{\text{outdoor}} + \frac{G}{Q} \times 1000 \cdot \left(1 - e^{-\frac{Q}{V} t}\right) + \left(C_0 - C_{\text{outdoor}}\right) e^{-\frac{Q}{V} t}$$

- $C_{\text{outdoor}} \approx 420\text{ ppm}$ (Fresh outdoor air baseline).
- $C_0$ = Initial indoor CO2 concentration (ppm) at $t = 0$ (chained across discrete simulation steps $\Delta t$).
- $V$ = Room volume in $\text{m}^3$.
- $G$ = Total CO2 generation rate in $\text{L/h}$:
  * $G(t) = N(t) \times g_{\text{person}}$
  * Sedentary desk work: $g_{\text{person}} \approx 16\text{ L/h}$.
  * Active speaking / presenting: $g_{\text{person}} \approx 22 - 25\text{ L/h}$.
- $Q$ = Fresh outdoor airflow rate in $\text{m}^3/\text{h}$ ($\text{Q} = \text{ACH} \times V$).
  * **Windows Closed ($Q_{\text{closed}}$):** Modeled via standard building infiltration: $\text{ACH} \approx 0.25 \implies Q_{\text{closed}} = 0.25 \times V$. (Never set $Q = 0$).
  * **Windows Open ($Q_{\text{open}}$):** Parameterized directly by open window area and count:
    $$Q_{\text{open}} = Q_{\text{closed}} + \left(k_{\text{flow}} \times N_{\text{win}} \times A\right)$$
    *(Empirical flow constant $k_{\text{flow}} \approx 120\text{ m}^3/(\text{h}\cdot\text{m}^2)$ for tilted windows, or $\approx 350\text{ m}^3/(\text{h}\cdot\text{m}^2)$ for standard open windows).*

### B. Cognitive Performance Index (CPI) Model
Fitted to empirical dose-response data from the **Harvard T.H. Chan School of Public Health (COGfx Study, Allen et al.)** and **Lawrence Berkeley National Lab (Satish et al.)**, anchored at $550\text{ ppm} = 100\%$ cognitive ceiling:

```python
import math

def update_co2_step(c_0: float, n_people: float, v_m3: float, q_m3h: float, dt_hours: float, c_out: float = 420.0, g_per_person: float = 16.0) -> float:
    """Calculates CO2 (ppm) after time step dt_hours."""
    g_total = n_people * g_per_person
    q_safe = max(q_m3h, 0.1)  # Prevent division by zero
    decay = math.exp(-(q_safe / v_m3) * dt_hours)
    c_t = c_out + (g_total / q_safe) * 1000.0 * (1.0 - decay) + (c_0 - c_out) * decay
    return round(c_t, 1)

def get_cognitive_metrics(co2_ppm: float) -> dict:
    """Translates CO2 ppm into executive function metrics based on Harvard COGfx & LBNL curves."""
    delta = max(0.0, co2_ppm - 550.0)
    
    # Overall Exponential Cognitive Ceiling Index (100% at <=550 ppm, ~72% at 950 ppm, ~50% at 1400 ppm)
    overall_cpi = round(100.0 * math.exp(-0.00081 * delta), 1)
    
    # Domain-Specific Linear Decay (clamped 10% to 100%)
    def calc_domain(k: float) -> int:
        return max(10, min(100, round(100.0 - k * delta)))

    return {
        "overall_index": overall_cpi,
        "routine_search": calc_domain(0.005),      # Resilient to CO2
        "focused_activity": calc_domain(0.035),    # Moderate drop
        "information_usage": calc_domain(0.052),   # High drop (~85% drop at 1400 ppm vs baseline)
        "crisis_response": calc_domain(0.064),     # Severe drop (~97% relative drop)
        "strategic_thinking": calc_domain(0.068),  # Severe drop (~91% relative drop)
        "reaction_speed_penalty_pct": round(-1.6 * (max(0.0, co2_ppm - 420.0) / 500.0), 2)