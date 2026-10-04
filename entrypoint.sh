#!/bin/sh
set -eu

if [ "${START_USBMUXD:-1}" = "1" ]; then
    usbmuxd -f -U root &
fi

exec gunicorn \
    --bind 0.0.0.0:8080 \
    --workers 1 \
    --worker-class gthread \
    --threads 8 \
    --timeout 300 \
    --access-logfile - \
    --error-logfile - \
    app:app
