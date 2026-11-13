# MySkills

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

## Every push to main: refresh the screenshot and the product page (Juan, 13 Nov 2026)

Each time you push MySkills changes to `main`, also update both public showcases so they match the shipped dashboard:

1. Capture the running dashboard at 1500×1000, 2× scale, on the My skills tab: `"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --hide-scrollbars --user-data-dir=<temp dir> --window-size=1500,1000 --force-device-scale-factor=2 --virtual-time-budget=8000 --screenshot=<file>.png http://127.0.0.1:8765/`, check the image, then `cwebp -q 82`.
2. Replace `docs/screenshot.webp` (the README image) and keep the README alt text accurate.
3. In `~/CODE/jdjuan.io`, add the image as a new versioned file (`public/myskills/dashboard-vN.webp`, so caches don't serve the old one), point `pages/myskills.tsx` at it (the image, its link and the JSON-LD `screenshot`), and update any page copy or FAQ answer the change made out of date, with the Spanish strings in `content/locales/es.json`. Follow that repo's AGENTS.md for local checks and its standing publish workflow.
