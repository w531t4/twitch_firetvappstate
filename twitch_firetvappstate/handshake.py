# Copyright (c) 2025 w531t4
#
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license text.

from typing import Dict, Any
from pathlib import Path
import appdaemon.plugins.hass.hassapi as hass
# pip install adb-shell
from adb_shell.adb_device import AdbDeviceTcp
from adb_shell.auth.sign_pythonrsa import PythonRSASigner
from adb_shell.auth.keygen import keygen  # available in adb-shell

class Handshake(hass.Hass):
    def initialize(self):
        """ Gather attributes from apps.yaml, otherwise set defaults """
        self.args: Dict[str, Any]
        self.out_dir: Path = Path(self.args.get("out_dir", "/config/apps"))
        self.out_file: Path = Path(self.args.get("out_file", "firetvappstate.key"))
        self.host = self.args.get("host")
        self.port = self.args.get("port")

        # run once, then on every change
        self.run_in(self._build, 1)

    def ensure_keys(self, key_path: Path) -> tuple[Path, Path]:
        priv = key_path
        pub = key_path.with_suffix(key_path.suffix + ".pub") if key_path.suffix else Path(str(key_path) + ".pub")
        priv.parent.mkdir(parents=True, exist_ok=True)
        if not priv.exists() or not pub.exists():
            self.log(f"Generating ADB keypair at {priv} ...", level="INFO")
            keygen(str(priv))  # creates both priv and pub
            # Tighten permissions (especially if running on Linux)
            priv.chmod(0o600)
        return priv, pub

    @staticmethod
    def load_signer(priv: Path, pub: Path) -> PythonRSASigner:
        with open(priv, "rb") as fpriv, open(pub, "rb") as fpub:
            priv_data = fpriv.read()
            pub_data = fpub.read()
        return PythonRSASigner(pub_data, priv_data)

    def _build(self, kwargs) -> None:
        host = self.host
        try:
            port = int(self.port)
        except ValueError:
            self.log(f"PORT must be an integer (e.g., 5555).", level="ERROR")
            raise

        # key_path = Path(sys.argv[3]) if len(sys.argv) >= 4 else Path("/config/.android/adbkey")
        key_path = self.out_dir / str(self.out_file)
        priv, pub = self.ensure_keys(key_path)
        signer = self.load_signer(priv, pub)

        # Create device object
        dev = AdbDeviceTcp(host, port, default_transport_timeout_s=10.0)

        # Connect: the first time, your TV may show “Allow USB debugging?”; accept it once.
        self.log(f"Connecting to {host}:{port} using key {priv} ...", level="INFO")
        ok = dev.connect(rsa_keys=[signer], auth_timeout_s=15.0)

        if not ok:
            self.log("ADB connect returned False (auth failed or device unreachable).", level="ERROR")
            raise ValueError("expected ok=True.. observed ok={ok}")
        self.log(f"Connected. Running a simple check (getprop ro.product.model)...", level="INFO")
        out = dev.shell("getprop ro.product.model")
        self.log(f"Model: {out.strip()}", level="INFO")

        # Cleanly close the socket
        dev.close()
        self.log(f"Done.", level="INFO")
