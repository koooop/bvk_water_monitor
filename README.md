# BVK Water Monitor — Home Assistant Custom Integration

A [HACS](https://hacs.xyz/)-compatible Home Assistant integration that pulls water consumption data from the **BVK (Brněnské vodárny a kanalizace)** customer portal and exposes it as sensors in Home Assistant.

---

## Sensors

| Sensor | Unit | Description |
|---|---|---|
| **Water Meter Index** | m³ | Current cumulative meter reading (`TOTAL_INCREASING`) |
| **Daily Water Consumption** | L | Most recent day's consumption |
| **Monthly Water Consumption** | L | Current month's total consumption |

The daily sensor also exposes a `last_7_days` attribute with a date → litres history, and the meter index sensor exposes the `last_reading_at` timestamp.

---

## How it works

1. Authenticates with the [BVK customer portal](https://zis.bvk.cz) using your email and password.
2. Uses a SUEZ Smart Solutions token URL to authenticate with the smart-meter data portal at `cz-sitr.suezsmartsolutions.com`.
3. Scrapes the portal for meter index, daily and monthly consumption every **2 hours**.

---

## Installation

### Via HACS (recommended)

1. In HACS go to **Integrations → ⋮ → Custom repositories**.
2. Add `https://github.com/koooop/bvk_water_monitor` with category **Integration**.
3. Install **BVK Water Monitor** and restart Home Assistant.

### Manual

1. Copy the `custom_components/water_monitor/` folder into your HA `config/custom_components/` directory.
2. Restart Home Assistant.

---

## Configuration

Go to **Settings → Integrations → Add Integration → BVK Water Monitor**.

### Step 1 — BVK credentials
Enter your **email** and **password** for the BVK customer portal (`https://zis.bvk.cz`).

The integration attempts to auto-detect your SUEZ smart-meter token URL. If your account has smart metering enabled and the token is found automatically, setup completes here.

### Step 2 — SUEZ token URL *(only if auto-detection fails)*

1. Open [https://zis.bvk.cz](https://zis.bvk.cz) in your browser and log in.
2. Navigate to **Odběrná místa** (Consumption Places).
3. Click the **smart meter icon** next to your consumption place.
4. Copy the full URL from the browser address bar — it will look like:
   ```
   https://cz-sitr.suezsmartsolutions.com/eMIS.SE_BVK/Login.aspx?token=<long_token>&langue=cs-CZ
   ```
5. Paste it into the **SUEZ token URL** field.

> **Note:** The token is long-lived (valid for months). If it ever stops working, repeat the steps above to get a fresh one and update the integration via **Settings → Integrations → BVK Water Monitor → Reconfigure**.

---

## Supported regions

Currently only **BVK Brno (Czech Republic)** is supported. The integration relies on the SUEZ Smart Solutions portal used by BVK.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Invalid auth` at setup | Double-check your BVK email and password. |
| Sensors unavailable after HA restart | The SUEZ session expired. The integration retries automatically using the stored token URL. |
| Token URL validation fails | Get a fresh token URL by logging into BVK in your browser (see Step 2 above). |
| Data is 1–2 days behind | Expected — the smart meter transmits readings with a ~24 h delay. |

---

## License

MIT
