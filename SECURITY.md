# Security

iPhone backups, message exports, search indexes, and PDFs contain private data. Keep all mapped shares private and do not expose the web interface directly to the internet.

- Set unique `APP_PASSWORD` and `FLASK_SECRET_KEY` values.
- Use a trusted HTTPS reverse proxy for access outside the local network, then set `COOKIE_SECURE=1`.
- Leave `VIEWER_PASSWORD` empty unless read-only access is required.
- Keep the Unraid host, container image, and reverse proxy updated.
- Treat backup encryption passwords as secrets. They are used only for the requested operation and are not stored by this application.
- Back up `/data/config`; it contains the search index, operational history, and schedule state.

Report vulnerabilities privately through GitHub's security advisory feature after the repository is published. Do not include message content, backup files, pairing records, passwords, or render tokens in reports.
