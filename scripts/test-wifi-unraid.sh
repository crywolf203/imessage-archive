#!/bin/bash
# Connection-only hardware test. No existing backups, exports or settings are mounted.
set -euo pipefail

APP_IMAGE=${1:?Usage: bash test-wifi-unraid.sh APP_IMAGE WIFI_IMAGE IPHONE_UDID}
WIFI_IMAGE=${2:?Supply the tested Wi-Fi image}
UDID=${3:?Supply the iPhone UDID}
[[ -f /etc/unraid-version ]] || { echo 'Run this on Unraid, not Windows.'; exit 1; }
[[ $UDID =~ ^[A-Za-z0-9-]{1,128}$ ]] || { echo 'Invalid iPhone UDID.'; exit 1; }

production=imessage-archive
test_app=imessage-wifi-test-app
test_wifi=imessage-wifi-test-discovery
for name in "$test_app" "$test_wifi"; do
    if docker inspect "$name" >/dev/null 2>&1; then
        echo "Test container already exists: $name. Inspect it before retrying."
        exit 1
    fi
done
pairing=$(docker inspect "$production" --format '{{range .Mounts}}{{if eq .Destination "/var/lib/lockdown"}}{{.Source}}{{end}}{{end}}')
[[ -n $pairing && -f $pairing/$UDID.plist ]] || { echo 'No saved trust record for this phone.'; exit 1; }
was_running=$(docker inspect "$production" --format '{{.State.Running}}')
docker pull "$APP_IMAGE"
docker pull "$WIFI_IMAGE"
echo 'This temporarily pauses imessage-archive. Finish or cancel any current job first.'
read -r -p 'Phone connected by USB and unlocked? Press Enter to start, or Ctrl+C to cancel. ' _

run_dir=$(mktemp -d /mnt/cache/appdata/imessage-archive/wifi-test.XXXXXX)
chmod 700 "$run_dir"
cleanup() {
    # Names were checked unused above; only containers labelled by this script are removed.
    for name in "$test_wifi" "$test_app"; do
        if [[ $(docker inspect "$name" --format '{{index .Config.Labels "imessage.wifi-test"}}' 2>/dev/null) == 1 ]]; then
            docker rm -f "$name" >/dev/null || true
        fi
    done
    [[ $was_running != true ]] || docker start "$production" >/dev/null || true
    rmdir "$run_dir" 2>/dev/null || echo "Empty test-directory cleanup deferred: $run_dir"
}
trap cleanup EXIT
[[ $was_running != true ]] || docker stop "$production" >/dev/null
docker run -d --name "$test_app" --label imessage.wifi-test=1 --privileged \
    -e APP_PASSWORD="$(openssl rand -hex 32)" -e SCHEDULE_ENABLED=0 \
    -e WIFI_USBMUXD_SOCKET=/run/imessage-wifi/usbmuxd.sock \
    --mount type=bind,src=/dev/bus/usb,dst=/dev/bus/usb \
    --mount "type=bind,src=$pairing,dst=/var/lib/lockdown,readonly" \
    --mount "type=bind,src=$run_dir,dst=/run/imessage-wifi,readonly" \
    --tmpfs /data/backups --tmpfs /data/exports --tmpfs /data/pdfs --tmpfs /data/config \
    "$APP_IMAGE" >/dev/null
usb_ready=false
for _ in $(seq 1 30); do
    if timeout 7 docker exec "$test_app" idevice_id -l | grep -Fxq "$UDID"; then
        usb_ready=true
        break
    fi
    sleep 1
done
[[ $usb_ready == true ]] || { echo 'The selected USB iPhone did not become available.'; exit 1; }
docker exec "$test_app" /opt/wifi/bin/python /app/wifi_setup.py enable --udid "$UDID"
docker run -d --name "$test_wifi" --label imessage.wifi-test=1 \
    --network host --read-only --cap-drop ALL --security-opt no-new-privileges:true \
    --memory 256m --cpus 1 \
    --mount "type=bind,src=$pairing,dst=/var/lib/lockdown,readonly" \
    --mount "type=bind,src=$run_dir,dst=/run/imessage-wifi" \
    "$WIFI_IMAGE" >/dev/null
read -r -p 'Unplug USB. Keep the phone unlocked on your home Wi-Fi, then press Enter. ' _
if ! usb_after=$(timeout 7 docker exec "$test_app" idevice_id -l); then
    echo 'USB status could not be checked; the wireless test cannot establish USB disconnection.'
    exit 1
fi
if grep -Fxq "$UDID" <<< "$usb_after"; then
    echo 'The phone is still visible by USB. Disconnect it before the wireless test.'
    exit 1
fi
echo 'Waiting up to about two minutes for Wi-Fi discovery...'
network_ready=false
for _ in $(seq 1 20); do
    if timeout 7 docker exec -e USBMUXD_SOCKET_ADDRESS=UNIX:/run/imessage-wifi/usbmuxd.sock \
        "$test_app" idevice_id -n | grep -Fxq "$UDID"; then
        network_ready=true
        break
    fi
    sleep 1
done
if [[ $network_ready != true ]]; then
    echo 'Wi-Fi discovery did not find this phone. No backup was attempted.'
    docker logs --tail 40 "$test_wifi"
    exit 1
fi
timeout 20 docker exec -e USBMUXD_SOCKET_ADDRESS=UNIX:/run/imessage-wifi/usbmuxd.sock \
    "$test_app" ideviceinfo -n -u "$UDID" -k ProductVersion
echo 'PASS: phone reached over Wi-Fi with USB disconnected. No backup started.'
echo 'Test containers will be removed and the original app restored.'
