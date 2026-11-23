"""Constants for the BVK Water Monitor integration."""
from datetime import timedelta

DOMAIN = "water_monitor"

# SUEZ smart solutions portal
SUEZ_BASE_URL = "https://cz-sitr.suezsmartsolutions.com/eMIS.SE_BVK"
SUEZ_DAILY_URL = f"{SUEZ_BASE_URL}/Site_Energie.aspx?Affichage=ConsoJour"

# Config keys
CONF_TOKEN_URL = "token_url"

# Coordinator
DEFAULT_UPDATE_INTERVAL = timedelta(hours=2)

# Sensor unique ID suffixes
SENSOR_METER_INDEX = "meter_index"
SENSOR_DAILY = "daily_consumption"
SENSOR_MONTHLY = "monthly_consumption"
