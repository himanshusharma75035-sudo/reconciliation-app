# Known issues — deliberate non-fixes

Each item below is a real finding that was **intentionally left unchanged** because fixing
it changes runtime behavior, needs a data-migration plan, or needs finance/ops sign-off.
Do not "fix" these casually in a drive-by PR; open an issue and get sign-off first.

## Security

- **Default admin user** — `admin / Admin@1234` is seeded when no admin exists
  (`core/auth.py`). Every deployment must change it immediately. Replacing it with a
  generated one-time password is the right fix but changes first-run behavior.
- **SECRET_KEY dev fallback** — `core/auth.py` falls back to a known string when
  `SECRET_KEY` is unset and `ENV != production`. Always set a real `SECRET_KEY`
  (see `.env.example`). Hard-failing on a missing key would break casual local runs.
- **Clear/delete endpoints use three different permission gates** (an inconsistency, not a
  uniform policy): `DELETE /api/upload/clear` requires the `clear_data` permission (admins
  short-circuit); `DELETE /api/upload/clear-selected` requires only authentication
  (maker-checker intercepts it when enabled); the module clears (`/api/aeps|qr|sbi/clear`)
  require the `upload` permission. Unifying them onto one gate needs an ops decision.
- **E-Value endpoints** use bare authentication without per-module permission gating,
  unlike core upload.
- **`GET /api/admin/partners-public`** is deliberately unauthenticated — the login page
  needs it. Listed so nobody is surprised.
- **Audit log immutability** is by convention only (no DB-level enforcement).
- **Error responses** in some recon endpoints include exception text; scrub before
  exposing the API beyond a trusted network.

## Correctness quirks (frozen until there's a data plan)

- **Match-ID prefix/sequence quirks** — interbank pass scans `INT-` but mints `IBT-` IDs;
  internal self-match series naming is inconsistent. Existing IDs are audit references;
  changing the scheme retroactively breaks them. Also: `_next_seq` resolves the prefix
  WITH the db (so it can read `PartnerConfig.match_prefix`) but `_make_match_id` resolves
  it WITHOUT the db (`partner[:3].upper()` fallback). For an admin-added partner whose
  `match_prefix` ≠ `slug[:3].upper()`, the two disagree: `_next_seq` scans an always-empty
  series and every pass reissues seq 1, so IDs collide. Affects only custom-prefix admin
  partners (all shipped partners resolve identically, e.g. axis→AXI); latent across every
  pass. Fix = give `_make_match_id` the db, but that touches all passes → needs its own
  characterization first.
- **NEFT D+1 ignores amount** despite its docstring (see behavior contract #8).
- **Three ageing-bucket definitions** disagree slightly between dashboard, Open Items and
  Excel export (contract #19).
- **Format presets exist in three places** (hardcoded dict in `upload.py`, DB seed rows,
  admin-editable table) but detection reads only the hardcoded dict — admin edits to
  presets do not affect auto-detection.
- **Money is floats** with per-engine tolerances, not Decimal (contract #7/#23).
- **Match-ID allocation is single-process** — running uvicorn with multiple workers can
  mint duplicate match IDs. Run one worker until allocation is made DB-atomic.
- **SQLite under Dropbox/OneDrive sync** can corrupt the DB if two machines write
  concurrently. Production should use MySQL/PostgreSQL.

## Architecture debt

- Ingest pipeline duplicated between `routes/upload.py` (interactive) and
  `core/ingest_service.py` (watch-folder), with small deliberate divergences.
- `core/` imports from `routes/` (layering inversion).
- God files: `routes/upload.py`, `routes/recon.py`, `models/database.py`,
  `frontend/src/pages/Upload.jsx`, `Admin.jsx`. Split only with characterization tests and
  re-exporting facades so import sites keep working.
- **Characterization coverage now exists** for the highest-risk logic — the matching engine
  and the ingest parsing/classification helpers (`backend/tests/test_matching_engine_characterization.py`,
  `test_ingestion_characterization.py`, `test_ingest_pipeline_characterization.py`). This is
  the prerequisite for safely splitting the god files and de-duplicating the two ingest copies.
- `@app.on_event` startup hooks, `datetime.utcnow()`, and pydantic v1 `.dict()` are
  deprecated APIs — migrate mechanically, but `utcnow()` must keep returning **naive** UTC
  (contract #12).

## Publication checklist (before making the repo public)

- [ ] Rewrite git history (e.g. `git filter-repo`, or re-init with one clean commit of the
      current tree) — the initial commit still contains the BRD `.docx` files, pre-scrub
      sources with real account numbers, internal vendor names, and employee names in
      absolute paths. Verify afterward with `git grep <term> $(git rev-list --all)`.
- [ ] Rotate `SECRET_KEY` in every deployment (the current value matches the public code
      fallback).
- [ ] Verify `backend/recon.db` and `backend/uploads/` never appear anywhere in history.
- [ ] Change the default admin password on every live install.
- [ ] Set `ALLOWED_ORIGINS` to real origins (the example `.env` ships restrictive).
- [x] `backend/tests/test_evalue_engine.py` fixture filenames previously embedded three real
      account numbers — **done**: renamed to synthetic `*-sample.*` (tests skip when the
      external sample dir is absent, so CI is unaffected).
- [ ] `backend/instance/seed_accounts.json` (real account registry) is gitignored AND
      dockerignored — copy it to each deployment manually; fresh installs start with an
      empty registry and log a notice.
