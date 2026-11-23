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

1. Uses your SUEZ Smart Solutions token URL to authenticate with the smart-meter data portal at `cz-sitr.suezsmartsolutions.com`.
2. Scrapes the portal for meter index, daily and monthly consumption every **2 hours**.
3. Does not log in to the BVK customer portal or store BVK credentials.

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

### SUEZ token URL

Enter the full SUEZ URL you previously saved. It will look like:
```
https://cz-sitr.suezsmartsolutions.com/eMIS.SE_BVK/Login.aspx?token=<long_token>&langue=cs-CZ
```

Each URL is for one water meter; add a separate integration entry for each meter you want to monitor.

The token URL is stored in the Home Assistant config entry. It is never written to integration logs. If it expires, obtain a fresh URL and update it via **Settings → Integrations → BVK Water Monitor → Reconfigure**. The integration will never ask for your BVK email or password.

Existing installations are migrated without their stored BVK credentials; enter the saved token URL when Home Assistant prompts you to re-authenticate. The config entry is retained, but sensor entity IDs may change because the meter is now identified from the SUEZ URL rather than the BVK account's place list.

---

## Supported regions

Currently only **BVK Brno (Czech Republic)** is supported. The integration relies on the SUEZ Smart Solutions portal used by BVK.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Token URL is rejected | Confirm it is the full HTTPS URL for the SUEZ smart-meter portal. |
| Sensors unavailable after HA restart | The token URL may have expired; reconfigure the integration with a fresh URL. |
| Data is 1–2 days behind | Expected — the smart meter transmits readings with a ~24 h delay. |

---

## License

MIT
