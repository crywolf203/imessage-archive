# iMessage Archive for Unraid

iMessage Archive is a self-hosted iPhone backup, message browser, search index, and export appliance. It uses the open-source `libimobiledevice` tools to create local iPhone backups and ReagentX's `imessage-exporter` to read and export Messages data. The pinned exporter version is defined in [Dockerfile](Dockerfile).

`imessage-exporter` provides the message parsing plus HTML and text exports. This project adds the Docker image, USB and Wi-Fi backup workflow, browser interface, fast SQLite search, bounded conversation pages, CSV and ZIP downloads, resumable PDF rendering, scheduling, storage controls, and Unraid packaging. It is not affiliated with Apple, iMazing, or the `imessage-exporter` project.

## Start here

For a normal installation, choose a username, set a unique administrator password, generate a session secret once, and check the storage paths. Leave performance settings at their defaults and scheduling/retention off until a manual backup and export work. No paid iMazing installation is needed.

- [Install or upgrade](#install-or-upgrade-on-unraid)
- [Every variable, its default, and how to use it](#configuration-reference)
- [Which password goes where](#passwords-and-the-session-secret)
- [First backup](#first-backup)
- [Export controls and downloads](#export-controls-and-downloads)
- [PDF performance](#pdf-performance)
- [Automation and retention](#automation-and-retention)
- [Common problems and their fixes](#health-and-troubleshooting)
- [Dependency updates and candidate testing](docs/updates.md)
- [Credits and acknowledgements](#credits-and-acknowledgements)

The following screenshot is the real app running through Compose with generated test messages, not a personal iPhone backup. [Compose proof and timings](docs/compose.md#automated-proof).

![Compose Web UI with synthetic messages](docs/screenshots/desktop-dark.png)

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

These are defaults, not requirements. Cache improves search responsiveness but must have room for the first full iPhone backup. Large exports can use an array-backed share. A `/mnt/user` path does not itself guarantee array-only storage: check the share's primary/secondary storage and mover settings in Unraid. Keep every share private; the search index also contains message text.

## Install or upgrade on Unraid

Choose either the Unraid template or Compose, not both for the same archive. Both use the same app and storage layout. Installation requires Linux amd64, Docker, enough free storage, and a data-capable USB cable. Compose also requires the `docker compose` command to be installed. Windows Docker Desktop does not provide this Linux/Unraid USB workflow directly.

### Unraid Apps installation

Search **Apps** for `imessage-archive`. The canonical template is in [crywolf203/unraid-templates](https://github.com/crywolf203/unraid-templates/blob/main/templates/imessage-archive.xml). A template being on GitHub does not guarantee the public CA listing is live. To install before catalog indexing, run this in the **Unraid terminal**, not PowerShell:

```sh
mkdir -p /boot/config/plugins/community.applications/private/crywolf203
curl --fail --location \
  https://raw.githubusercontent.com/crywolf203/unraid-templates/main/templates/imessage-archive.xml \
  --output /boot/config/plugins/community.applications/private/crywolf203/imessage-archive.xml
```

Open **Apps > Home > Private Apps**, select the app, and select **Install** only if no existing container already manages this archive. During setup:

1. Check the host paths in the storage table. Leave their container paths unchanged.
2. Choose the **Administrator** username; the template defaults to `admin`.
3. Set a unique **Administrator password** (`APP_PASSWORD`). There is no shared default password.
4. Generate and paste a **Session secret** (`FLASK_SECRET_KEY`) using the instructions below. This is not another login password.
5. Keep **Secure cookies** at `0` for an HTTP LAN connection. Use `1` only with HTTPS.
6. Apply the template and open `http://YOUR-UNRAID-IP:8087`.

The template uses **privileged mode** and binds `/dev/bus/usb` for USB hotplug. This gives the container broad access to the server. Use a trusted private server; do not expose the Web UI to the internet. No GPU mapping is required.

### Fresh Compose installation

Use this only for a fresh installation. Do not overwrite an existing Compose file or `.env`; follow the upgrade section instead. Run in the Unraid terminal:

```sh
mkdir -p /mnt/cache/appdata/imessage-archive/build
cd /mnt/cache/appdata/imessage-archive/build
curl --fail --location --output docker-compose.image.yml \
  https://raw.githubusercontent.com/crywolf203/imessage-archive/main/docker-compose.image.yml
curl --fail --location --output .env.example \
  https://raw.githubusercontent.com/crywolf203/imessage-archive/main/.env.example
cp -n .env.example .env
chmod 600 .env
openssl rand -hex 32
nano .env
```

Paste the generated value after `FLASK_SECRET_KEY=`. Replace `APP_PASSWORD` with your own long password; the sample `replace-with-...` values are placeholders, not secure credentials. `APP_USER` defaults to `aaron` in Compose; change it to your preferred username. Review the host paths in `docker-compose.image.yml` before starting.

```sh
docker compose -f docker-compose.image.yml config --quiet
docker compose -f docker-compose.image.yml pull
docker compose -f docker-compose.image.yml up -d --wait
docker compose -f docker-compose.image.yml ps
curl --fail http://127.0.0.1:8087/healthz
```

Open `http://YOUR-UNRAID-IP:8087` and sign in with `APP_USER` and `APP_PASSWORD`. Use `docker-compose.image.yml` consistently for normal installation and updates; `docker-compose.yml` is the source-build option. More detail and tested screenshots are in the [Compose guide](docs/compose.md).

### Safe upgrades and applying changed variables

Before changing anything, let the current job finish or cancel it, and record the actual mounts:

```sh
docker inspect imessage-archive --format '{{range .Mounts}}{{println .Destination "->" .Source}}{{end}}'
```

Keep the same host paths, login settings, session secret, and pairing directory. Older installs may have exports and PDFs under `/mnt/cache/appdata/imessage-archive/`. Changing a mapping does not move files: an empty new path can make an existing archive appear missing.

For a template-managed container, use **Docker > Edit**, change the relevant fields, and **Apply**. Use the Docker tab's update action to download a new image. Existing private templates may need to be downloaded again to get new template descriptions; do not install a second container to update the first.

For an image-based Compose install, edit `.env` for variables or `docker-compose.image.yml` for host paths, then run:

```sh
cd /mnt/cache/appdata/imessage-archive/build
nano .env
docker compose -f docker-compose.image.yml config --quiet
docker compose -f docker-compose.image.yml pull
docker compose -f docker-compose.image.yml up -d --force-recreate --wait
docker compose -f docker-compose.image.yml logs --tail=100
```

Environment changes need a container recreation; `docker restart` alone does not apply edited Compose variables. These commands do not delete mounted data. Do not use `down --volumes` for an upgrade. Do not switch a running Compose install to template management without stopping the old instance and checking every mapping first. Full Compose configuration output can reveal passwords; use `config --quiet` for routine validation.

## Configuration reference

All supported application variables are listed below. In Unraid, their names appear as **Container Variable: NAME** under **Docker > Edit**; use advanced view for hidden fields. For a documented variable not already listed, add a **Variable** with its **Key** set to the exact name. `IMAGE` and `WEB_PORT` are Compose settings; their Unraid equivalents are **Repository** and the host-side **Web UI** port, not new variables.

In Compose, set existing variables in `.env` using `NAME=value`, one per line. `.env` is only a substitution file: a new name is not forwarded to the container unless it is also added under the service's `environment:` section. The advanced directory overrides below are not forwarded by the supplied Compose files. Keep `.env` private, with mode `600`, and never commit it.

For a Compose password containing spaces, `$`, or `#`, use single quotes around the value in `.env` to keep it literal; for example, `APP_PASSWORD='your-own-unique-password'`, replacing the example with your actual password. Do not include those wrapper quotes in an Unraid password field. Hexadecimal values from `openssl rand -hex 32` avoid these quoting issues. Shell variables with the same names can override `.env`; see [Docker's interpolation rules](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/).

Use `0` for off and `1` for on. Do not use `true`, `yes`, units such as `900s`, or decimal numbers in integer fields. Numeric settings are bounded as documented; nonnumeric values can prevent startup. Most users should leave advanced performance values alone.

### Passwords and the session secret

These are three separate things, with different purposes:

| Item | What it is for | Where to enter it | Keep it? |
| --- | --- | --- | --- |
| Administrator password | Signing into this web app | `APP_PASSWORD` in the template or `.env`; use it on the login page | Yes, in a password manager |
| Session secret | Signing browser login sessions so they cannot be forged; also protects the session holding form-security tokens | `FLASK_SECRET_KEY` in the template or `.env`; never on the login page | Yes, reuse across upgrades |
| iPhone backup encryption password | Encrypting local phone backups and allowing diagnostics/export to read an encrypted backup | **Backup encryption password** when enabling encryption; **Backup password** when reading that backup | Yes; losing it can make the encrypted backup unreadable |

Generate the session secret in the **Unraid terminal**:

```sh
openssl rand -hex 32
```

This prints a random 64-character value. Paste the entire value into the Unraid **Session secret** field, or after `FLASK_SECRET_KEY=` in `.env`, without spaces. Generate it once; you do not need to memorize it, type it when logging in, or regenerate it on every update. You can run the command a second time to generate a separate random administrator password; do not reuse the secret as the login password.

The app can generate a temporary secret when this variable is empty, but it changes on every application restart, invalidating login sessions and open forms. The Unraid template intentionally requires a persistent value. Changing the key signs existing sessions out; it does not erase messages or backups. Never publish it in screenshots, logs, issues, or a public `.env`.

### Login and browser security

| Variable | Default | What it does | How to use it |
| --- | --- | --- | --- |
| `APP_USER` | `admin` in Unraid; `aaron` in Compose/app | Administrator username, with permission to run jobs and change settings | Choose a username and use the same value on the login page. Keep it different from the viewer username. |
| `APP_PASSWORD` | No secure default; `.env.example` contains a placeholder | Administrator login password | Required for a safe installation. Use a long unique password or a separately generated random value. Do not leave it blank or use `change-me-before-lan-use`: those values disable authentication in the app. Replace all sample placeholders. |
| `FLASK_SECRET_KEY` | Empty in Unraid; placeholder in `.env.example`; app generates a temporary key if empty | Private session-signing key, not a login or backup password | Generate with `openssl rand -hex 32`, paste once, and retain across upgrades. See the explanation above. |
| `VIEWER_USER` | `viewer` | Username of the optional read-only account | Leave at `viewer` unless you need a different name. The account is disabled until a viewer password is configured. |
| `VIEWER_PASSWORD` | Empty | Enables the read-only account when nonempty | Leave blank for a single-user installation. Set a separate password to let someone browse and download existing exports, but not run backups, generate PDFs/ZIPs, or change settings. This still gives access to private messages. |
| `COOKIE_SECURE` | `0` | Restricts login-session cookies to HTTPS when set to `1` | Keep `0` for `http://SERVER:8087`. Set `1` only when you use a working HTTPS connection, such as a trusted reverse proxy. It does not enable HTTPS or encrypt HTTP traffic; setting it to `1` on HTTP can cause a login loop. |

Changing a username/password does not revoke already signed-in sessions immediately. If access must be revoked, also generate a new `FLASK_SECRET_KEY`, apply the changes, and sign in again. Do not share any of these secrets. An HTTPS connection is recommended when transmitting login or backup passwords; plain HTTP is not encrypted even on a LAN.

### Image, port, time zone, USB, and logs

| Variable | Default | What it does | How to use it |
| --- | --- | --- | --- |
| `IMAGE` | `ghcr.io/crywolf203/imessage-archive:latest` | Compose-only Docker image selection | `latest` follows manually approved, tested stable promotions after a pull/update, not every main-branch build. Set `IMAGE=ghcr.io/crywolf203/imessage-archive:v4.0.1` to pin a released version. In Unraid edit **Repository** instead. Changing a tag does not move data. |
| `WEB_PORT` | `8087` | Compose-only host port mapped to container port `8080` | Change to an unused host port, for example `8088`, if another app uses 8087. Then open `http://SERVER:8088`. In Unraid change the host-side Web UI port. Keep the container port at 8080; internal PDF rendering uses it. |
| `TZ` | `America/New_York` | Container time zone for locally formatted dates, job logs, and filenames | Use an IANA zone such as `America/Chicago`, `Europe/London`, or `Etc/UTC`, not a city nickname. Scheduling uses elapsed intervals, not a fixed wall-clock appointment. |
| `START_USBMUXD` | `1` | Starts the USB multiplexer that libimobiledevice uses to communicate with the phone | Keep `1` for the supplied USB installation. Set `0` only if you have deliberately provided a working external usbmuxd/socket configuration, or an export-only setup with no live phone. This switch alone does not connect to a host service or enable Wi-Fi. Do not run competing USB backup services. |
| `LOG_LINES` | `300` | Maximum recent job-log lines retained/displayed | Whole number, minimum `50`. Keep `300` for responsive status updates. Increase moderately for troubleshooting, not to thousands of attachment lines. Older lines are discarded from the in-memory job log; this is not a complete persistent log archive. |

### PDF and conversation performance

| Variable | Default | What it does | How to use it |
| --- | --- | --- | --- |
| `LIBRARY_CHUNK_MESSAGES` | `300` | Approximate messages/announcements per indexed viewer and reliable PDF part | Effective range `50`-`1000`. Smaller parts reduce per-page/per-render work but produce more parts. Try `150` on a constrained server. Applies when conversations are newly indexed/reindexed; restarting or **Refresh library** alone skips unchanged HTML. Run a new HTML export to regenerate existing parts at the new size. |
| `PDF_TIMEOUT_SECONDS` | `900` | Maximum time for each Chromium PDF rendering process | Seconds, minimum `60`; default is 15 minutes per part, not 15 minutes for the whole job. Increase to `1800` only when a legitimate large part hits the render timeout. It does not speed rendering and does not cover image preparation or final merging. |
| `PDF_IMAGE_MAX_EDGE` | `1800` | Maximum longest edge of optimized images in the **Balanced** PDF profile | Pixels, minimum `800`. Try `1200` for smaller PDF images. Smaller images are not enlarged. **Fast** uses its own fixed 1200-pixel value; **Archive quality** skips this optimization. Original export attachments are not changed. |
| `PDF_IMAGE_QUALITY` | `80` | JPEG quality of optimized **Balanced** PDF images | Effective range `55`-`95`. Lower values reduce image bytes and may reduce render work; try `68` for compact output. **Fast** uses its own fixed quality of 68; **Archive quality** skips optimization. Not an export-quality setting. |
| `PDF_IMAGE_WORKERS` | `2` | Concurrent image-conversion processes during PDF preparation | Effective range `1`-`4`. Use `1` if RAM is tight. Try `3` or `4` only with spare RAM/CPU and many large images. This is not a Chromium thread count; the PDF parts are still rendered sequentially. More workers can increase memory pressure rather than improve speed. |
| `PDF_OPTIMIZE_MIN_BYTES` | `524288` | Size threshold for optimizing browser-native JPEG/PNG/WebP images | Bytes, minimum `0`; default is 512 KiB. `0` makes all such images eligible; `1048576` skips native images smaller than 1 MiB. Other image formats are eligible regardless of size. Applies to **Fast/Balanced** PDFs with images, not text-only or archive-quality output. |
| `PDF_NICE_LEVEL` | `5` | Lowers CPU scheduling priority of the Chromium renderer | Effective range `0`-`19`. `0` uses normal priority; larger values favor other server work under load and may make PDFs slower. This is not a CPU limit or thread count, and does not change image-conversion priority. Keep `5` for a shared server. |
| `PDF_RENDERER_PROCESSES` | `2` | Chromium renderer-process limit | Effective range `1`-`8`. Keep `2`; more processes rarely help a single print job and can increase RAM use. This does not specify CPU threads, parallel PDF parts, or the total number of Chromium subprocesses. |

There is one background job at a time. The web service uses one worker with eight request threads so status and browsing can remain available; those threads are not eight PDF renderers. PDF processing happens on the server. GPU passthrough is not needed and the supplied render command disables GPU use.

### Scheduling, notifications, and retention

These variables are initial defaults. Once **Save automation** stores a value in `/data/config/settings.json`, that saved value takes precedence over the matching environment variable, including a saved `false`, `0`, or empty webhook. To change an existing installation's automation, use **Automation and support** in the web app and save there; editing `.env` alone may not change it. Do not delete the configuration folder to reset a setting.

| Variable | Default | What it does | How to use it |
| --- | --- | --- | --- |
| `SCHEDULE_ENABLED` | `0` | Enables scheduled iPhone backups | Leave off until a manual backup works. `1` starts periodic device checks; it does not schedule message exports or PDFs. Saving enabled automation requests a device check within about one minute, so a backup may start soon, not only after a full interval. |
| `SCHEDULE_INTERVAL_HOURS` | `24` | Wait after a successful scheduled backup before the next scheduled attempt | Effective range `1`-`720` hours. `24` is daily; `168` is weekly. The UI offers 6, 12, 24, and 168 hours; custom environment intervals can be used before UI settings are saved. It is not a start-at-midnight setting. |
| `SCHEDULE_NETWORK` | `1` | Uses network discovery/connection for scheduled backups | `1` requires an already paired phone visible to the network tools. Use `0` for a plugged-in USB phone. This does not turn on iOS Wi-Fi syncing, pair a new phone, or affect the connection explicitly selected for a manual backup. |
| `SCHEDULE_FULL` | `0` | Forces full rather than normal incremental scheduled backups | Keep `0` for recurring backups. Use `1` only when a full refresh is intentional; it can require hours, more I/O, and more storage activity. It does not create a separate dated snapshot for each run. |
| `NOTIFY_WEBHOOK_URL` | Empty | Endpoint receiving a generic JSON job-completion notification | Optional complete HTTP/HTTPS URL for a receiver accepting this app's JSON format. Prefer HTTPS. Blank disables notifications unless a saved UI URL overrides it. Slack/Discord or another service may require an adapter; an arbitrary webhook is not guaranteed to accept the payload below. URLs can contain secrets: keep them private. |
| `NOTIFY_ON_SUCCESS` | `1` | Includes successful jobs in webhook notifications | `1` sends successes as well as other completed statuses; `0` suppresses successes, not errors/cancellations. Has no effect without a configured webhook. |
| `EXPORT_RETENTION_DAYS` | `0` | Age limit for old rotated HTML/text export directories when cleanup is requested | Effective range `0`-`3650` days. `0` keeps everything. `30` makes old `archive-*` and `text-archive-*` directories eligible after 30 days, based on modification time. Does not remove current exports, portable ZIPs, or iPhone backups. Select **Apply retention cleanup** to actually delete eligible items. |
| `PDF_RETENTION_DAYS` | `0` | Age limit for finished top-level PDF files when cleanup is requested | Effective range `0`-`3650` days. `0` keeps everything; for example, `90` makes PDFs older than 90 days eligible. Select **Apply retention cleanup** to delete them. Does not remove backups, timing history, or preserved `.resume` parts. Deletion is permanent. |

The optional **Preferred iPhone** is a web-app setting, not an environment variable. Choose a specific phone when multiple devices are paired; leaving it blank selects the first available device found by the tools. A scheduler waiting for a missing phone checks again after about ten minutes; a failed scheduled backup is retried after about an hour. A busy app does not run another backup concurrently.

The webhook receives JSON like this, without message content or backup passwords:

```json
{"application":"iMessage Archive","job":"Scheduled iPhone backup","status":"success","elapsed_seconds":120}
```

To replace a saved webhook, enter a new URL and select **Save automation**. An empty input preserves the saved address; select **Remove saved webhook** and save to disable it.

### Advanced container-directory overrides

These are paths **inside the container**, not Unraid host paths. Normal installations should keep them unchanged and change only the host side of each bind mount in the storage table. Setting a host path such as `/mnt/cache/...` here without mounting it there can write data into the container's disposable filesystem.

| Variable | Default | What it does | How to use it |
| --- | --- | --- | --- |
| `BACKUPS_DIR` | `/data/backups` | Root of discovered/created iPhone backups; new backups go under `latest/DEVICE-ID` | Keep default. For a custom deployment, use an absolute container path with a matching persistent mount. Not forwarded by supplied Compose files unless you add it explicitly. |
| `EXPORTS_DIR` | `/data/exports` | Root for current/rotated HTML and text exports, viewer parts, and portable ZIPs | Keep default. A custom override needs a matching persistent mount and may require a new export/index rebuild. Do not change it to solve a full host disk. |
| `PDFS_DIR` | `/data/pdfs` | Root for finished PDFs, resume parts, and rendering timing history | Keep default and map it to a private host folder. A custom override needs a matching persistent mount. |
| `CONFIG_DIR` | `/data/config` | Root for the search database, job/backup history, saved automation, and scheduler state | Keep default and preserve this mount across upgrades. A new empty path hides the old index/settings; it does not migrate them. |

The **Pairing Records** mount at `/var/lib/lockdown` contains sensitive device trust records. The **USB Bus** mount at `/dev/bus/usb` exposes USB devices; it is not a data-storage folder. Neither is a password field or an app directory variable. Keep both container paths unchanged for the standard setup.

### Build-time and internally managed variables

These are included for completeness, not required installation settings. Build arguments cannot be changed by editing a running container's environment.

| Variable | Default | What it does | How to use it |
| --- | --- | --- | --- |
| `IMESSAGE_EXPORTER_VERSION` | Pinned in [Dockerfile](Dockerfile) | Docker build argument selecting the Rust exporter version | For source builds only, pass `--build-arg IMESSAGE_EXPORTER_VERSION=...` to `docker build` and verify CLI compatibility. The published image already includes its exporter; a Container Variable does not replace it. Renovate proposes changes to this single default. |
| `APP_VERSION` | `4.0.1` | Docker build argument for the image's version label | Release-maintainer metadata, not a feature switch or update command. Do not set it as a Container Variable to upgrade the app; pull the desired image tag instead. |
| `PYTHONDONTWRITEBYTECODE` | `1` in the image | Prevents Python from creating bytecode-cache files | Internal image default; no normal user action needed. |
| `PYTHONUNBUFFERED` | `1` in the image | Sends Python output to container logs without normal buffering | Internal image default; leave unchanged. |
| `BACKUP_PASSWORD` | Not configured globally | Temporary child-process environment used for enabling iPhone backup encryption | Managed by the app for that requested operation only. Enter the encryption password in the web form, not in `.env` or the Unraid template. Setting this globally is not the supported way to unlock exports. |

For a source build, `docker build -t imessage-archive-local .` uses the pinned exporter default; it does not start the app or supply mounts/login settings. The normal published-image workflow avoids this build entirely.

### Separate staging settings

These apply only to `docker-compose.staging.yml` and `.env.staging`. They are not production Container Variables. See the [candidate testing guide](docs/updates.md#try-a-candidate-on-unraid); never use staging with production data mounts.

| Variable | Default | What it does | How to use it |
| --- | --- | --- | --- |
| `STAGING_IMAGE` | Required; sample tag is a placeholder | Selects the candidate image for the separate test container | Paste the immutable `ghcr.io/...@sha256:...` reference from a successful candidate's metadata. |
| `STAGING_PORT` | `8088` | Staging's host web port | Choose an unused port, leaving production's port unchanged. Open that port on your private LAN. |
| `STAGING_PASSWORD` | Required; sample is a placeholder | Login password for the staging-only `staging-admin` account | Set a unique password in `.env.staging`; do not reuse production credentials. |
| `STAGING_SECRET_KEY` | Required; sample is a placeholder | Signs staging-only login sessions | Generate with `openssl rand -hex 32` and retain in `.env.staging`, separate from the production secret. |

## First backup

1. Connect the unlocked iPhone by USB and accept **Trust This Computer**.
2. Select **Pair device**, then **Run preflight**.
3. Enter a new backup password and select **Enable encrypted backups** once.
4. Select **Start backup**. Leave **Force full backup** off for normal incremental updates.
5. Keep the phone unlocked if iOS reports `MBErrorDomain/208`.

When more than one iPhone is available, choose the USB or Wi-Fi device explicitly. Each device remains in its own UDID folder inside the shared backup destination.

Enabling encryption applies to future backups. It does not encrypt a backup that already exists, so create a fresh full backup after enabling it when protected data is required.

If encrypted backups are already enabled on the phone, keep that password and do not enable encryption again just to export. Use its existing password when reading the backup. The login password/session secret cannot decrypt it. After backup success, select the backup in **Export**, run **Verify backup**, and run **Run diagnostics** with its backup password if encrypted. Verification checks core manifests and the unencrypted manifest database's structure; it does not prove every attachment is present or fully verify encrypted contents.

## Export controls and downloads

Start with one conversation and a short date range to check the result before exporting years of photos. Exporting requires disk space for copied/converted media as well as the original backup. An export's size estimate is a warning, not a reason to ignore a nearly full disk.

| Web control | How to use it |
| --- | --- |
| **Backup** | Select a completed backup belonging to the intended iPhone. The phone does not need to remain connected to export an existing backup. |
| **Backup password** | Enter that backup's encryption password for diagnostics/export; leave blank only for an unencrypted backup. It is not saved by the app as an automation credential. |
| **Phone number, email, or chat filter** | Optional exporter filter. Blank exports all matching data without a contact filter. Use the number/email as stored in Messages, including its country code when appropriate. A matching participant can also include group chats; this is not a guaranteed one-to-one-only filter. |
| **Export formats** | **Browsable HTML** populates the conversation viewer/search/PDF workflow. **Text only** creates downloadable plain text but not a new browser library. **HTML and text** does both; the second text export skips attachment copies to avoid duplicating media again. |
| **Attachment handling** | **Original files** copies source media with the least conversion; HEIC may not display in every browser. **Browser-compatible images** converts supported images for viewing. **Convert images, audio, and video** does broader conversion and takes longer. **No attachment copies** reduces output storage but copied media will not be available in the export/PDF. None of these options retrieves media absent from the backup. |
| **Start date / End date** | Optional bounds passed to the exporter. Leave blank for no bound. Use a small range for the first test and check the result before making a full-history export. |
| **PDF-ready images** | Writes HTML without the exporter's lazy-image-loading markup. It does not itself create a PDF or convert HEIC. The app's viewer still loads images lazily for responsiveness. |
| **Refresh library** | Indexes current HTML and creates bounded viewer parts; unchanged files are skipped. Use after adding an HTML export or recovering a missing index. It does not copy missing attachments or re-read the phone. |
| **Search messages** | Searches the current indexed export's text and senders. Multiple words are combined as matching search terms; this is not a search of the entire phone or old rotated exports. Select a result to open its conversation part. |
| **Download CSV** | Downloads the selected indexed conversation's message rows. A CSV is not an attachment archive. |
| **Create ZIP archive** | Packages the selected original HTML, available referenced files, and newest matching PDF, if one exists. Wait for job success, then download it under **Portable archives**. Missing media cannot be added, and ZIP output is not encrypted. |
| **Render PDF** | Select a conversation, choose image options, then render on the server. Wait for job success and download the result under **PDFs**; HTML export comes first. Audio/video cannot play inside the PDF. |

A new HTML export rotates the previous `current` directory into an `archive-*` directory and replaces the current searchable library with the new export. It does not merge successive filtered exports into one library. Old output remains on disk until you intentionally clean it up; finish PDFs/ZIP downloads for the current export before replacing it.

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

**Reliable parts** normally renders the whole selected conversation as multiple bounded parts, not only the part currently visible on screen. Keep the same export, image profile, and include-images/reliable-parts choices when retrying to reuse completed parts. A new HTML export or changed options can require a new render. The ETA is approximate and based on completed work and recent render timings, not a guaranteed finish time. Image-heavy first runs may take longer than the estimate.

More Chromium renderer processes rarely improve print-to-PDF performance and can increase memory pressure. The default is two. The Intel GPU device may be passed into the container, but Chromium PDF layout and printing are predominantly CPU-bound, so GPU passthrough is not expected to provide a meaningful improvement.

## Automation and retention

The **Automation** section stores settings in `/data/config/settings.json`:

- Scheduled paired-device backups every 6 hours, 12 hours, day, or week
- USB or Wi-Fi operation, an optional preferred iPhone, and optional forced full backups
- Generic JSON webhook notifications without message content
- Retention for old rotated exports and generated PDFs

Scheduled Wi-Fi backups require a previously paired iPhone that is visible to `idevice_id -n -l`. The scheduler waits when the phone is unavailable and never overlaps another job. A retention value of `0` disables deletion. Backups are never removed by retention; deleting a backup requires typing its folder name.

In this version, retention is applied when you select **Apply retention cleanup**, not automatically after every job. Positive retention values make old files eligible; saving them alone does not delete files. Review the settings and retain another copy before cleanup. The scheduled backup destination is updated incrementally per device; it is not a series of dated immutable snapshots. To keep an older backup, preserve a separate copy outside that live destination.

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

The repository includes GitHub Actions workflows that build and regression-test `amd64` candidate images in `ghcr.io/crywolf203/imessage-archive`. Stable `latest` updates require manual promotion of a successful main-branch candidate.

The canonical template and its setup guide live in [crywolf203/unraid-templates](https://github.com/crywolf203/unraid-templates/blob/main/docs/imessage-archive.md); follow the private Apps installation above when needed. Adding a template to an already enabled repository is not the same as immediate publication of a new app in CA. The submission portal can reject a duplicate repository as already enabled; do not create a duplicate just to force indexing.

Renovate proposes dependency PRs with automatic merging disabled. **Build candidate** runs on pushes/PRs/manual runs and performs weekly uncached Debian-package refreshes. Candidates run the bundled smoke tests and real Compose/browser regressions before publication. After the first successful publish, the container package must be **Public** for unauthenticated Unraid pulls.

The shared candidate checks exercise the real exporter against a synthetic unencrypted iOS backup, real Chromium PDFs with and without images, HEIC conversion, 10,450 indexed sample messages, CSV, portable ZIP exports, and browser login/navigation. `compose-runtime-proof` records versions, timings, performance ceilings and desktop/mobile screenshots. **Verify published image** can rerun these checks on a selected image. No CI job connects to an iPhone. See the [update/testing guide](docs/updates.md) for setup, staging, promotion and device-testing limits, and the [Compose guide](docs/compose.md) for deployment proof.

Local test commands:

```sh
python -m pip install -r app/requirements.txt
python tests/smoke.py
python tests/auth_smoke.py
python tests/docs.py
python tests/pipeline.py
docker compose --env-file .env.example config --quiet
```

## Health and troubleshooting

`/healthz` checks the catalog and mounted directories. In the web app, use **Run preflight**, **Verify backup**, **Run diagnostics**, and **Create support bundle** in that order. Diagnostics are saved for the newest backup and summarized without loading the entire log into the page.

If a large job is running, the web interface remains responsive because command output is bounded and status is polled without reloading the conversation iframe.

### Common questions

| Symptom or question | What to check or do |
| --- | --- |
| **What are the login credentials?** | The values you set in `APP_USER` and `APP_PASSWORD`. Unraid's template defaults the username to `admin`; Compose defaults it to `aaron`. There is no safe built-in password. Your Unraid server login and iPhone passcode are separate. |
| **Do I really need the session secret?** | The app needs a key to sign login sessions. It can generate a temporary one, but the Unraid template requires a fixed one to avoid restart-related logouts. Generate once with `openssl rand -hex 32`, paste it into `FLASK_SECRET_KEY`, and retain it. It is not a password you enter on login. |
| **The login page keeps coming back.** | Check the username/password and whether `COOKIE_SECURE=1` is being used over plain HTTP. Use HTTPS when secure cookies are enabled. After changing secrets/restarting, reload the page and sign in again. |
| **The form expired / HTTP 403.** | Reload the page to obtain a new form token, then retry. This can happen after restarting or changing the session secret. A viewer account cannot run jobs; use the administrator account for those actions. |
| **The phone charges, but no device appears.** | Unlock it, use a known data-capable cable, accept Trust/passcode prompts, check `/dev/bus/usb` is mounted and `START_USBMUXD=1`, and ensure no competing usbmuxd owns the phone. Use the commands below to distinguish USB detection from pairing. |
| **Pairing says a passcode is set / Device locked (208).** | Enter the passcode on the iPhone, leave it unlocked, and retry pairing or backup. Charging alone is not evidence of a working data connection. |
| **Wi-Fi backups never start.** | Complete USB trust/pairing and a manual USB backup first. Check that `idevice_id -n -l` sees the device and that the preferred phone is available. Same-LAN membership or `SCHEDULE_NETWORK=1` alone does not establish Wi-Fi backup support. Use USB when network discovery is unavailable. |
| **No backup with a Manifest file found.** | Confirm the backup job actually completed and that the Backups host mapping contains its device folder under `latest`. An empty replacement mount or failed/locked backup is not a usable archive. Do not delete a valid older backup to fix discovery. |
| **Not enough free disk space / estimated export larger than free space.** | Check both backup and export destinations in **Storage**. Reduce the contact/date range or disable attachment copies for a text-focused export, or choose a larger private share. `/mnt/user` may still use cache according to share settings. Changing a mount does not migrate existing files; verify a separate migration before recreating. Do not blindly bypass the warning. |
| **Attachment not found at specified path.** | Verify the selected backup and run diagnostics. Check that mounts still point to the original files and that the backup completed. Some media may be absent from a local backup; conversion cannot recover it. Messages can still export without those attachments. |
| **No HEIC converter found / images do not display.** | Use the current published image, which includes ImageMagick/HEIF support. Select **Browser-compatible images** for a new export. An old local build may be missing tools; simply restarting it does not update its image. Original HEIC files may not render in the viewing browser. |
| **There is no PDF button.** | Run a browsable HTML export, wait for indexing, and select a conversation. A text-only export does not populate the browser/PDF workflow. A viewer account cannot submit PDF jobs. |
| **PDF rendering is slow or uses too much RAM.** | Keep **Reliable parts** on. First try text-only; then **Fast** images. Keep image workers at `1`-`2` and renderer processes at `2`; reduce conversation part size for a new export. Large images and final PDF merging still need RAM. More processes or GPU passthrough is not a universal speed fix. |
| **A PDF job timed out.** | Retry with unchanged options to reuse completed parts, use text-only/Fast images, or increase the per-render timeout if appropriate. The limit does not include preparation/merging. Successful parts may survive a failed/cancelled multi-part render; a new export changes the resume input. |
| **Search has no results / a conversation says Refresh the library.** | Search covers only the current HTML export. Run **Refresh library** and check its job succeeds. Text-only and old rotated exports are not added to the current index automatically. |
| **I changed a variable but nothing happened.** | Apply/recreate the container; restart alone does not load changed Compose settings. For scheduling, webhook, and retention, saved UI settings override environment defaults: change them in **Automation and support** and save. |
| **The archive disappeared after an upgrade.** | Compare the old/new mount mappings. Do not initialize another backup or delete anything until the original folders are located. Changing a path does not transfer data; preserve the existing `/data/config` and pairing mounts too. |
| **Compose says no configuration file provided.** | Go to `/mnt/cache/appdata/imessage-archive/build` and use `-f docker-compose.image.yml`. Commands run from `root@Tower:~#` do not automatically find the project's file. |
| **The web port is already allocated.** | Use an unused host port in `WEB_PORT` or the Unraid Web UI field, apply/recreate, and open the new URL. Do not change container port 8080. |
| **CA says Added May 30, 2015 / Last Update Unknown.** | Private templates may lack catalog dates. CA displays a fallback date; it is not the image's release date or your installation date. No reinstall is needed just to fix that label. [CA display logic](https://github.com/unraid/community.applications/blob/master/source/community.applications/usr/local/emhttp/plugins/community.applications/skins/Narrow/skin.php). |
| **Cleanup did not free space.** | Both retention values default to zero. Save a positive value and run **Apply retention cleanup** only after reviewing what it makes eligible. Current exports, ZIPs, backup folders, and incomplete PDF resume parts are outside those rules. |

Useful **Unraid terminal** checks (read-only; do not post passwords or personal job logs publicly):

```sh
docker ps --filter name=imessage-archive
docker inspect imessage-archive --format '{{range .Mounts}}{{println .Destination "->" .Source}}{{end}}'
docker logs --tail=100 imessage-archive
lsusb
docker exec imessage-archive idevice_id -l
docker exec imessage-archive idevice_id -n -l
docker exec imessage-archive idevicepair validate
curl --fail http://127.0.0.1:8087/healthz
```

Substitute your chosen web port in the health-check URL. For multiple phones, select the device in the UI rather than assuming the first one is correct. If the above checks do not resolve a problem, generate a **Create support bundle**, inspect it before sharing, and use [app issues](https://github.com/crywolf203/imessage-archive/issues) for a reproducible error. A bundle is not permission to publish actual phone backups or message exports.

## License

GPL-3.0-only. See [LICENSE](LICENSE).

## Credits and acknowledgements

iMessage Archive would not be possible without the work of these open-source authors, maintainers, and contributors. Thank you for making these tools available to the community.

| Project | Contribution to iMessage Archive |
| --- | --- |
| **[imessage-exporter](https://github.com/ReagentX/imessage-exporter), by [ReagentX](https://github.com/ReagentX) and contributors** | The core message parser, database diagnostics, HTML/text exports, and original exported conversation styling. This app invokes the upstream exporter; it does not claim authorship of that work. |
| **[libimobiledevice](https://github.com/libimobiledevice/libimobiledevice)** | iPhone communication, trust/pairing tools, device information, and local backups through `idevicebackup2`. |
| **[usbmuxd](https://github.com/libimobiledevice/usbmuxd)** | The device connection service used by the iPhone tools. |
| **[Chromium](https://www.chromium.org/Home/)** | Server-side HTML layout and print-to-PDF rendering. |
| **[ImageMagick](https://imagemagick.org/) and [libheif](https://github.com/strukturag/libheif)** | Image resizing and conversion, including HEIC/HEIF support for compatible exports and PDF images. |
| **[FFmpeg](https://ffmpeg.org/)** | Audio and video conversion when requested through the exporter's conversion options. |
| **[Flask](https://flask.palletsprojects.com/) and [Gunicorn](https://gunicorn.org/)** | The web framework and production web server. |
| **[SQLite](https://sqlite.org/)** | The conversation catalog and full-text search engine. |
| **[Beautiful Soup](https://www.crummy.com/software/BeautifulSoup/) and [lxml](https://lxml.de/)** | HTML parsing for the indexed viewer and print views. |
| **[Pexpect](https://pexpect.readthedocs.io/)** | Handling interactive password prompts from device and export tools. |
| **[pypdf](https://github.com/py-pdf/pypdf)** | Reading and merging completed PDF parts. |
| **[Python](https://www.python.org/), [Debian](https://www.debian.org/), and [Tini](https://github.com/krallin/tini)** | The application runtime, container operating-system packages, and container process management. |

This repository's custom work is the web dashboard, authentication, job orchestration, indexed search and bounded viewer pages, scheduling and storage controls, CSV/ZIP downloads, resumable PDF workflow, and Docker/Compose/Unraid packaging. Message parsing and the underlying backup, media, and rendering engines belong to the projects credited above.

Upstream projects retain their own authorship and licenses. iMessage Archive is an independent integration, not an iMazing fork or an official release from any of the credited projects.
