# Wi-Fi Setup and Testing

This is an opt-in, experimental integration. It does not require Windows, Apple Devices, iTunes, Messages in iCloud, Developer Mode, or enabling wireless debugging. USB remains the default. Synthetic CI tests are not proof of compatibility with every iOS version or home network; complete the connection-only iPhone test before using wireless backups.

## Two Separate Requirements

1. The iPhone must allow Wi-Fi connections for the existing trusted host. **Enable Wi-Fi** uses `pymobiledevice3` over USB to set `EnableWifiConnections` and read it back. It refuses to create a new pairing and leaves the saved trust record unchanged.
2. Unraid must discover the phone and provide a network connection. The optional `netmuxd` helper listens for Bonjour advertisements on the host network. It has no USB access and reads the same pairing directory read-only.

The main app keeps its bridge network, existing web port, storage mounts, and stock USB service. Wi-Fi commands use a separate Unix socket. The helper does not publish a TCP usbmux port. Merely checking a Wi-Fi backup option, putting both devices on the same LAN, or changing the main app to Host networking does not complete setup.

## Before Testing

- Finish or cancel any running backup/export/PDF job.
- Keep scheduling off in the app while changing the setup.
- Connect the iPhone to Unraid with a data-capable USB cable. Unlock it, trust Unraid and validate pairing.
- Use the same home LAN, not a guest network with client isolation. The server can be wired and the phone wireless.
- Allow local Bonjour/mDNS multicast (UDP 5353) and connections to the phone; do not open or forward ports on your internet router.
- Preserve completed backups before starting another backup. Normal app backups update `latest/<UDID>`; neither Wi-Fi action creates a historical snapshot.

USB is generally preferable for a first very large backup. Wi-Fi can be convenient for subsequent incremental backups, but does not make a full backup faster or restore originals missing from the phone.

## Connection-Only Hardware Test

The repository includes [test-wifi-unraid.sh](../scripts/test-wifi-unraid.sh). Run it as root in Unraid, not PowerShell. Supply the tested app and Wi-Fi image references reported by the candidate build, plus your iPhone UDID:

```sh
bash scripts/test-wifi-unraid.sh APP_CANDIDATE_IMAGE WIFI_CANDIDATE_IMAGE IPHONE_UDID
```

Replace all three uppercase placeholders. Candidate builds publish the Wi-Fi image with the same candidate tag plus `-wifi`. Do not use a guessed or nonexistent stable Wi-Fi tag. From an existing container, the USB identifier is available with:

```sh
docker exec imessage-archive idevice_id -l
```

The script checks the existing pairing mount, pulls both test images, asks you to finish any current job, pauses the original app, and starts disposable containers. It mounts **no existing backups, exports, PDFs or app configuration**. Their data paths use temporary memory-backed filesystems; schedules are off. The saved pairing directory is mounted read-only in both test containers.

While the iPhone is unlocked and USB-connected, it enables the Wi-Fi flag using the saved trust identity. It then asks you to unplug USB, waits for network-only discovery, and reads the phone's iOS version using `ideviceinfo -n`. It never invokes `idevicebackup2`. On exit it removes only its labelled test containers and restores the original app's previous running state. The phone's newly enabled Wi-Fi flag remains enabled.

Expected final result:

```text
PASS: phone reached over Wi-Fi with USB disconnected. No backup started.
```

If the script fails, retain the error and helper log for troubleshooting. It will not automatically fall back to USB or attempt a backup. A successful check establishes an authenticated network connection; an actual wireless backup still needs a separate test with a protected backup plan.

## Compose Installation

Use your existing Compose file and mappings. Do not replace them with default paths when upgrading. Download or check out the tested source revision containing `docker-compose.wifi.yml`.

In `.env`, set `WIFI_PAIRING_PATH` to the app's actual pairing host directory. Leave `START_USBMUXD=1`. Set a shared socket directory:

```text
WIFI_PAIRING_PATH=/mnt/cache/appdata/imessage-archive/lockdown
WIFI_RUN_PATH=/mnt/cache/appdata/imessage-archive/wifi-run
```

Prepare that directory:

```sh
mkdir -p /mnt/cache/appdata/imessage-archive/wifi-run
chmod 700 /mnt/cache/appdata/imessage-archive/wifi-run
```

For source builds:

```sh
docker compose -f docker-compose.yml -f docker-compose.wifi.yml config --quiet
docker compose -f docker-compose.yml -f docker-compose.wifi.yml up -d --build
```

For tested candidate images, set `IMAGE` and `WIFI_IMAGE` to the exact paired image references first, then use:

```sh
docker compose -f docker-compose.image.yml -f docker-compose.wifi.yml config --quiet
docker compose -f docker-compose.image.yml -f docker-compose.wifi.yml pull
docker compose -f docker-compose.image.yml -f docker-compose.wifi.yml up -d --no-build
```

The override sets the app's `WIFI_USBMUXD_SOCKET` and adds only the socket mapping. It does not move the main app to Host networking or modify backup paths. Recreate the app after changing environment or mount settings; restarting alone does not apply them. Use both Compose files consistently while wireless support is enabled.

## Template-Managed Unraid Installation

Do not start a second Compose-managed app with the same phone or archive. Use a tested candidate app image in the existing Unraid template, retaining every current data mapping and secret. Create the helper separately:

```sh
mkdir -p /mnt/cache/appdata/imessage-archive/wifi-run
chmod 700 /mnt/cache/appdata/imessage-archive/wifi-run

docker run -d --name imessage-archive-wifi --restart unless-stopped \
  --network host --read-only --cap-drop ALL --security-opt no-new-privileges:true \
  --memory 256m --cpus 1 \
  --mount type=bind,src=/mnt/cache/appdata/imessage-archive/lockdown,dst=/var/lib/lockdown,readonly \
  --mount type=bind,src=/mnt/cache/appdata/imessage-archive/wifi-run,dst=/run/imessage-wifi \
  WIFI_CANDIDATE_IMAGE
```

Replace the image placeholder and adjust the pairing source if your existing mount differs. If a helper of this name exists, inspect it first; this command does not replace it.

In **Docker > imessage-archive > Edit**, add:

| Type | Host Path / Key | Container Path / Value | Access |
| --- | --- | --- | --- |
| Path | `/mnt/cache/appdata/imessage-archive/wifi-run` | `/run/imessage-wifi` | Read Only |
| Variable | `WIFI_USBMUXD_SOCKET` | `/run/imessage-wifi/usbmuxd.sock` | Not a secret |

Apply the changes. Keep the main app's network, web port, USB mount, pairing directory and backup paths unchanged. The helper is optional; the main CA template does not silently install or start it.

## App Workflow

1. While USB-connected and unlocked, select the intended USB device if there is more than one, then use **Enable Wi-Fi**.
2. Wait for the job log to report `iPhone Wi-Fi connections: enabled`.
3. Disconnect USB and keep the phone unlocked on the home Wi-Fi network. Refresh the app after Bonjour discovery has had time to run.
4. Select the Wi-Fi device and use **Check Wi-Fi**. It queries device information, not backup contents.
5. After preserving the existing snapshot, select that device's **Wi-Fi** entry in the backup controls for a manual backup. Keep scheduling off until that backup succeeds.

Changing the Wi-Fi flag does not enable Messages in iCloud. The app cannot bypass the phone's passcode or iOS backup authorization. Newer iOS versions can require unlocking or entering the passcode even for a trusted wireless device; unattended locked-phone backups are not guaranteed.

## Troubleshooting

Run these commands in Unraid. The second command deliberately routes only this process to the wireless socket, leaving ordinary USB commands unchanged:

```sh
docker exec imessage-archive idevice_id -l
docker exec -e USBMUXD_SOCKET_ADDRESS=UNIX:/run/imessage-wifi/usbmuxd.sock imessage-archive idevice_id -n
docker logs --tail 50 imessage-archive-wifi
```

- **No USB device:** check the data cable, unlocked phone and exclusive ownership of the USB service.
- **Wi-Fi helper has no socket:** verify matching socket-directory mounts, root ownership, image startup and helper logs. It can coexist with the main app's stock USB daemon because it disables USB discovery.
- **USB works but no Wi-Fi device:** confirm flag enablement, matching trust directory, same LAN, mDNS availability and no guest/client isolation. Restart the helper after a new pairing if needed.
- **Private Wi-Fi address or iOS discovery changes:** Bonjour matching depends on the upstream backend's supported identity formats. Do not delete trust records, blindly edit their MAC addresses or disable security settings. Capture the iOS version and redacted helper log for investigation.
- **Listed but Check Wi-Fi fails:** unlock the phone, check local routing/firewall rules and validate USB trust again. A stale Bonjour entry is not proof that the phone is reachable.
- **Slow or interrupted backup:** Wi-Fi adds network bottlenecks and disconnect risks. Use USB for large transfers; never assume a completed discovery test means a backup finished.

The socket grants access to an already trusted phone. Keep its containing directory `700`, its socket `600`, and pairing records private. Do not publish trust records or debug logs containing certificate/key material.

## What Testing Establishes

`tests/wifi.py` checks transport separation, USB-only flag setup, exact device selection, absent/untrusted devices, readback failure, bounded connection checks, secret handling and no implicit backup. Authorization tests cover administrator-only actions and form-security tokens.

Candidate CI builds the pinned Linux helper and runs real Bonjour discovery, `idevice_id -n`/`-l` separation and an `ideviceinfo -n` query against a synthetic device. **That fixture disables heartbeat and TLS; production does not.** It cannot prove real iOS trust/TLS, heartbeat behavior, locked-phone access or backup completion. The separate connection-only hardware test exercises actual trust, production heartbeat and network access without exposing or rewriting an archive.

Upstream sources: [pymobiledevice3 wireless configuration](https://github.com/doronz88/pymobiledevice3/blob/v11.26.1/pymobiledevice3/cli/lockdown.py), [netmuxd](https://github.com/jkcoxson/netmuxd), and [libimobiledevice device selection](https://github.com/libimobiledevice/libimobiledevice/blob/1.3.0/tools/idevice_id.c).
