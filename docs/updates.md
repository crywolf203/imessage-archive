# Dependency updates and candidate testing

Dependency updates are proposals, not automatic upgrades of a user's container. Renovate must be installed/enabled for this repository before its configuration can create pull requests. Install the official [Renovate GitHub App](https://github.com/apps/renovate), select **Only select repositories**, and choose `crywolf203/imessage-archive`. Review its permissions and any onboarding request. Adding `renovate.json` alone does not install the bot.

## What is monitored

- Python packages in `app/requirements.txt`
- Python and Rust Docker base images, including digest updates
- GitHub Actions versions and commit digests
- The `IMESSAGE_EXPORTER_VERSION` default in `Dockerfile`, using the crates.io datasource

Renovate checks published dependency versions, not arbitrary upstream commits. Update requests are limited to four open PRs, created before 6 a.m. Monday in America/New_York. Existing PR checks/rebases may run outside that window. Automatic merging is disabled; major updates additionally require approval in the Dependency Dashboard. The exporter version has one source of truth in `Dockerfile`; source Compose builds use that default rather than a second fixed version.

Debian-installed packages such as Chromium, libimobiledevice, usbmuxd, ImageMagick and FFmpeg are refreshed by the **Build candidate** workflow every Monday at 08:23 UTC. Scheduled builds pull base images and bypass the Docker build cache. This refreshes packages available to the chosen Debian release, not necessarily the latest upstream releases. Scheduled runs can be delayed by GitHub. To force a refresh, run **Build candidate** manually with **refresh_packages** checked.

## Candidate pipeline

Main-branch pushes, version-tag pushes, pull requests, manual runs and scheduled refreshes build an amd64 image. The workflow runs smoke/authentication/configuration tests, builds and loads the candidate, then tests the actual image through isolated Compose before publication:

- A generated unencrypted iOS backup is read by the real exporter: HTML/text, contact filtering, HTML escaping, diagnostics and viewer indexing must work without modifying the source message database.
- 10,450 generated HTML messages exercise indexing, bounded pages, search and CSV.
- Real Chromium generates text and image PDFs; PDF content and embedded images are inspected. HEIC conversion and portable ZIP contents are checked.
- Playwright checks login, navigation, dark/light themes, image loading and mobile width.
- Timing limits in `tests/performance-budgets.json` catch severe slowdowns. They are generous absolute ceilings for shared CI runners, not a precise percent-regression comparison or a forecast for real phone data.

Each run uploads `compose-runtime-proof` containing timings, tool versions and synthetic-data screenshots. Successful publishable runs also upload `candidate-metadata`, with `candidate.json` recording the exact source revision, run/attempt, image tag and registry digest. Artifacts expire after 30 days; expired/missing metadata cannot be used for promotion.

Only successful candidates are published, using unique `candidate-<commit>-<run>-<attempt>` or `pr-<number>-<commit>-<run>-<attempt>` tags. Fork PRs are built/tested but never receive registry-publishing credentials. No `pull_request_target` execution or privileged `workflow_run` handoff is used. Same-repository PR authors and installed bots are trusted repository collaborators. The candidate workflow cannot publish `latest` or overwrite a versioned release tag.

## Try a candidate on Unraid

Use a private LAN. Do not replace your production container or reuse its folders, pairing records, or phone. The separate [staging Compose file](../docker-compose.staging.yml) uses another container name, port 8088, new named volumes, no privileged access and no USB mount. USB, schedules, notifications and retention are disabled. This is for viewer/export/PDF testing, not real device backups.

From a current checkout, run in the Unraid terminal:

```sh
cp -n .env.staging.example .env.staging
chmod 600 .env.staging
openssl rand -hex 32
nano .env.staging
```

In `.env.staging`, replace the password and session-secret placeholders. For `STAGING_IMAGE`, paste the `image_reference` value (`ghcr.io/crywolf203/imessage-archive@sha256:...`) from a successful candidate's `candidate.json`, not `latest` or the sample tag. Use a free `STAGING_PORT`. Sign in as `staging-admin` with your staging password.

```sh
docker compose --env-file .env.staging -p imessage-staging -f docker-compose.staging.yml config --quiet
docker compose --env-file .env.staging -p imessage-staging -f docker-compose.staging.yml pull
docker compose --env-file .env.staging -p imessage-staging -f docker-compose.staging.yml up -d --wait
```

Open `http://YOUR-UNRAID-IP:8088` (or your selected port). New staging storage starts empty. To exercise the viewer, copy only synthetic HTML and attachments, or a separate copy of an export you are authorized to test, into staging's `/data/exports/current`, then select **Refresh library**. Staging exports are also sensitive and unencrypted. Never bind-mount production storage into staging. Real phone backup/trust, encrypted-backup decryption, Wi-Fi discovery and photo-heavy performance still require separate, deliberate device testing before release.

To stop staging while retaining its test volumes:

```sh
docker compose --env-file .env.staging -p imessage-staging -f docker-compose.staging.yml down
```

## Approve a stable update

1. Review the dependency PR, upstream release notes and candidate results. Merge the reviewed PR; its PR image itself is not eligible for stable promotion.
2. Wait for the resulting **main-branch Build candidate** run to finish successfully. Review its `compose-runtime-proof` and `candidate-metadata` artifacts and complete appropriate device testing.
3. Open **Actions -> Promote tested candidate -> Run workflow**, choose branch `main`, enter that successful run's numeric ID, and check the approval box.
4. The workflow validates the run/repository/branch/attempt/commit and digest metadata, requires the commit to belong to main history, checks the image's revision label, and promotes that exact tested digest to `latest` without rebuilding.

This manual dispatch and confirmation box are the release gate. They do not configure GitHub branch protection or environment required reviewers; those can be added separately in repository settings. Repository writers can still modify workflows, so this is not protection against a malicious repository administrator.

Failed, cancelled, fork, PR, tag-only, mismatched or incomplete runs cannot be promoted. An older successful main candidate can be deliberately selected for rollback while its provenance artifact remains available. Promotion changes `latest`, not existing containers and not `v4.0.1` or other versioned release tags. Versioned releases need a separate reviewed release/tagging process.

Users tracking `latest` then update through Unraid or pull/recreate their production Compose stack, preserving every existing data mapping and secret. Users pinned to a version tag stay on that release. A container restart alone does not upgrade dependencies. Candidate images and synthetic test artifacts do not prove universal compatibility with every iOS version or backup.
