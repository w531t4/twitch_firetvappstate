# SPDX-FileCopyrightText: 2025 Aaron White <w531t4@gmail.com>
# SPDX-License-Identifier: MIT

from pathlib import Path
import re
import time
import threading
from datetime import datetime
from typing import Optional
import appdaemon.plugins.hass.hassapi as hass

from adb_shell.adb_device import AdbDeviceTcp
from adb_shell.auth.sign_pythonrsa import PythonRSASigner
from twitch_firetvappstate.handshake import Handshake


class TwitchPlayback(hass.Hass):
    """ produce entities describing the state of thw twitch app """

    entity_prefix: str  # prefix to prepend to generated entity values
    session_header: str  # header to key-in on for twitch is-active status
    adb: Optional[AdbDeviceTcp]  # adb device holder
    host: str  # adb host to connect to
    connected: bool  # is adb connected?
    last_playbackstate: Optional[str]  # previous playbackstate
    last_appinfocus: Optional[bool]  # previous appinfocus
    last_playbackactivechannel: Optional[str]  # previous playbackactivechannel
    adbkey: Path  # adb private key
    adbkey_pub: Path  # adb pub key
    _adb_lock: threading.Lock  # serialize the (non-thread-safe) adb socket
    _dump_in_flight: bool  # a background dump worker is running
    dump_deadline_secs: float  # how long the dump worker retries before giving up

    def initialize(self):
        """ get values from apps.yaml """
        self.host = self.args["host"]
        self.port = self.args.get("port", 5555)
        self.adbkey = Path(self.args["adbkey"]).expanduser()
        self.adbkey_pub = (
            Path(self.args["adbkey_pub"]).expanduser()
            if self.args.get("adbkey_pub")
            else Path(str(self.adbkey) + ".pub")
        )
        self.entity_prefix = self.args.get("entity_prefix", "firetv_twitch")
        self.poll_secs = int(self.args.get("poll_interval", 5))
        self.session_header = self.args.get("session_header", "TwitchMediaSession")
        self.dump_deadline_secs = float(self.args.get("dump_deadline_secs", 30))

        self.adb = None
        self.connected = False
        self.last_playbackstate = None
        self.last_appinfocus = None
        self.last_playbackactivechannel = None
        self._adb_lock = threading.Lock()
        self._dump_in_flight = False

        self.run_in(self._loop, 1)

    @property
    def port(self) -> int:
        """adp port to connect to"""
        return self._port

    @port.setter
    def port(self, data) -> None:
        if isinstance(data, str):
            self._port = int(data)
        elif isinstance(data, int):
            self._port = data
        else:
            raise TypeError(f"Expecting str or int, observed={type(data)}")

    @property
    def poll_secs(self) -> int:
        """period for polling adb"""
        return self._poll_secs

    @poll_secs.setter
    def poll_secs(self, data) -> None:
        if isinstance(data, str):
            self._poll_secs = int(data)
        elif isinstance(data, int):
            self._poll_secs = data
        else:
            raise TypeError(f"Expecting str or int, observed={type(data)}")

    # ----------- ADB plumbing (adb-shell) -----------

    def _load_signer(self) -> PythonRSASigner:
        if not self.adbkey.exists():
            raise FileNotFoundError(f"ADB pri key missing: {self.adbkey}")
        elif not self.adbkey_pub.exists():
            raise FileNotFoundError(f"ADB pub key missing: {self.adbkey_pub}")
        return Handshake.load_signer(priv=self.adbkey, pub=self.adbkey_pub)

    def _connect(self):
        try:
            signer = self._load_signer()
            self.adb = AdbDeviceTcp(self.host, self.port, default_transport_timeout_s=10.0)
            ok = self.adb.connect(rsa_keys=[signer], auth_timeout_s=10.0)
            self.connected = bool(ok)
            if self.connected:
                self.log(f"ADB connected to {self.host}:{self.port}")
            else:
                self.error("ADB connect returned falsy result")
        except Exception as e:
            self.connected = False
            self.adb = None
            self.error(f"ADB connect error: {e}")

    def _adb_shell(self, cmd: str) -> str:
        if not self.connected or not self.adb:
            return ""
        try:
            with self._adb_lock:  # serialize; adb socket isn't thread-safe
                data = self.adb.shell(cmd)
            if data and isinstance(data, bytes):
                return data.decode("utf-8")
            elif data and isinstance(data, str):
                return data
            else:
                return ""
        except Exception as e:
            self.error(f"adb shell error for '{cmd}': {e}")
            self.connected = False
            try:
                self.adb.close()
            except Exception:
                pass
            self.adb = None
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
            (r"TwitchMediaSession\s+tv\.twitch\.android\.viewer/.*?(?:\n.*){0,40}?"
             r"PlaybackState\s*\{[^}]*\bstate\s*=\s*(\d+)\b"),
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
        self.set_state(sensor_ent,
                       state=state_val if state_val is not None else "unknown",
                       attributes=attrs)

        # binary_sensor: on when state==3
        bin_ent = f"binary_sensor.{self.entity_prefix}_playing"
        is_playing = state_val == 3
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

        if state_val != self.last_playbackstate:
            self.last_playbackstate = state_val
            self.fire_event(
                "twitch_playback_state_changed",
                host=self.host,
                state=state_val,
                playing=is_playing,
            )

    def _parse_twitch_appinfocus(self, text: str):
        """
        Return Twitch PlaybackState 'state' (int) or None.
        Strategy: find the Twitch header line, then scan the next ~40 lines
        for the first 'PlaybackState {state=...}'.
        """
        if not text:
            return None

        # 1) fast path: exact header anchor (cheap and reliable)
        anchor = "tv.twitch.android.viewer"
        return any([anchor in x and "mCurrentFocus=" in x for x in text.split("\n")])

    def _publish_twitch_appinfocus(self, state_val):
        updated_iso = datetime.utcnow().isoformat() + "Z"

        bin_ent = f"binary_sensor.{self.entity_prefix}_is_focused"
        is_focused = state_val
        self.set_state(
            bin_ent,
            state="on" if is_focused else "off",
            attributes={
                "friendly_name": f"{self.entity_prefix} is focused",
                "device_class": "running",
                "updated": updated_iso,
                "source": "dumpsys media_session",
            },
        )

        if state_val != self.last_appinfocus:
            self.last_appinfocus = state_val
            self.fire_event(
                "twitch_is_focused_changed",
                host=self.host,
                state=state_val,
            )

    def _publish_twitch_playbackactivechannel(self, state_val):
        updated_iso = datetime.utcnow().isoformat() + "Z"

        # numeric sensor
        sensor_ent = f"sensor.{self.entity_prefix}_playback_channel"
        attrs = {
            "friendly_name": f"{self.entity_prefix} playback channel",
            "updated": updated_iso,
        }
        self.set_state(sensor_ent,
                       state=state_val if state_val is not None else "unknown",
                       attributes=attrs)

        if state_val != self.last_playbackactivechannel:
            self.last_playbackactivechannel = state_val
            self.fire_event(
                "twitch_playback_active_channel_changed",
                host=self.host,
                state=state_val,
            )

    # ----------- Main loop -----------
    def _uia_dump_xml(self) -> Optional[str]:
        """One dump attempt: write the UI hierarchy to a file, then read it back.
        Returns the XML, or None if the (flaky) dump failed. _dump_worker retries."""
        dump_path = "/sdcard/window_dump.xml"
        out = self._adb_shell(f"uiautomator dump --compressed {dump_path} 2>&1")
        if f"UI hierchary dumped to: {dump_path}" not in out:
            return None
        xml_text = self._adb_shell(f"cat {dump_path}")
        if not xml_text or "<hierarchy" not in xml_text:
            self.error(f"Failed to read UI dump from {dump_path}; "
                       f"cat returned: {repr(xml_text)[:120]}")
            return None
        return xml_text

    def _dump_worker(self) -> Optional[str]:
        """Off-thread: retry the flaky dump up to dump_deadline_secs. Returns the
        streamer name, or None on failure. Must not raise (else the callback is
        skipped and _dump_in_flight never clears)."""
        deadline = time.monotonic() + self.dump_deadline_secs
        while time.monotonic() < deadline:
            try:
                name = self.find_streamer_name(self._uia_dump_xml() or "")
            except Exception as e:
                self.error(f"dump worker error: {e}")
                name = None
            if name:
                return name
            time.sleep(1.0)
        return None

    def _on_dump_result(self, result, **kwargs):
        """submit_to_executor callback (runs on a normal worker thread). Publish
        the channel, or "unknown" on failure so breakage stays visible."""
        self._dump_in_flight = False
        self._publish_twitch_playbackactivechannel(result or "unknown")

    def _loop(self, _):
        try:
            if not self.connected or self.adb is None:
                self._connect()

            if self.connected:
                # Determine if twitch app is in current focus
                out = self._adb_shell("dumpsys window")
                state_val = self._parse_twitch_appinfocus(out) if out else None
                self._publish_twitch_appinfocus(state_val)
                out = None
                state_val = None

                # Determine playback state of twitch app
                out = self._adb_shell("dumpsys media_session")
                state_val = self._parse_twitch_playbackstate(out) if out else None
                self._publish_twitch_playbackstate(state_val)
                out = None
                state_val = None

                # Determine what channel is currently being watched.
                # get_state returns the "on"/"off" strings; compare explicitly
                # since "off" is truthy.
                is_focused = self.get_state("binary_sensor.firetv_twitch_is_focused") == "on"
                is_playing = self.get_state("binary_sensor.firetv_twitch_playing") == "on"
                if is_focused and is_playing:
                    # Dump is flaky/slow: run off-thread so _loop stays under
                    # AppDaemon's 10s limit; _on_dump_result publishes it.
                    if not self._dump_in_flight:
                        self._dump_in_flight = True
                        self.submit_to_executor(self._dump_worker, callback=self._on_dump_result)
                else:
                    self._publish_twitch_playbackactivechannel("unknown")

        except Exception as e:
            self.error(f"Poll error: {e}")
            self.connected = False
        finally:
            self.run_in(self._loop, self.poll_secs)

    @staticmethod
    def find_streamer_name(xml_text: str) -> str | None:
        """
        find stramers name in text blob
        """
        match = re.search(r"\"Go to (?P<name>\S+)'s profile(?:\.\.\.)?", xml_text)
        if match:
            return match.group("name")
        return None
