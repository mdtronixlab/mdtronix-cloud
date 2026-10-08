"""Constants for the MDtronix Cloud integration."""

DOMAIN = "mdtronix_cloud"

# Relay origin. Device login, the tunnel, and the /link page live here.
RELAY_URL = "https://relay.mdtronix.online"

CONF_TUNNEL_TOKEN = "tunnel_token"
CONF_TUNNEL_URL = "tunnel_url"
CONF_SUBDOMAIN = "subdomain"
CONF_REMOTE_URL = "remote_url"

# Used when HA's own port can't be read from the running instance.
DEFAULT_LOCAL_PORT = 8123

# Options: the devices Google Home and Alexa may control, chosen separately for each platform.
# Nothing is exposed until the owner picks some.
CONF_EXPOSED_GOOGLE = "exposed_google"
CONF_EXPOSED_ALEXA = "exposed_alexa"
# Before the per-platform choice, one list applied to both platforms. It is still read, so existing homes keep it.
CONF_EXPOSED_ENTITIES = "exposed_entities"
# Options: the 4-8 digit PIN that unlocks locks and disarms alarms by voice. Empty means those are refused.
CONF_VOICE_PIN = "voice_pin"
