"""Constants for the BVK Water Monitor integration."""
from datetime import timedelta

DOMAIN = "water_monitor"

# BVK portal
BVK_BASE_URL = "https://zis.bvk.cz"
BVK_LOGIN_URL = f"{BVK_BASE_URL}/"
BVK_PLACE_LIST_URL = f"{BVK_BASE_URL}/ConsumptionPlaceList.aspx"

# SUEZ smart solutions portal
SUEZ_BASE_URL = "https://cz-sitr.suezsmartsolutions.com/eMIS.SE_BVK"
SUEZ_HOME_URL = f"{SUEZ_BASE_URL}/Site.aspx"
SUEZ_DAILY_URL = f"{SUEZ_BASE_URL}/Site_Energie.aspx?Affichage=ConsoJour"

# Config keys
CONF_USERNAME = "username"
CONF_PASSWORD = "password"

# Coordinator
DEFAULT_UPDATE_INTERVAL = timedelta(hours=2)

# Sensor unique ID suffixes
SENSOR_METER_INDEX = "meter_index"
SENSOR_DAILY = "daily_consumption"
SENSOR_MONTHLY = "monthly_consumption"
