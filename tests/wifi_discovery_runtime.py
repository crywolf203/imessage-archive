"""Real netmuxd/Bonjour/libimobiledevice integration with synthetic pairing only.

The fixture has no TLS or heartbeat: hardware testing must validate those on iOS.
"""

import inspect
import os
import plistlib
import socket
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from pymobiledevice3.lockdown import create_using_usbmux
from zeroconf import IPVersion, ServiceInfo, Zeroconf


UDID = "00008140-1234567890ABCDEF"
MAC = "02:00:00:00:00:42"
SERVICE = "_apple-mobdev2._tcp.local."
REQUESTS = []
for name in ("serial", "autopair", "connection_type", "pair_record", "pairing_records_cache_folder"):
    assert name in inspect.signature(create_using_usbmux).parameters, name


def exact(stream, length):
    data = b""
    while len(data) < length:
        block = stream.recv(length - len(data))
        if not block:
            raise EOFError
        data += block
    return data


class FakeLockdown(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(15)
        try:
            while True:
                size, = struct.unpack(">I", exact(self.request, 4))
                assert size <= 1024 * 1024
                query = plistlib.loads(exact(self.request, size))
                action = query.get("Request")
                REQUESTS.append((action, query.get("Key")))
                response = {"Request": action}
                if action == "QueryType":
                    response["Type"] = "com.apple.mobile.lockdown"
                elif action == "GetValue":
                    values = {"UniqueDeviceID": UDID, "ProductVersion": "99.0", "DeviceClass": "iPhone"}
                    response["Value"] = values.get(query.get("Key"), "") if query.get("Key") else values
                elif action == "StartSession":
                    response.update({"SessionID": "synthetic-session", "EnableSessionSSL": False})
                elif action not in ("StopSession", "ValidatePair"):
                    response["Error"] = "InvalidRequest"
                payload = plistlib.dumps(response)
                self.request.sendall(struct.pack(">I", len(payload)) + payload)
        except (EOFError, ConnectionError, TimeoutError):
            pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    # Select the test container's default interface without sending network traffic.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
        route.connect(("192.0.2.1", 9))
        address = route.getsockname()[0]
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Synthetic Wi-Fi regression fixture")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1))
            .sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM))
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                serialization.NoEncryption())
    record = {"HostID": "11111111-1111-1111-1111-111111111111",
              "SystemBUID": "22222222-2222-2222-2222-222222222222", "WiFiMACAddress": MAC,
              "HostCertificate": cert, "RootCertificate": cert, "DeviceCertificate": cert,
              "HostPrivateKey": private, "RootPrivateKey": private}
    with tempfile.TemporaryDirectory(prefix="wifi-regression-") as temporary:
        root = Path(temporary)
        records = root / "lockdown"
        records.mkdir()
        (records / (UDID + ".plist")).write_bytes(plistlib.dumps(record))
        (records / "SystemConfiguration.plist").write_bytes(plistlib.dumps({
            "HostID": record["HostID"], "SystemBUID": record["SystemBUID"],
        }))
        original = {path.name: path.read_bytes() for path in records.iterdir()}
        socket_path = root / "wifi.sock"
        with Server(("0.0.0.0", 62078), FakeLockdown) as server, (root / "netmuxd.log").open("w+") as log:
            threading.Thread(target=server.serve_forever, daemon=True).start()
            process = subprocess.Popen([
                sys.argv[1], "--disable-usb", "--disable-heartbeat",
                "--plist-storage", str(records), "--socket-path", str(socket_path),
            ], stdout=log, stderr=subprocess.STDOUT)
            bonjour = Zeroconf(interfaces=[address], ip_version=IPVersion.V4Only)
            advert = ServiceInfo(SERVICE, f"{MAC}@synthetic.{SERVICE}",
                                 addresses=[socket.inet_aton(address)], port=62078,
                                 server="synthetic-iphone.local.")
            try:
                env = {**os.environ, "USBMUXD_SOCKET_ADDRESS": "UNIX:" + str(socket_path)}
                bonjour.register_service(advert)
                for _ in range(60):
                    if process.poll() is not None:
                        raise AssertionError("netmuxd exited before discovery")
                    result = subprocess.run(["idevice_id", "-n"], capture_output=True,
                                            text=True, timeout=5, env=env)
                    if result.returncode == 0 and result.stdout.strip() == UDID:
                        break
                    time.sleep(0.5)
                else:
                    raise AssertionError("The synthetic Bonjour device was not discovered")
                usb = subprocess.run(["idevice_id", "-l"], capture_output=True, text=True,
                                     timeout=5, env=env, check=True)
                assert not usb.stdout.strip(), "A Wi-Fi device was misreported as USB"
                version = subprocess.run(["ideviceinfo", "-n", "-u", UDID, "-k", "ProductVersion"],
                                         capture_output=True, text=True, timeout=15, env=env)
                assert version.returncode == 0, (version.stdout, version.stderr)
                assert version.stdout.strip() == "99.0", version.stdout
                assert original == {path.name: path.read_bytes() for path in records.iterdir()}, \
                    "Discovery changed the synthetic trust records"
                print("Real Bonjour discovery, network-only listing and lockdown query passed (synthetic, no TLS/heartbeat).")
            except Exception:
                print("Synthetic lockdown request sequence:", REQUESTS, file=sys.stderr)
                log.flush()
                log.seek(0)
                print(log.read(), file=sys.stderr)
                raise
            finally:
                bonjour.unregister_service(advert)
                bonjour.close()
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                server.shutdown()


if __name__ == "__main__":
    main()
