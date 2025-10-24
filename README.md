# homeassistant-python-lightautomation
Appdaemon automation for smart lighting in HomeAssistant
# AppDaemon Light Controller (with Adaptive Lighting + Quiet Hours)

A lightweight AppDaemon app for Home Assistant that drives room lighting from motion and ambient light,
supports **manual overrides** (per-light), integrates with **Home Assistant Adaptive Lighting** (targets + manual control),
adds **media-player dimming**, and supports **quiet hours** to block auto-ON/OFF during specified windows.

## Features
- Motion + Lux driven auto-ON/OFF
- Manual override detection (on/off and brightness/CT tweaks) with **echo protection**
- Re-automate button per room
- Media-player dimming while playing, restore when stopped
- **Adaptive Lighting**: use current targets on auto-ON, mark manual control on user tweaks, timed reset
- **Quiet Hours / Block Windows**: block auto-ON, auto-OFF, or both during windows (with weekdays, midnight crossing)

## Install
1. Copy `apps/lights.py` to your AppDaemon container at `/config/apps/lights.py`.
2. Add apps to your `apps.yaml` (see examples below).
3. Restart AppDaemon.

> Requires: AppDaemon 4.x, Home Assistant, and (optional) the [Adaptive Lighting](https://github.com/basnijholt/adaptive-lighting) integration.

## Example `apps.yaml`
See [`examples/apps.yaml`](examples/apps.yaml) for a full set. Minimal example:

```yaml
light_automation_manager:
  module: lights
  class: LightAutomationManager
  apps:
    - kitchen_light

kitchen_light:
  module: lights
  class: LightController
  light: light.kitchen
  triggers:
    - presence:
        - binary_sensor.kitchen_motion
  delay_off: 120
  lux_sensor: sensor.kitchen_illuminance
  lux_threshold: 20
  only_when_dark: true

  # Adaptive Lighting
  adaptive_lighting_switch: switch.adaptive_lighting_kitchen
  al_use_targets: true
  al_take_over_on_manual: true
  al_manual_reset_seconds: 900

  # Block only auto-ON during quiet hours
  block_windows:
    - start: "00:00"
      end: "06:30"
      days: [mon, tue, wed, thu, fri, sat, sun]
      actions: on
