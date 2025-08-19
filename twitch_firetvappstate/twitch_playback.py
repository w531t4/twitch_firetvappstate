from pathlib import Path
import re
from datetime import datetime
import appdaemon.plugins.hass.hassapi as hass

from adb_shell.adb_device import AdbDeviceTcp
from adb_shell.auth.sign_pythonrsa import PythonRSASigner
from twitch_firetvappstate.handshake import Handshake


class TwitchPlayback(hass.Hass):
    def initialize(self):
        self.host = self.args["host"]                 # e.g. 192.168.1.50
        self.port = int(self.args.get("port", 5555))
        self.adbkey = Path(self.args["adbkey"]).expanduser()
        self.adbkey_pub = (
            Path(self.args["adbkey_pub"]).expanduser()
            if self.args.get("adbkey_pub")
            else Path(str(self.adbkey) + ".pub")
        )
        self.entity_prefix = self.args.get("entity_prefix", "firetv_twitch")
        self.poll_secs = int(self.args.get("poll_interval", 5))
        self.session_header = self.args.get("session_header", "TwitchMediaSession")

        self._adb = None
        self._connected = False
        self._last_playbackstate = None

        self.run_in(self._loop, 1)

    # ----------- ADB plumbing (adb-shell) -----------

    def _load_signer(self) -> PythonRSASigner:
        if not self.adbkey.exists():
            raise FileNotFoundError(f"ADB pri key missing: {self.adbkey}")
        elif not self.adbkey_pub.exists():
            raise FileNotFoundError(f"ADB pub key missing: {self.adbkey_pub}")
        return Handshake.load_signer(priv=self.adbkey, pub=self.adbkey_pub)
        # priv = self.adbkey.read_bytes()
        # pub = self.adbkey_pub.read_bytes()
        # return PythonRSASigner.FromRSAKey(priv, pub)

    def _connect(self):
        try:
            signer = self._load_signer()
            self._adb = AdbDeviceTcp(self.host, self.port, default_transport_timeout_s=10.0)
            ok = self._adb.connect(rsa_keys=[signer], auth_timeout_s=10.0)
            self._connected = bool(ok)
            if self._connected:
                self.log(f"ADB connected to {self.host}:{self.port}")
            else:
                self.error("ADB connect returned falsy result")
        except Exception as e:
            self._connected = False
            self._adb = None
            self.error(f"ADB connect error: {e}")

    def _adb_shell(self, cmd: str) -> str:
        if not self._connected or not self._adb:
            return ""
        try:
            return self._adb.shell(cmd) or ""
        except Exception as e:
            self.error(f"adb shell error for '{cmd}': {e}")
            self._connected = False
            try:
                self._adb.close()
            except Exception:
                pass
            self._adb = None
            return ""

    # ----------- Parsing + publishing -----------

    def _parse_twitch_playbackstate(self, text: str):
        """
        Return Twitch PlaybackState 'state' (int) or None.
        Strategy: find the Twitch header line, then scan the next ~40 lines
        for the first 'PlaybackState {state=...}'.
        """
        if not text:
            return None

        # 1) fast path: exact header anchor (cheap and reliable)
        anchor = "TwitchMediaSession tv.twitch.android.viewer/TwitchMediaSession"
        idx = text.find(anchor)
        if idx != -1:
            after = text[idx:].splitlines()
            for line in after[:40]:
                m = re.search(r"PlaybackState\s*\{[^}]*\bstate\s*=\s*(\d+)\b", line)
                if m:
                    return int(m.group(1))
            # fall through if not seen in first 40 lines

        # 2) fallback: header → playback within a limited window (regex)
        m2 = re.search(
            r"TwitchMediaSession\s+tv\.twitch\.android\.viewer/.*?(?:\n.*){0,40}?PlaybackState\s*\{[^}]*\bstate\s*=\s*(\d+)\b",
            text,
            re.DOTALL,
        )
        if m2:
            return int(m2.group(1))

        return None
    def _publish_twitch_playbackstate(self, state_val):
        updated_iso = datetime.utcnow().isoformat() + "Z"

        # numeric sensor
        sensor_ent = f"sensor.{self.entity_prefix}_playback_state"
        attrs = {
            "friendly_name": f"{self.entity_prefix} playback state",
            "updated": updated_iso,
            "meanings": {
                "1": "stopped/idle/menu",
                "3": "playing",
                "6": "transition/unknown (observed)",
            },
        }
        self.set_state(sensor_ent, state=state_val if state_val is not None else "unknown", attributes=attrs)

        # binary_sensor: on when state==3
        bin_ent = f"binary_sensor.{self.entity_prefix}_playing"
        is_playing = (state_val == 3)
        self.set_state(
            bin_ent,
            state="on" if is_playing else "off",
            attributes={
                "friendly_name": f"{self.entity_prefix} playing",
                "device_class": "running",
                "updated": updated_iso,
                "source": "dumpsys media_session",
            },
        )

        if state_val != self._last_playbackstate:
            self._last_playbackstate = state_val
            self.fire_event(
                "twitch_playback_state_changed",
                host=self.host,
                state=state_val,
                playing=is_playing,
            )

    # ----------- Main loop -----------

    def _loop(self, _):
        try:
            if not self._connected or self._adb is None:
                self._connect()

            if self._connected:
                # Determine playback state of twitch app
                out = self._adb_shell("dumpsys media_session")
                state_val = self._parse_twitch_playbackstate(out) if out else None
                self._publish_twitch_playbackstate(state_val)
        except Exception as e:
            self.error(f"Poll error: {e}")
            self._connected = False
        finally:
            self.run_in(self._loop, self.poll_secs)
