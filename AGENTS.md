# MySkills

- Keep category membership, sidebar order, explicit skill order, steps, and compact-section rules in the user's data-directory `catalog.json`. The tracked `catalog.json` is a neutral first-run template, never a personal catalog.
- Do not restore dependencies on an installed coordinator skill or duplicate catalog rules in JavaScript.
- Keep Python standard-library-only and the frontend buildless.
- Preserve loopback binding, origin/host/token checks, system protection, and recoverable Trash deletion.
- Never commit installed skill contents, plugin caches, runtime tokens, machine-specific launchd files, or credentials.
- Run `python3 scripts/verify.py` before pushing. Check UI changes in a local browser. For Windows-specific changes, run the preflight on Windows before pushing. Use an existing non-publishing CI ref only if it already includes the changes. Report blockers before pushing. Monitor authorized releases in the background and report their actual status.
- Identify the existing checkout or managed service before restarting it after Python changes. Restart only the matching service; do not create a duplicate or replace a managed installation to test checkout code. HTML and catalog changes are picked up on refresh.

- Skill folders are the source of truth for names, picker metadata, descriptions, icons, instructions, and size. Derive these at runtime; never duplicate them in this repository. Keep only dashboard-specific organization and presentation rules here.

- Managed installation data lives outside release files. Preserve catalogs and settings across updates; development checkouts must never auto-update.
- Native macOS and Windows are supported. Keep runtime, service, Trash/Recycle Bin and usage-log behavior portable; do not claim Windows verification without running there.
- Release packaging uses an explicit application allowlist. Do not publish, change visibility or rewrite history without explicit authorization.

## Refresh public screenshots when the displayed UI changes

When a change makes the public screenshots outdated, update both showcases with the shipped dashboard using sample data:

1. Capture the running dashboard on the My skills tab at 1500×1000 and 2× scale. Inspect the image and convert it with `cwebp -q 82`.
2. Replace `docs/screenshot.webp` (the README image) and keep the README alt text accurate.
3. In `~/CODE/jdjuan.io`, add the image as a new versioned file (`public/myskills/dashboard-vN.webp`, so caches don't serve the old one), point `pages/myskills.tsx` at it (the image, its link and the JSON-LD `screenshot`), and update any page copy or FAQ answer the change made out of date, with the Spanish strings in `content/locales/es.json`. Follow that repo's AGENTS.md for local checks and its standing publish workflow.
