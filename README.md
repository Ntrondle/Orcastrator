# Orcastrator

Home Assistant integration that OrcaSlicer uploads to as if it were OctoPrint.
For each file it:

1. reads the G-code to find which printer it was sliced for,
2. turns that printer's power switch on (if the printer is offline or its power
   switch is off — supporting both whole-printer and PSU-only smart plugs),
3. waits until Moonraker answers and Klipper reports `ready`. If a freshly
   powered printer boots into an MCU error, it sends `FIRMWARE_RESTART`, up to
   the configured limit,
4. uploads the file to Moonraker and starts the print ("Upload and Print") or
   just stores it ("Upload").

OrcaSlicer gets its answer as soon as the file is received and the printer is
identified. Power-on and boot happen in the background. A file that matches no
printer is refused right away, so OrcaSlicer shows the error.

## Install

### HACS (custom repository)

1. In Home Assistant, open **HACS → Integrations → ⋮ → Custom repositories**.
2. Add `https://github.com/Ntrondle/Orcastrator` with category **Integration**.
3. Install **Orcastrator** and restart Home Assistant.

### Manual

Copy `custom_components/orcastrator` to `/config/custom_components/` on Home
Assistant, then restart Home Assistant.

## Configuration

Go to **Settings → Devices & services → Add integration → Orcastrator**, once per
printer. All settings — including the printer name and collapsible **Advanced
settings** — can be edited anytime from the integration's **Configure** menu.
The setup dialog also displays the exact Home Assistant upload URL to copy into
OrcaSlicer.

### Basic settings

| Field | Example |
|---|---|
| Name | `Voron 2.4` (also the tag value in the G-code) |
| Moonraker URL | `http://192.168.1.50` (`:7125` if no nginx) |
| Power switch | `switch.printer_power` |
| Aliases | optional, e.g. the exact OrcaSlicer printer preset name |
| Moonraker API key | optional, only if Moonraker requires authentication |

### Advanced settings

| Field | Default | Description |
|---|---|---|
| Turn on power switch automatically | `true` | Turn on the configured power switch when a job arrives for an offline/off printer |
| Boot timeout | `300` s | Maximum time to wait for Klipper to report `ready` |
| Firmware restart if Klipper boots into an error | `true` | Send `FIRMWARE_RESTART` when a freshly powered printer boots into an MCU error |
| Maximum firmware restarts | `3` | Maximum automatic `FIRMWARE_RESTART` attempts per job |
| Moonraker poll interval | `3` s | How often to poll `/server/info` while waiting |
| Error grace period | `10` s | Wait before restarting or failing when Klipper enters `error`/`shutdown` |
| Firmware restart backoff | `20` s | Minimum time between consecutive `FIRMWARE_RESTART` calls |
| Create persistent notification on failure | `true` | Raise a persistent HA notification when a job fails or cannot auto-start |

## OrcaSlicer

For each printer preset:

1. **Printer settings → Notes**: add a line `orcastrator_printer = Voron 2.4`
   (the Orcastrator printer name; case, spaces and `-`/`_` don't matter).
   You can also put it as a comment on the first line of *Machine start G-code*.
   Without a tag, the OrcaSlicer preset name (`printer_settings_id`) is matched
   against the printer's name and aliases.
2. **Physical printer** (Wi-Fi icon next to the printer):
   - Host type: `Octo/Klipper`
   - Hostname, IP or URL: `http://homeassistant.local:8123/api/orcastrator` (shown in the setup dialog)
   - API key: a Home Assistant long-lived access token (Profile → Security)
   - Device UI: the printer's Mainsail/Fluidd URL, so the Device tab keeps working
   - **Test** should report the connection is working.

Then slice → **Print** → *Upload and Print*.

## In Home Assistant

Each configured printer exposes a device with:

- `sensor.<printer>_job`: `idle`, `checking_printer`, `powering_on`,
  `waiting_for_printer`, `uploading`, `print_started`, `print_queued`,
  `uploaded`, `cancelled`, `failed`. Attributes: `filename`, `start_print`,
  `detail` (Klipper state while waiting), `last_error`, `queued_jobs`, `updated`.
- `button.<printer>_cancel_job`: cancels the currently waiting/uploading job and
  any queued jobs for that printer.
- `switch.<printer>_auto_power_on`: toggles automatic power-on in place without
  reloading or cancelling active jobs.
- `switch.<printer>_firmware_restart_on_boot_error`: toggles automatic
  `FIRMWARE_RESTART` on boot errors in place.
- **Diagnostics**: **Settings → Devices & services → Orcastrator → ⋮ → Download
  diagnostics** exports redacted config and current runtime state (API keys redacted).
- Event `orcastrator_job` on every state change (`printer`, `entry_id`, `state`,
  `detail`, `filename`, `error`).

Example mobile notification automation on print start / failure:

```yaml
alias: Orcastrator job notification
trigger:
  - platform: event
    event_type: orcastrator_job
condition:
  - condition: template
    value_template: "{{ trigger.event.data.state in ['print_started', 'failed', 'cancelled'] }}"
action:
  - action: notify.mobile_app_your_phone
    data:
      title: "Orcastrator: {{ trigger.event.data.printer }}"
      message: >
        {{ trigger.event.data.filename }} — {{ trigger.event.data.state }}
        {% if trigger.event.data.error %}({{ trigger.event.data.error }}){% endif %}
```

Jobs for one printer run one at a time. Klipper is only restarted automatically
when the printer was offline or its power switch was `off` when the job arrived.
A printer that was already powered on and in `error`/`shutdown` (thermal runaway,
etc.) fails the job instead.

`.gcode.3mf` uploads are unpacked (plate chosen by OrcaSlicer's `plateindex`).
Binary G-code (`.bgcode`) is not supported.

## Development

```sh
uv venv .venv --python 3.14
uv pip install --python .venv/bin/python pytest-homeassistant-custom-component
ruff check .
pyright
.venv/bin/python -m pytest -v
```
