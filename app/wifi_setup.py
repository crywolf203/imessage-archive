"""Enable wireless lockdown using an existing USB pairing, never create a new one."""

import argparse
import asyncio
import plistlib
import re
import sys
import tempfile
from pathlib import Path


class SetupError(ValueError):
    """An actionable message that is safe to display without private device data."""


async def configure(udid: str, enable: bool, records: Path, factory=None) -> bool:
    if not re.fullmatch(r"[A-Za-z0-9-]{1,128}", udid):
        raise SetupError("Invalid device identifier.")
    path = records / (udid + ".plist")
    if not path.is_file():
        raise SetupError("No saved USB trust record. Pair this iPhone on Unraid first.")
    record = plistlib.loads(path.read_bytes())
    if not isinstance(record, dict) or not all(
        record.get(key) for key in ("HostID", "SystemBUID", "HostCertificate", "HostPrivateKey")
    ):
        raise SetupError("USB trust record is incomplete. Validate trust before Wi-Fi setup.")
    if factory is None:
        from pymobiledevice3.lockdown import create_using_usbmux

        factory = create_using_usbmux
    # Keep the library's cache/temporary credentials out of persistent pairing records.
    with tempfile.TemporaryDirectory(prefix="imessage-wifi-setup-") as cache:
        client = await factory(
            serial=udid, connection_type="USB", autopair=False,
            pair_record=record, pairing_records_cache_folder=Path(cache),
        )
        async with client:
            if client.udid != udid or not client.paired:
                raise SetupError("USB trust validation failed. Unlock and pair this iPhone first.")
            if enable:
                await client.set_enable_wifi_connections(True)
            result = await client.get_enable_wifi_connections()
            if enable and not result:
                raise SetupError("The iPhone did not enable Wi-Fi connections.")
            return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "enable"))
    parser.add_argument("--udid", required=True)
    arguments = parser.parse_args()
    try:
        enabled = asyncio.run(asyncio.wait_for(
            configure(arguments.udid, arguments.action == "enable", Path("/var/lib/lockdown")),
            timeout=25,
        ))
    except SetupError as error:
        print(str(error), file=sys.stderr)
        return 1
    except TimeoutError:
        print("Wi-Fi setup timed out. Unlock the iPhone and check the USB connection.", file=sys.stderr)
        return 1
    except Exception as error:
        # Never print a library traceback or the pairing record's private keys.
        print(f"Wi-Fi setup failed ({type(error).__name__}). Unlock the iPhone and validate trust.", file=sys.stderr)
        return 1
    print("iPhone Wi-Fi connections: " + ("enabled" if enabled else "disabled"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
