# Skill Dashboard

- Keep category membership, sidebar order, explicit skill order, steps, friendly labels, summaries, and compact-section rules in `catalog.json`.
- Do not restore dependencies on an installed coordinator skill or duplicate catalog rules in JavaScript.
- Keep Python standard-library-only and the frontend buildless.
- Preserve loopback binding, origin/host/token checks, system protection, and recoverable Trash deletion.
- Never commit installed skill contents, plugin caches, runtime tokens, machine-specific launchd files, or credentials.
- Verify changes with `python3 -m unittest discover -s tests -v`. UI changes also need local browser verification.
- The live service runs from this checkout. Restart it with `launchctl kickstart -k gui/$(id -u)/io.jdjuan.skills-library` after Python changes; HTML and catalog changes are picked up on refresh.
