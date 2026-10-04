# Changelog

## 4.0.1 - 2026-10-04

- Fixed backup percentage parsing so progress and ETA update for normal tool output.
- Added real image tests for Chromium PDFs, HEIC conversion, indexed browsing, CSV, and ZIP exports.
- Documented how to retain existing Unraid storage mappings during upgrades.

## 4.0.0 - 2026-10-02

- Added explicit USB and Wi-Fi targeting for multiple iPhones.
- Made normal backups incremental and added encrypted-backup password confirmation.
- Added preflight checks, backup integrity verification, per-backup diagnostics, and persistent run history.
- Added a bounded-memory SQLite FTS5 conversation index with fast viewer parts and cancellable progress.
- Added HTML, text, streamed CSV, portable ZIP, and three-profile PDF exports.
- Added chunked, resumable PDF rendering with image optimization, progress, ETA, and cancellation.
- Added dark mode, independent desktop scrolling, responsive mobile controls, and read-only accounts.
- Added scheduled Wi-Fi backups, preferred-device selection, webhook notifications, and safe retention.
- Added guarded backup deletion, storage visibility, support bundles, and health checks.
- Added GitHub Container Registry automation, an Unraid template, security guidance, and release smoke tests.
