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
