import time
from datetime import datetime, timedelta
import appdaemon.plugins.hass.hassapi as hass


def _now_ts() -> float:
    return time.time()


def _as_list(val):
    if val is None:
        return []
    if isinstance(val, (list, tuple)):
        return list(val)
    return [val]


class LightController(hass.Hass):
    def initialize(self):
        self._app_name = self.name
        cfg = self.args or {}

        # Lights (required)
        lights = cfg.get("light") or cfg.get("lights")
        self._lights = _as_list(lights)
        if not self._lights:
            raise ValueError("lights: must list one or more light entities")

        # Presence/motion sensors
        self._motion_entities = []
        for trig in _as_list(cfg.get("triggers")):
            if isinstance(trig, dict) and "presence" in trig:
                self._motion_entities.extend(_as_list(trig["presence"]))
        self._motion_entities = list(dict.fromkeys(self._motion_entities))

        # Lux / darkness
        self._lux_sensor = cfg.get("lux_sensor")
        self._lux_threshold = cfg.get("lux_threshold")
        self._only_when_dark = bool(cfg.get("only_when_dark", True))

        # Timings (delays, re-auto, boot grace)
        self._delay_off = float(cfg.get("delay_off", 60))
        self._manual_off_reauto_delay = float(cfg.get("manual_off_reautomate_delay", 3600))
        self._motion_reauto_seconds = float(cfg.get("motion_reauto_seconds", 5))
        self._boot_grace = float(cfg.get("boot_grace", 60))

        # Echo protection (per-entity)
        self._echo_window = float(cfg.get("echo_window", 3))
        self._echo_max_window = float(cfg.get("echo_max_window", 60))
        self._expected_echo = {}          # entity -> {"until": ts, "expected": "on"/"off", "logged": False}
        self._last_cmd_by_entity = {}     # entity -> ("on"|"off", ts)

        # Media-player dimming (optional dim when media players are playing)
        self._media_players = _as_list(cfg.get("media_players") or cfg.get("media_player"))
        self._media_dim_brightness_pct = cfg.get("media_dim_brightness_pct")
        self._auto_brightness_pct = cfg.get("auto_brightness_pct")  # optional default brightness for auto ON
        self._media_playing = False
        self._before_media_brightness_pct = {}  # light.entity -> pct before we dimmed

        # --- Adaptive Lighting integration (HA Adaptive Lighting) ---
        self._al_switch = cfg.get("adaptive_lighting_switch")
        self._al_use_targets = bool(cfg.get("al_use_targets", True))
        self._al_take_over_on_manual = bool(cfg.get("al_take_over_on_manual", True))
        self._al_manual_reset_seconds = cfg.get("al_manual_reset_seconds", 900)
        try:
            self._al_manual_reset_seconds = (int(self._al_manual_reset_seconds)
                                             if self._al_manual_reset_seconds is not None else None)
        except Exception:
            self._al_manual_reset_seconds = 900
        self._al_adapt_brightness = cfg.get("al_adapt_brightness")  # True/False/None
        self._al_adapt_color = cfg.get("al_adapt_color")            # True/False/None

        # Tracking for AL manual-control timers (per entity)
        self._al_manual_timers = {}   # entity -> handle

        # --- Time-based blocking ---
        # Either provide a single window via quiet_start/quiet_end ("HH:MM")
        # or multiple windows via block_windows: [{start: "22:00", end: "07:00", days: ["mon","tue",...], actions: "on|off|on_off"}]
        self._quiet_start = cfg.get("quiet_start")  # e.g. "22:00"
        self._quiet_end = cfg.get("quiet_end")      # e.g. "07:00"
        self._block_windows = _as_list(cfg.get("block_windows"))
        # Normalize actions (accepts bool or string)
        self._block_actions_default = self._norm_actions(cfg.get("block_actions")) or "on_off"  # on|off|on_off

        # Internal state
        self._mode = "auto"   # auto | manual_on | manual_off
        self._presence = False
        self._off_timer = None
        self._motion_reauto_timer = None
        self._started_ts = _now_ts()

        # Helper entities
        self._state_sensor = f"sensor.light_state_{self._app_name}"
        self._status_sensor = f"sensor.light_status_{self._app_name}"
        self._reauto_button = f"button.reautomate_{self._app_name}"

        # Create virtual entities
        self.set_state(self._state_sensor, state="unknown", attributes={"app": self._app_name})
        self.set_state(
            self._status_sensor,
            state=self._mode,
            attributes={
                "manual_state": self._mode,
                "reautomate_button": self._reauto_button,
                "lights": self._lights,
                "motion": self._motion_entities,
                "lux_sensor": self._lux_sensor,
                "media_players": self._media_players,
                "adaptive_lighting_switch": self._al_switch,
            },
        )
        self.set_state(
            self._reauto_button,
            state="idle",
            attributes={"friendly_name": f"Re-automate {self._app_name}"},
        )

        # Listeners
        for ent in self._motion_entities:
            self.listen_state(self._on_motion, ent)
        for l in self._lights:
            # Separate listeners: power and key attributes
            self.listen_state(self._on_light_power, l)  # default attribute="state"
            self.listen_state(self._on_light_attr, l, attribute="brightness")
            self.listen_state(self._on_light_attr, l, attribute="brightness_pct")
            self.listen_state(self._on_light_attr, l, attribute="color_temp")
            self.listen_state(self._on_light_attr, l, attribute="color_temp_kelvin")
        if self._lux_sensor:
            self.listen_state(self._on_lux_changed, self._lux_sensor)
        for mp in self._media_players:
            self.listen_state(self._on_media_state, mp)

        # Button press via call_service event
        self.listen_event(self._on_button_press, "call_service", domain="button", service="press")

        self._logx(f"Initialized | {self._snapshot()}", level="INFO")

    # ---- Logging / snapshots ----
    def _logx(self, msg, level="INFO"):
        super().log(f"[{self._app_name}] {msg}", level=level)

    def _snapshot(self):
        return {
            "lights": self._lights,
            "motion": self._motion_entities or None,
            "lux": self._lux_sensor or None,
            "only_when_dark": self._only_when_dark,
            "delay_off": self._delay_off,
            "manual_off_reauto_delay": self._manual_off_reauto_delay,
            "motion_reauto_seconds": self._motion_reauto_seconds,
            "echo_window": self._echo_window,
            "echo_max_window": self._echo_max_window,
            "boot_grace": self._boot_grace,
            "media_players": self._media_players,
            "media_dim_brightness_pct": self._media_dim_brightness_pct,
            "auto_brightness_pct": self._auto_brightness_pct,
            "adaptive_lighting_switch": self._al_switch,
            "al_use_targets": self._al_use_targets,
            "al_take_over_on_manual": self._al_take_over_on_manual,
            "al_manual_reset_seconds": self._al_manual_reset_seconds,
        }

    def _publish_status(self):
        self.set_state(
            self._status_sensor,
            state=("auto" if self._mode == "auto" else self._mode),
            attributes={
                "manual_state": self._mode,
                "presence": self._presence,
                "only_when_dark": self._only_when_dark,
                "lux_sensor": self._lux_sensor,
                "media_players": self._media_players,
                "media_playing": self._media_playing,
                "reautomate_button": self._reauto_button,
                "adaptive_lighting_switch": self._al_switch,
            },
        )

    def _publish_debug_state(self):
        self.set_state(
            self._state_sensor,
            state=self._mode,
            attributes={
                "mode": self._mode,
                "presence": self._presence,
                "media_playing": self._media_playing,
                "expected_echo": self._expected_echo,
                "now": _now_ts(),
            },
        )

    # ---- Helpers ----
    def _norm_actions(self, val):
        """Normalize actions input (string/bool) to 'on', 'off', or 'on_off'."""
        if isinstance(val, bool):
            return "on_off" if val else None
        if isinstance(val, str):
            v = val.strip().lower()
            if v in ("on", "off", "on_off"):
                return v
        return None

    def _parse_hhmm(self, s):
        try:
            parts = str(s).split(":")
            h, m = int(parts[0]), int(parts[1])
            return h * 60 + m
        except Exception:
            return None

    def _now_minutes_and_day(self):
        dt = datetime.now()
        return dt.hour * 60 + dt.minute, dt.strftime("%a").lower()

    def _within_window(self, start_min, end_min, now_min):
        if start_min is None or end_min is None:
            return False
        if start_min == end_min:
            return False
        if start_min < end_min:
            return start_min <= now_min < end_min
        return now_min >= start_min or now_min < end_min  # crosses midnight

    def _blocked_now(self):
        """Return (blocked: bool, reason: str|None)."""
        now_min, day = self._now_minutes_and_day()
        for w in self._block_windows:
            try:
                s = self._parse_hhmm(w.get("start"))
                e = self._parse_hhmm(w.get("end"))
                if not self._within_window(s, e, now_min):
                    continue
                days = [d.lower()[:3] for d in _as_list(w.get("days"))] if w.get("days") else None
                if days and day[:3] not in days:
                    continue
                actions = self._norm_actions(w.get("actions")) or self._block_actions_default
                return True, f"{actions} {w.get('start')}-{w.get('end')}"
            except Exception:
                continue
        s = self._parse_hhmm(self._quiet_start) if self._quiet_start else None
        e = self._parse_hhmm(self._quiet_end) if self._quiet_end else None
        if self._within_window(s, e, now_min):
            return True, f"{self._block_actions_default} {self._quiet_start}-{self._quiet_end}"
        return False, None

    def _automation_allowed(self, action: str) -> bool:
        """action: 'on' or 'off'"""
        blocked, why = self._blocked_now()
        if not blocked:
            return True
        if "on_off" in (why or ""):
            return False
        if action == "on" and " on" in (why or ""):
            return False
        if action == "off" and " off" in (why or ""):
            return False
        return True

    def _is_dark_enough(self) -> bool:
        if not self._only_when_dark or not self._lux_sensor:
            return True
        val = self.get_state(self._lux_sensor)
        try:
            lux = float(val) if val is not None else None
        except Exception:
            lux = None
        if lux is None:
            return True
        low = self._lux_threshold
        if low is None:
            return True
        return lux <= float(low)

    def _any_light_on(self) -> bool:
        for l in self._lights:
            if (self.get_state(l) or "").lower() == "on":
                return True
        return False

    def _any_motion_on(self) -> bool:
        for ent in self._motion_entities:
            if (self.get_state(ent) or "").lower() in ("on", "home", "occupied", "true", "1"):
                return True
        return False

    def _cancel_timer_safe(self, handle):
        if not handle:
            return
        try:
            self.cancel_timer(handle)
        except Exception:
            pass

    # ---- Echo protection ----
    def _mark_expected_echo(self, state: str):
        now = _now_ts()
        until = min(now + self._echo_window, now + self._echo_max_window)
        for l in self._lights:
            self._expected_echo[l] = {"until": until, "expected": state, "logged": False}
            self._last_cmd_by_entity[l] = (state, now)

    def _ignore_if_expected_echo(self, entity: str, old: str, new: str) -> bool:
        exp = self._expected_echo.get(entity)
        if not exp:
            return False
        now = _now_ts()
        if now > exp["until"]:
            self._expected_echo.pop(entity, None)
            return False
        old_on = (old or "").lower() == "on"
        new_on = (new or "").lower() == "on"
        if exp["expected"] == "on" and (not old_on) and new_on:
            if not exp["logged"]:
                self._logx("Ignoring echo from our own service call.")
                exp["logged"] = True
            return True
        if exp["expected"] == "off" and old_on and (not new_on):
            if not exp["logged"]:
                self._logx("Ignoring echo from our own service call.")
                exp["logged"] = True
            return True
        return False

    # Small grace for attribute updates right after our own command
    def _recent_app_change(self, entity: str, seconds: float = None) -> bool:
        if seconds is None:
            seconds = max(self._echo_window, 3.0)
        last = self._last_cmd_by_entity.get(entity)
        if not last:
            return False
        _, ts = last
        return (_now_ts() - ts) <= seconds

    # ---- Adaptive Lighting helpers ----
    def _mireds_to_kelvin(self, mireds):
        try:
            m = float(mireds)
            if m <= 0:
                return None
            return int(round(1000000.0 / m))
        except Exception:
            return None

    def _al_switch_attrs(self):
        if not self._al_switch:
            return {}
        return self.get_state(self._al_switch, attribute="attributes") or {}

    def _al_current_targets(self):
        """Return (brightness_pct, color_temp_kelvin, adapt_brightness, adapt_color)."""
        attrs = self._al_switch_attrs()
        bri = attrs.get("brightness_pct")
        if bri is None:
            b2 = attrs.get("brightness")
            if isinstance(b2, (int, float)):
                bri = int(b2)
        ct_kelvin = attrs.get("color_temp_kelvin")
        if ct_kelvin is None:
            m = attrs.get("color_temp")
            if m is not None:
                ct_kelvin = self._mireds_to_kelvin(m)
        adapt_b = self._al_adapt_brightness
        if adapt_b is None:
            adapt_b = bool(attrs.get("adapt_brightness", True))
        adapt_c = self._al_adapt_color
        if adapt_c is None:
            adapt_c = bool(attrs.get("adapt_color", True))
        return bri, ct_kelvin, adapt_b, adapt_c

    def _al_set_manual_control(self, lights=None):
        if not self._al_switch or not self._al_take_over_on_manual:
            return
        try:
            self.call_service(
                "adaptive_lighting/set_manual_control",
                entity_id=self._al_switch,
                lights=lights or self._lights,
            )
            self._logx("Told Adaptive Lighting to enable manual control for these lights.")
        except Exception as e:
            self._logx(f"Failed to call adaptive_lighting.set_manual_control: {e}", level="WARNING")

    def _al_reset(self, lights=None):
        if not self._al_switch:
            return
        try:
            self.call_service(
                "adaptive_lighting/reset",
                entity_id=self._al_switch,
                lights=lights or self._lights,
            )
            self._logx("Reset manual control on Adaptive Lighting for these lights.")
        except Exception as e:
            self._logx(f"Failed to call adaptive_lighting.reset: {e}", level="WARNING")

    def _al_schedule_reset_for(self, entity):
        h = self._al_manual_timers.pop(entity, None)
        self._cancel_timer_safe(h)
        if not self._al_manual_reset_seconds:
            return
        self._al_manual_timers[entity] = self.run_in(
            self._al_reset_timer_cb, self._al_manual_reset_seconds, entity=entity
        )

    def _al_reset_timer_cb(self, kwargs):
        ent = kwargs.get("entity")
        self._al_manual_timers.pop(ent, None)
        self._logx(f"Manual window elapsed for {ent}; resetting Adaptive Lighting control.")
        self._al_reset(lights=[ent])

    def _al_is_change_like_al(self, entity, attr, old, new) -> bool:
        """Return True if a brightness/CT change looks like Adaptive Lighting's own nudge."""
        if not self._al_switch or attr not in ("brightness", "brightness_pct", "color_temp", "color_temp_kelvin"):
            return False
        bri_t, ct_k_t, _, _ = self._al_current_targets()
        try:
            if attr in ("brightness", "brightness_pct") and bri_t is not None:
                val = None
                if attr == "brightness":
                    if isinstance(new, (int, float)):
                        val = int(round((float(new) / 255.0) * 100))
                else:
                    val = int(new) if new is not None else None
                if val is not None and abs(int(bri_t) - int(val)) <= 3:
                    return True
            if attr in ("color_temp", "color_temp_kelvin") and ct_k_t is not None:
                valk = None
                if attr == "color_temp":
                    valk = self._mireds_to_kelvin(new)
                else:
                    valk = int(new) if new is not None else None
                if valk is not None and abs(int(ct_k_t) - int(valk)) <= 150:
                    return True
        except Exception:
            return False
        return False

    # ---- Light control ----
    def _turn_on(self, reason="auto"):
        self._mark_expected_echo("on")
        # Prefer Adaptive Lighting targets if configured
        bri_target = None
        ct_kelvin = None
        adapt_b = adapt_c = True
        if self._al_switch and self._al_use_targets:
            bri_target, ct_kelvin, adapt_b, adapt_c = self._al_current_targets()

        # Choose brightness based on context (media dim overrides AL brightness target)
        bp = None
        if self._media_playing and self._media_dim_brightness_pct is not None:
            bp = int(self._media_dim_brightness_pct)
        elif self._auto_brightness_pct is not None:
            bp = int(self._auto_brightness_pct)
        if bp is None and bri_target is not None and adapt_b:
            bp = int(bri_target)

        for l in self._lights:
            if l.startswith("switch."):
                self.call_service("switch/turn_on", entity_id=l)
            else:
                data = {"entity_id": l}
                if bp is not None:
                    data["brightness_pct"] = int(bp)
                if ct_kelvin is not None and adapt_c:
                    data["color_temp_kelvin"] = int(ct_kelvin)
                self.call_service("light/turn_on", **data)
        self._logx(f"Turn ON ({reason}).")

    def _turn_off(self, reason="auto"):
        self._mark_expected_echo("off")
        for l in self._lights:
            if l.startswith("switch."):
                self.call_service("switch/turn_off", entity_id=l)
            else:
                self.call_service("light/turn_off", entity_id=l)
        self._logx(f"Turn OFF ({reason}).")

    def _schedule_off(self):
        self._cancel_timer_safe(self._off_timer)
        self._off_timer = self.run_in(self._auto_off_elapsed, self._delay_off)
        self._logx(f"Scheduled auto-off in {int(self._delay_off)}s.")

    def _auto_off_elapsed(self, kwargs):
        if self._mode == "auto" and not self._presence:
            if self._automation_allowed("off"):
                self._logx("Auto-off timer elapsed; turning light off.")
                self._turn_off("auto_off")
            else:
                self._logx("Auto-off elapsed but OFF is blocked; keeping lights as-is.")
        self._off_timer = None

    # ---- Event handlers ----
    def _on_motion(self, entity, attribute, old, new, kwargs):
        is_on = (new or "").lower() in ("on", "home", "occupied", "true", "1")
        self._presence = is_on

        if self._mode == "manual_on" and is_on:
            self._cancel_timer_safe(self._motion_reauto_timer)
            self._motion_reauto_timer = self.run_in(self._reautomate_from_motion, self._motion_reauto_seconds)
            self._logx("Motion during manual_on; scheduling quick re-automate.", level="INFO")

        if self._mode != "auto":
            self._publish_status(); self._publish_debug_state(); return

        if is_on:
            self._cancel_timer_safe(self._off_timer)
            if not self._automation_allowed("on"):
                self._logx("Motion but automation is blocked now; ignoring ON.")
                self._publish_status(); self._publish_debug_state(); return
            if self._is_dark_enough():
                if not self._any_light_on():
                    self._turn_on("motion")
                elif self._media_playing and self._media_dim_brightness_pct is not None:
                    self._apply_media_dimming()
            else:
                self._logx("Motion but not dark; leaving lights as-is.")
        else:
            if not self._any_motion_on():
                if self._automation_allowed("off"):
                    self._schedule_off()
                else:
                    self._logx("Motion cleared but OFF is blocked now; leaving lights as-is.")

        self._publish_status(); self._publish_debug_state()

    def _on_light_power(self, entity, attribute, old, new, kwargs):
        # attribute == "state" here
        if (_now_ts() - self._started_ts) < self._boot_grace:
            return
        if self._ignore_if_expected_echo(entity, old, new):
            return

        new_on = (new or "").lower() == "on"
        old_on = (old or "").lower() == "on"

        if new_on and not old_on:
            if self._mode != "manual_on":
                self._mode = "manual_on"
                self._cancel_timer_safe(self._off_timer)
                self._logx(f"Manual ON; pausing automation. (source=state_change:{entity}=on)")
                self._al_set_manual_control(lights=[entity])
                self._al_schedule_reset_for(entity)
        elif (not new_on) and old_on:
            if self._mode != "manual_off":
                self._mode = "manual_off"
                self._cancel_timer_safe(self._off_timer)
                self._logx(
                    f"Manual OFF (source=state_change:{entity}=off); will re-automate in {int(self._manual_off_reauto_delay)}s."
                )
                self.run_in(self._reautomate_from_manual_off, self._manual_off_reauto_delay)

        self._publish_status(); self._publish_debug_state()

    def _changed_meaningfully(self, attribute, old, new) -> bool:
        try:
            if attribute in ("brightness", "brightness_pct"):
                o = float(old or 0); n = float(new or 0)
                return abs(n - o) >= 5  # >=5% change
            if attribute in ("color_temp", "color_temp_kelvin"):
                o = float(old or 0); n = float(new or 0)
                if attribute.endswith("kelvin"):
                    return abs(n - o) >= 100  # >=100 K
                return abs(n - o) >= 5      # >=5 mireds
        except Exception:
            pass
        return True

    def _on_light_attr(self, entity, attribute, old, new, kwargs):
        # brightness / color temp manual adjustments
        if (_now_ts() - self._started_ts) < self._boot_grace:
            return
        # Ignore attribute flips that land right after our own ON/OFF
        if self._recent_app_change(entity):
            return
        # If this looks like AL's own adaptation, ignore.
        if self._al_is_change_like_al(entity, attribute, old, new):
            return
        # Ignore tiny jitters
        if not self._changed_meaningfully(attribute, old, new):
            return
        # Only react when the light is on.
        if (self.get_state(entity) or "").lower() != "on":
            return

        if self._mode != "manual_on":
            self._mode = "manual_on"
            self._cancel_timer_safe(self._off_timer)
            self._logx(f"Manual tweak on {entity} ({attribute}) -> entering manual_on and pausing automation.")
        self._al_set_manual_control(lights=[entity])
        self._al_schedule_reset_for(entity)
        self._publish_status(); self._publish_debug_state()

    def _on_lux_changed(self, entity, attribute, old, new, kwargs):
        try:
            lux = float(new) if new is not None else None
        except Exception:
            lux = None
        self._logx(f"Lux changed: {lux}")
        if self._mode == "auto" and self._presence and self._is_dark_enough():
            if not self._any_light_on():
                if self._automation_allowed("on"):
                    self._turn_on("lux_dark_now")
                else:
                    self._logx("Dark now but ON is blocked; ignoring.")
            elif self._media_playing and self._media_dim_brightness_pct is not None:
                self._apply_media_dimming()
        self._publish_status(); self._publish_debug_state()

    # ---- Media handling ----
    def _on_media_state(self, entity, attribute, old, new, kwargs):
        playing = (new or "").lower() == "playing"
        self._media_playing = playing
        self._logx(f"Media state on {entity}: {new}")
        if self._mode != "auto":
            self._publish_status(); self._publish_debug_state(); return

        if playing:
            if self._is_dark_enough() and self._any_light_on():
                self._apply_media_dimming()
        else:
            if self._any_light_on():
                self._restore_from_media()
        self._publish_status(); self._publish_debug_state()

    def _apply_media_dimming(self):
        if self._media_dim_brightness_pct is None:
            return
        target = int(self._media_dim_brightness_pct)
        changed = False
        for l in self._lights:
            if not l.startswith("light."):
                continue
            attrs = self.get_state(l, attribute="attributes") or {}
            bri = attrs.get("brightness")
            if l not in self._before_media_brightness_pct:
                if isinstance(bri, (int, float)):
                    pct = max(1, min(100, int(round((bri / 255) * 100))))
                else:
                    pct = None
                self._before_media_brightness_pct[l] = pct
            self.call_service("light/turn_on", entity_id=l, brightness_pct=target)
            changed = True
        if changed:
            self._logx("Media playing -> dimming lights.", level="INFO")

    def _restore_from_media(self):
        if not self._before_media_brightness_pct:
            if self._auto_brightness_pct is not None:
                for l in self._lights:
                    if l.startswith("light."):
                        self.call_service("light/turn_on", entity_id=l, brightness_pct=int(self._auto_brightness_pct))
                self._logx("Media stopped -> setting lights to auto brightness.", level="INFO")
            return
        for l, pct in list(self._before_media_brightness_pct.items()):
            if not l.startswith("light."):
                continue
            if pct is None:
                continue
            self.call_service("light/turn_on", entity_id=l, brightness_pct=int(pct))
        self._before_media_brightness_pct.clear()
        self._logx("Media stopped -> restoring brightness.", level="INFO")

    # ---- Re-automation paths ----
    def _reautomate_from_motion(self, kwargs):
        self._motion_reauto_timer = None
        self._mode = "auto"
        if self._presence and self._is_dark_enough():
            if self._automation_allowed("on"):
                self._logx("Re-automated (motion_reauto); motion present -> ensuring ON under auto.", level="INFO")
                self._turn_on("motion_reauto")
            else:
                self._logx("Re-automated (motion_reauto); ON is blocked now; doing nothing.", level="INFO")
        else:
            self._logx("Re-automated (motion_reauto); no motion -> turning OFF.", level="INFO")
            self._turn_off("motion_reauto")
        self._publish_status(); self._publish_debug_state()

    def _reautomate_from_manual_off(self, kwargs):
        self._mode = "auto"
        if self._presence and self._is_dark_enough():
            if self._automation_allowed("on"):
                self._logx("Re-automated (manual_off_timeout); presence -> turning ON.")
                self._turn_on("manual_off_timeout")
            else:
                self._logx("Re-automated (manual_off_timeout); ON is blocked; doing nothing.")
        else:
            self._logx("Re-automated (manual_off_timeout); no presence or not dark -> turning OFF.")
            self._turn_off("manual_off_timeout")
        self._publish_status(); self._publish_debug_state()

    def _on_button_press(self, event_name, data, kwargs):
        if data.get("domain") != "button" or data.get("service") != "press":
            return
        sd = data.get("service_data") or {}
        ent = sd.get("entity_id")
        if not ent:
            return
        if self._reauto_button not in _as_list(ent):
            return

        self._logx(f"Re-automate accepted from button.press for {self._reauto_button}", level="INFO")
        self._mode = "auto"
        if self._presence and self._is_dark_enough():
            self._logx("Re-automated (button.press); motion present -> ensuring ON under auto.")
            self._turn_on("event")
        else:
            self._logx("Re-automated (button.press); no motion or not dark -> turning OFF.")
            self._turn_off("event")
        self._publish_status(); self._publish_debug_state()


class LightAutomationManager(hass.Hass):
    def initialize(self):
        self._app_name = self.name
        self._managed = _as_list(self.args.get("apps"))
        self._sensor = "binary_sensor.light_automation_manager"
        self.set_state(self._sensor, state="off", attributes={"reautomate_buttons": []})
        self.run_every(self._refresh, "now", 30)
        self.run_in(self._refresh, 5)
        self._logx(f"[LAM] Initialized; managing apps={self._managed}", level="INFO")

    def _logx(self, msg, level="INFO"):
        super().log(f"[{self._app_name}] {msg}", level=level)

    def _refresh(self, kwargs):
        buttons = []
        for app in self._managed:
            status_ent = f"sensor.light_status_{app}"
            st = self.get_state(status_ent, attribute="all")
            if not st or "state" not in st:
                continue
            state = (st["state"] or "").lower()
            attrs = st.get("attributes") or {}
            btn = attrs.get("reautomate_button")
            manual_state = (attrs.get("manual_state") or state).lower()
            if manual_state in ("manual_on", "manual_off") and btn:
                buttons.append(btn)
        self.set_state(self._sensor, state=("on" if buttons else "off"),
                       attributes={"reautomate_buttons": buttons})
