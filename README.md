# iMessage Archive for Unraid

iMessage Archive is a self-hosted iPhone backup, message browser, search index, and export appliance. It uses the open-source `libimobiledevice` tools to create local iPhone backups and ReagentX's `imessage-exporter` 4.3.0 to read and export Messages data.

`imessage-exporter` provides the message parsing plus HTML and text exports. This project adds the Docker image, USB and Wi-Fi backup workflow, browser interface, fast SQLite search, bounded conversation pages, CSV and ZIP downloads, resumable PDF rendering, scheduling, storage controls, and Unraid packaging. It is not affiliated with Apple, iMazing, or the `imessage-exporter` project.

## Important limits

- An iPhone must be unlocked and trusted before a backup can start.
- Messages are read from an iPhone backup, not directly from the phone.
- A first backup can take hours. Later backups are incremental unless **Force full backup** is selected.
- Encrypted local backups include more protected iPhone data. Keep the password: neither this app nor Apple can recover it.
- HTML, text, CSV, ZIP, and PDF outputs are not encrypted. Keep their Unraid shares private.

## Storage layout

| Data | Unraid path | Container path |
| --- | --- | --- |
| Backups | `/mnt/cache/appdata/imessage-archive/backups` | `/data/backups` |
| Pairing records | `/mnt/cache/appdata/imessage-archive/lockdown` | `/var/lib/lockdown` |
| Search index and settings | `/mnt/cache/appdata/imessage-archive/config` | `/data/config` |
| HTML, text, and ZIP exports | `/mnt/user/iphone-message-archive/exports` | `/data/exports` |
| PDFs | `/mnt/user/iphone-message-archive/pdfs` | `/data/pdfs` |

Backups and the search database stay on cache for responsive incremental updates and search. Large generated exports live on the array-backed user share.

## Install or upgrade on Unraid

Put `iphone-message-archive-v4.tar.gz` in `/mnt/cache/appdata/`, then run:

```sh
mkdir -p /mnt/cache/appdata/imessage-archive/build
tar -xzf /mnt/cache/appdata/iphone-message-archive-v4.tar.gz \
  -C /mnt/cache/appdata/imessage-archive/build
cd /mnt/cache/appdata/imessage-archive/build

cp -n .env.example .env
chmod 600 .env
nano .env

docker compose config
docker compose build
docker compose up -d --force-recreate
docker compose ps
docker compose logs --tail=100
```

For an existing install, the archive does not replace `.env`. Add a persistent session key once:

```sh
openssl rand -hex 32
```

Paste the result after `FLASK_SECRET_KEY=` in `.env`. Set a strong `APP_PASSWORD`. Recreating the container preserves every mounted backup, export, PDF, setting, index, and pairing record.

Open `http://YOUR-UNRAID-IP:8087`.

## First backup

1. Connect the unlocked iPhone by USB and accept **Trust This Computer**.
2. Select **Pair device**, then **Run preflight**.
3. Enter a new backup password and select **Enable encrypted backups** once.
4. Select **Start backup**. Leave **Force full backup** off for normal incremental updates.
5. Keep the phone unlocked if iOS reports `MBErrorDomain/208`.

When more than one iPhone is available, choose the USB or Wi-Fi device explicitly. Each device remains in its own UDID folder inside the shared backup destination.

Enabling encryption applies to future backups. It does not encrypt a backup that already exists, so create a fresh full backup after enabling it when protected data is required.

## Fast browsing and search

After an HTML export, the app builds an SQLite FTS5 search index and divides each large conversation into bounded viewer parts. The original HTML is left unchanged. Search and page loading therefore do not require the browser to parse one enormous conversation file.

**Refresh library** is incremental: unchanged conversations are skipped. The default part size is 300 messages and can be changed with `LIBRARY_CHUNK_MESSAGES`.

Available outputs are:

- Scrollable HTML with attachments
- Plain text
- Streamed CSV from the search index
- Portable ZIP containing HTML, referenced attachments, and the newest matching PDF
- PDF rendered by headless Chromium

## PDF performance

PDF jobs run inside the container, not in the browser on the viewing computer. They run in the background and expose progress, elapsed time, and an estimated time remaining.

For long conversations, keep **Reliable parts** selected. Each bounded viewer part becomes one PDF part and is merged at the end. Successful parts survive a cancellation or failure and are reused on retry.

- **Fast**: 1200-pixel JPEG images at quality 68
- **Balanced**: defaults to 1800 pixels at quality 80
- **Archive quality**: original images; slowest and highest memory use
- **Include images** off: fastest text-only PDF

More Chromium renderer processes rarely improve print-to-PDF performance and can increase memory pressure. The default is two. The Intel GPU device may be passed into the container, but Chromium PDF layout and printing are predominantly CPU-bound, so GPU passthrough is not expected to provide a meaningful improvement.

## Automation and retention

The **Automation** section stores settings in `/data/config/settings.json`:

- Scheduled paired-device backups every 6 hours, 12 hours, day, or week
- USB or Wi-Fi operation, an optional preferred iPhone, and optional forced full backups
- Generic JSON webhook notifications without message content
- Retention for old rotated exports and generated PDFs

Scheduled Wi-Fi backups require a previously paired iPhone that is visible to `idevice_id -n -l`. The scheduler waits when the phone is unavailable and never overlaps another job. A retention value of `0` disables deletion. Backups are never removed by retention; deleting a backup requires typing its folder name.

## Security

- The admin account can run jobs and change settings.
- An optional viewer account can browse and download but cannot submit changes.
- Session cookies are HTTP-only and SameSite strict. POST requests use CSRF protection.
- Set `COOKIE_SECURE=1` only after placing the app behind HTTPS.
- Do not expose port 8087 directly to the internet. Use a private LAN, VPN, or authenticated HTTPS reverse proxy.
- The support bundle excludes messages, passwords, webhook addresses, and pairing records.

See [SECURITY.md](SECURITY.md) for reporting and deployment guidance.

## HEIC and media

The image includes ImageMagick, HEIF support, and FFmpeg. **Browser-compatible images** converts HEIC images; **Convert images, audio, and video** performs the broadest conversion. **Original files** is fastest and preserves source media but browsers may not display every format.

## GitHub and Unraid template

The repository includes a GitHub Actions workflow that publishes an `amd64` image to `ghcr.io/crywolf203/imessage-archive`.

After pushing to GitHub, copy `unraid/imessage-archive.xml` to Unraid's user-template location or use the Compose file directly. The image workflow publishes on the main branch, version tags, and manual runs.

The workflow runs the bundled smoke tests before publishing. After the first successful workflow, open the package settings on GitHub and change the container package visibility to **Public** so an unauthenticated Unraid server can pull it.

Local test commands:

```sh
python -m pip install -r app/requirements.txt
python tests/smoke.py
python tests/auth_smoke.py
docker compose --env-file .env.example config
```

## Health and troubleshooting

`/healthz` checks the catalog and mounted directories. In the web app, use **Run preflight**, **Verify backup**, **Run diagnostics**, and **Create support bundle** in that order. Diagnostics are saved for the newest backup and summarized without loading the entire log into the page.

If a large job is running, the web interface remains responsive because command output is bounded and status is polled without reloading the conversation iframe.

## License

GPL-3.0-only. See [LICENSE](LICENSE).
