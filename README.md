# MDtronix Cloud for Home Assistant

Remote access to your Home Assistant through the MDtronix relay, with no port forwarding.

## Install

1. In HACS, add this repository as a custom repository (category: Integration).
2. Install **MDtronix Cloud**, then restart Home Assistant.
3. Create an MDtronix Cloud account at [relay.mdtronix.online/register](https://relay.mdtronix.online/register), and wait for MDtronix to approve it.
4. Go to **Settings → Devices & services → Add integration → MDtronix Cloud**, and sign in with that same email and password.

## What it provides

- A "Remote access" sensor that is on while the tunnel is connected.
- A repair issue if your subscription is not active.
- Repair issues if your network settings would let remote visitors skip the password or hide their real address.
- Voice control with Google Home and Alexa for the devices you choose in the integration's options: lights, switches, fans, blinds, locks, thermostats, scenes, alarm panels, and more. Nothing is exposed until you choose it. Locks and alarms need a PIN, set in the options, to unlock or disarm by voice.

## Requirements

- Home Assistant 2025.1 or newer.
- An active MDtronix subscription.

## Development

Home Assistant's test harness runs on Linux (WSL works):

```
pytest tests/mdtronix_cloud/test_config_flow.py tests/mdtronix_cloud/test_proxy_check.py
```

Protocol and design: `docs/remote-access/PROTOCOL.md` and `docs/remote-access/PHASE0.md`.

## License

MIT. See [LICENSE](LICENSE).
