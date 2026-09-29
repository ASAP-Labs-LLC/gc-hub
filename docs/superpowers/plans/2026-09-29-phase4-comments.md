# Phase 4 (lane P4): comments, presets, report log — implementation plan

Spec: `docs/superpowers/specs/2026-09-29-phase3-4-bullets-comments-design.md`
(rev 2), section *Phase 4: Comments* and the lane/ownership table.
Branch: `p4/comments` from `feat/phase3-4`. Strict TDD: every task starts with
a failing test, committed together with the code that turns it green.

> **v3.1.0 (sign-in):** the self-declared initials below were replaced by the
> signed-in account name (`author_name`; initials derived by
> `comments.initials_from_name`, any sent by a client ignored; no initials box).
> See `docs/superpowers/specs/2026-09-29-public-url-login-design.md` D6.

## Scope (P4 only)

| Area | Files |
|---|---|
| Migration v2 (`comment_presets` + seeds, `sample_comments`, `report_log`), `REQUIRED_COLUMNS`, store helpers | `store.py` |
| Comment rules, `for_report`, `log_report` | new `comments.py` |
| Routes | new `comments_api.py`; one Blueprint registration in `app.py` |
| Analysis-tab comments UI | new `static/js/comments.js`; the Comments section, initials box and script tag in `templates/index.html`; the annotation code and `comments.js` init wiring in `static/js/app.js` |
| Presets admin | a section of `templates/hub_admin.html`, `static/js/hub_admin.js` |
| Route fates | new rows in `docs/superpowers/plans/2026-09-28-phase2-T4-routes.md` |

Out of scope (P3): `analysis_core.py`, app.py report routes,
`_generate_analysis_report_pdf`, `_run_export_analysis`, the QBench loop,
`report_payload.js`, the bullets/diff-plot/export-modal/queue-payload areas of
`app.js`, `settings.py`. P3 *calls* `comments.for_report` and
`comments.log_report`.

## Frozen seam for P3

```python
comments.for_report(sample_id: int, db) -> list[dict]
    # non-deleted comments of the sample, oldest first (created_at, id):
    # [{id, text, initials, created_at, t0, t1}]   (t0/t1 None unless annotation)

comments.log_report(sample_id: int, *, kind: str,            # 'download'|'zip'|'qbench'
                    revision=None, standard_name=None, params=None, ranges=None,
                    windows=None, bullets=None, bullets_text=None, conclusion=None,
                    conclusion_edited=None, comment_ids=None, pdf_sha256=None,
                    author_initials=None, author_ip=None, app_version=None,
                    created_at=None, db) -> int                # report_log.id
```

`params`/`ranges`/`windows`/`bullets`/`comment_ids` are JSON-encoded
(`*_json` columns); `app_version` defaults to `version.APP_VERSION`.

## Tasks

### T1 — Migration v2 (store.py)
Red:
- `tests/store/test_store.py`: `SPEC_COLUMNS` gains the three tables;
  `SCHEMA_VERSION == 2`; the WAL/atomicity tests use `SCHEMA_VERSION`, not
  literal 1/2.
- new `tests/store/test_store_v2.py`:
  - a v1 database (built by running only `MIGRATIONS[0]`) with samples keeps
    every row and column after `migrate` (additive), and gains the tables and
    indexes; a `pre-migrate-1-*.db` backup is written;
  - the 4 presets are seeded once, in the v2 step: a second `migrate`, and
    a `migrate` after an admin deleted/edited presets, never re-seeds;
  - a failed v2 step rolls back the seeds too (same transaction);
  - **v2.0.0's store.py** (committed verbatim as
    `tests/store/v2_0_0/store.py` + `paths.py`, checked equal to
    `git show v2.0.0:…` when the tag is present) runs `migrate` on a v2
    database in a subprocess: no error, `user_version` stays 2, a sample
    insert works.
Green: `MIGRATIONS` v2 step (CREATE TABLE ×3, 2 indexes, 4 INSERTs with
`created_by='seed'`), `REQUIRED_COLUMNS`, docstring.

### T2 — Store helpers
Red (`test_store_v2.py`): `comment_presets.list/get/add/update/reorder`,
`sample_comments.add/get/list/count_active/soft_delete`,
`report_log.add/list` round-trips; soft delete keeps the row and records
`deleted_*`; `list` hides deleted by default.
Green: three namespaces in `store.py`.

### T3 — `comments.py` rules, `for_report`, `log_report`
Red (`tests/test_comments.py`, in-process, no Flask):
- initials: upper-cased, `^[A-Z]{1,4}$` (`"ab"` ok, `"ABCDE"`, `"A1"`, `""`
  refused);
- text ≤ 500 (after strip), non-empty unless annotation; 100 active per
  sample (deleted ones don't count);
- preset: text **copied** (a later preset edit leaves the comment alone);
  inactive/unknown preset refused; `text` and `preset_id` together refused;
- annotation: `t0/t1` finite, both or neither, sorted; blank text defaults to
  `Marked region Cx–Cy` from the revision's `calibration_used` anchors (linear,
  extrapolated at the ends), else `Marked region a–b min`;
- `revision` = the sample's `current_revision`;
- delete: soft, needs initials, wrong sample → not found, twice → conflict;
- presets: text ≤ 200, ≤ 50 active; reorder must name every preset;
- `for_report` returns exactly `{id, text, initials, created_at, t0, t1}`,
  oldest first, no deleted, **no IP**;
- `log_report` writes one row with the spec's fields, JSON-encoded.

### T4 — `comments_api.py` routes + registration + route fates
Red (`tests/test_comments_api.py`, booted app via `bootapp.booted` +
`hub_boot.build_hub`):
- `GET/POST /api/samples/<id>/comments`, `POST …/comments/<cid>/delete`,
  `GET /api/comment-presets`, `POST /api/admin/comment-presets`
  (`action: list|create|update|reorder|deactivate|activate`);
- 415 without JSON, 413 over 64 KiB, 400 bad initials/text/limits, 404
  unknown sample/comment, 409 over 100 comments / already deleted;
- the stored `author_ip` is the request's address, never returned by GET;
- cross-site `Origin` → 403 on every comment POST;
- admin route: 415/403 without JSON/password; with it, create/update/
  reorder/deactivate work and `GET /api/comment-presets` reflects them;
- `tests/test_route_fates.py` passes with the new rows.
Green: blueprint, `app.register_blueprint(comments_api.bp)`, T4 plan rows.

### T5 — Analysis-tab comments UI
Red:
- node `tests/js/comments.test.js` (pure helpers in `static/js/comments.js`):
  `normInitials`, `validInitials`, `plotlySafe` (escapes `<`/`>`/`&`),
  `annotationOverlay(comments)` → shapes/labels only for `source='annotation'`,
  `commentLine(c)` text, `clearConfirmText(n)` names the count;
- headless Chrome `tests/test_ui_comments.py` (Plotly CDN blocked, a
  recording stub): initials remembered; preset chip adds a comment; free text
  `<img src=x onerror=…>` renders as text (no `<img>`, handler never runs);
  delete asks for confirmation; annotation modal save → exactly one POSTed
  annotation comment; shapes redrawn from GET after a sample change; the
  Plotly label text is escaped; Clear Annotations confirms with the count
  and soft-deletes only annotation comments; a queued item has no
  `annotations` key.
Green: `comments.js` (helpers + DOM, `textContent` only), index.html section,
app.js annotation code rewired (`annotationData` removed; the bullet-text
writes and `clearAllAnnotations`' line filter deleted).

### T6 — Presets admin section
Red (`tests/test_ui_comments.py` or API test): the `/admin/hub` page has the
presets panel; add/edit/reorder/deactivate via the page's JS calls the admin
route (headless: fill password, add a preset, it appears in
`GET /api/comment-presets`).
Green: `hub_admin.html` section + `hub_admin.js` code.

### T7 — Full suite, CLAUDE.md note, push, CI
Full `pytest tests/` + `node tests/js/run.js`; CLAUDE.md gets the comments
module and schema v2 lines (P4 parts only); push `p4/comments`; watch CI.
