# Fixtures

Sanitized, trimmed snapshots of Bilibili responses used by the offline contract tests.
Media URLs and signatures are replaced with `example.invalid`; only response shape,
quality metadata and titles are preserved.

- `view.json` — `/x/web-interface/view` for BV1GJ411x7h7 (recorded 2026-10-23)
- `playurl_regular.json` — `/x/player/playurl` (DASH, 64K/132K/192K AAC; no Hi-Res)
- `playurl_flac.json` — synthetic shape matching a Hi-Res FLAC response; update with a real
  logged-in response when one is available (`python scripts/update_fixtures.py --url URL`).

Refresh them intentionally with `scripts/update_fixtures.py`, then review the git diff:
an unexpected diff is an early warning that the upstream contract changed.
