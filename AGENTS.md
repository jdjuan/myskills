# Skill Dashboard

- Keep category membership, sidebar order, explicit skill order, steps, and compact-section rules in the user's data-directory `catalog.json`. The tracked `catalog.json` is a neutral first-run template, never a personal catalog.
- Do not restore dependencies on an installed coordinator skill or duplicate catalog rules in JavaScript.
- Keep Python standard-library-only and the frontend buildless.
- Preserve loopback binding, origin/host/token checks, system protection, and recoverable Trash deletion.
- Never commit installed skill contents, plugin caches, runtime tokens, machine-specific launchd files, or credentials.
- Before pushing, run `python3 scripts/verify.py` (syntax, the full `python3 -m unittest discover -s tests -v` suite, and release ZIP validation). UI changes also need local browser verification. New or changed Windows-specific behavior must pass Windows CI on a non-publishing branch before merging to main. After an authorized final push, use a background release monitor to surface failures; do not describe a queued release as published.
- Identify the existing checkout or managed service before restarting it after Python changes. Restart only the matching service; do not create a duplicate or replace a managed installation to test checkout code. HTML and catalog changes are picked up on refresh.

- Skill folders are the source of truth for names, picker metadata, descriptions, icons, instructions, and size. Derive these at runtime; never duplicate them in this repository. Keep only dashboard-specific organization and presentation rules here.

- Managed installation data lives outside release files. Preserve catalogs and settings across updates; development checkouts must never auto-update.
- Native macOS and Windows are supported. Keep runtime, service, Trash/Recycle Bin and usage-log behavior portable; do not claim Windows verification without running there.
- Release packaging uses an explicit application allowlist. Do not publish, change visibility or rewrite history without explicit authorization.
