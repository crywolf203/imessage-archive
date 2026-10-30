#!/bin/sh
set -eu

umask 077
mkdir -p /run/imessage-wifi
chmod 700 /run/imessage-wifi
netmuxd --disable-usb --plist-storage /var/lib/lockdown \
    --socket-path /run/imessage-wifi/usbmuxd.sock &
pid=$!
trap 'kill -TERM "$pid" 2>/dev/null || true; wait "$pid" || true; exit 0' TERM INT
for attempt in $(seq 1 100); do
    kill -0 "$pid" 2>/dev/null || exit 1
    if [ -S /run/imessage-wifi/usbmuxd.sock ]; then
        chmod 600 /run/imessage-wifi/usbmuxd.sock
        break
    fi
    sleep 0.1
done
[ -S /run/imessage-wifi/usbmuxd.sock ] || { kill "$pid"; exit 1; }
wait "$pid"
