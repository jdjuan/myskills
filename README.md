# Skill Dashboard

A local dashboard for browsing personal Codex skills, cached plugin collections, and built-in system skills. Runs on Python's standard library with plain HTML, CSS, and JavaScript. No database, frontend build, or hosting service.

## Run

Requires Python 3.10 or later:

```sh
python3 server.py
```

Open http://127.0.0.1:8765. Use `--port 8766` to choose another port. `Start.command` is a macOS double-click launcher for a manual server.

## Background service on macOS

```sh
python3 manage-service.py start
python3 manage-service.py status
python3 manage-service.py stop
```

The user-level `io.jdjuan.skills-library` launchd service starts at login and restarts if it exits. It runs while the Mac is awake and the user is logged in. `start` installs or updates the service to this checkout's absolute location. Run it again after moving the checkout. Do not run a manual server on the same port while the service is active.

Configuration: `~/Library/LaunchAgents/io.jdjuan.skills-library.plist`. Logs: `~/Library/Logs/SkillsLibrary/`.

## One catalog for organisation

**`catalog.json` is the only source for grouping and order.** No installed coordinator skill is needed.

- `groups`: ordered sidebar categories. Each group declares its section, area, label, description, explicit skill order, optional prefixes, and whether to show step labels. Explicit members may be strings or `{ "name": "…", "step": 1 }` objects. New matching prefix members follow the explicit members alphabetically.
- `fallback_group`: newly discovered personal skills that have no rule appear here.
- `compact_sections`: prefix/member matches, titles, and order for compact supporting-tool sections. These sections appear after standard cards.
- `plugin_labels`: friendly names for automatically discovered plugin origins. Plugin skills belong exclusively to their own plugin collection.
- `display`: optional friendly names and short card summaries. Actual skill instructions are unchanged.

The API sends ordered group definitions and each skill's group positions, step labels, and compact-section metadata to the UI. Refresh rescans skill files and rereads the catalog. The frontend renders those values; it does not contain a second list of category memberships or workflow stages. Visual styles and decorative icons remain frontend code.

Product order is definition → setup → design → build, with supporting design and authentication skills alongside their stages. Marketing order is SEO, video, images, content/outreach, launches, and analytics. Article publishing stays under Personal. HyperFrames tools share the video category and appear last in compact rows.

## Skill sources and deletion

Reads `${CODEX_HOME:-~/.codex}/skills`, `~/.agents/skills`, and `${CODEX_HOME:-~/.codex}/plugins/cache`. Actual skill folders, plugin caches, and credentials are **not stored in this repository**. Cached plugin files do not prove a plugin is currently enabled. Project-local skills outside these roots are not enumerated.

Deletion requires confirmation and moves the entire skill folder and identical cached copies to Finder's Trash. System skills and symlinked locations are protected. Plugin updates may restore cached skills. Existing Codex chats may need restarting to pick up installed-skill changes.

The server binds only to loopback, checks host and origin, requires a per-process token for mutations, and serves only the UI and skill API. It is intended for local use.

## Verify

```sh
python3 -m unittest discover -s tests -v
```

Tests cover independent catalog ordering, source separation, future prefix matches, fallback categories, recoverable deletion using disposable folders, and HTTP mutation protections. Tests do not modify installed skills.
