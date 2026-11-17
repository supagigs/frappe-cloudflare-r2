# Cloudflare R2 app: root-cause analyses (RCA)

## RCA-3: An R2 file's *Is Private* could change while its object stayed in the old bucket (17 Nov 2026, found in review of 1.0.1)
- **Symptom (found in review, not reported):** a private R2 file switched to public kept its object in the private bucket under a `/private/` key. `download_file` trusted the File row, so Guest got a signed URL for the private object.
- **Root cause:** R2 objects are never moved between buckets; `is_private` on the File row and the object's real location could disagree. `download_file` decided access and bucket from the row.
- **Fix (1.0.1):**
  - `R2FileMixin.validate` refuses an *Is Private* change on a File stored in R2, for every user (Administrator included): "Change visibility by re-uploading; R2 files keep their visibility."
  - `download_file` takes private or public from the key's own folder (`storage.key_is_private`). Free access needs both a public key and a public row; otherwise File read permission is checked. The signed URL uses the key's bucket. Keys without a `private`/`public` folder (custom `r2_key_generator`) fall back to "private if any row is private".
  - `cache.remove` deletes both the private and public cached copies, so a row changed before this fix leaves nothing behind.
- **Tests:** `tests/test_r2_cache_fixes.py`.

## RCA-2: "[Errno 2] No such file or directory: '/api/method/frappe_cloudflare_r2.controller.download_file?key=…'" (17 Nov 2026)
- **Symptom:** GrowthOS image upload (`growthos.api.images.upload`) returned 500, and LinkedIn publish failed with "The image is missing or changed after approval". The error was `[Errno 2] No such file or directory: '/api/method/frappe_cloudflare_r2.controller.download_file?key=private/…png'`.
- **Root cause:**
  1. After a file is moved to R2, its local copy is removed and `file_url` becomes the R2 download address.
  2. Frappe's `File.get_full_path()` (`frappe/core/doctype/file/file.py:751-784`) has no case for that address and returns it unchanged as if it were a path on disk.
  3. This app makes `get_content()`, `get_raw_content()` and `exists_on_disk()` R2-aware (`overrides/file.py`). It doesn't cover `get_full_path()` or the legacy helpers `frappe.utils.file_manager.get_file()` and `get_file_path()`.
  4. Any code that does `open(file.get_full_path())` or `get_file(...)` on an R2 file therefore fails. GrowthOS reads the image by path, so upload processing and the publish-time check both fail.
- **Also affected:** any app or Frappe feature that reads files by path: thumbnails, image optimisation, legacy file helpers.
- **Proposed fix:**
  - **A, in callers:** read bytes with `file_doc.get_raw_content()` instead of a path. Do this now in GrowthOS.
  - **B, in this app:** add a supported temporary local copy, `with file_doc.local_copy() as path:`, for libraries that need a real path.
  - **D, in this app:** make path-based helpers fail with a clear message ("stored in R2; use get_raw_content()").
  - **C, not recommended without a design:** make `get_full_path()` return a cached copy. Frappe also uses that path to delete and validate files.
- **Solution in brief:**
  - When code asks an R2 file for its disk path, the R2 app hands back a real, private, read-only copy, downloaded on demand and cached. Code written for local storage then works unchanged.
  - Saving, checking, deleting, trashing and rolling back still act on the object in R2.
  - A daily job clears old copies.
  - Two ERPNext doctypes that write back into their file stay on local disk.
  - Only this app changes. Frappe, ERPNext, GrowthOS and the other apps are unchanged and benefit automatically.
  - Status: built and tested on staging (36 passing), under review, not yet released.
  - **Review fixes (1.0.1):**
    - **Grace rule:** a cached copy used in the last hour is never removed for size, by the daily sweep or by the size check that runs when a copy is added. The limit is soft and a warning is logged when it can't be met.
    - **Write-back files already in R2:** the post-model-sync patch `move_write_back_files_to_local` moves them to the server disk through the R2 → local exit path (`migration._r2_to_local`), re-points File rows and Attach fields, and is idempotent. Each failure gets an Error Log entry and the file stays in R2.
    - **Visibility:** see RCA-3.
- **Requirement (user decision):** R2 replaces Frappe's default storage. Anything that works with local storage must keep working when R2 is on, in every app, and no other app will change. That rules out D on its own, and calls for C with a design.
- **Resolution (1.0.1, branch `fix/r2-local-file-access`): C, built as a private cache. B is kept as the explicit API. A is no longer needed.**
  - **C, paths:** for an R2 file, `get_full_path()` returns `sites/<site>/private/r2_cache/<sha256("private"|"public":key)><ext>` (`cache.py`).
    - The copy is downloaded on first use: streamed into a temp file in the same folder, made read-only, then renamed into place (atomic). It is reused while it exists, and each use updates its mtime.
    - The folder is 0700 and the copy is 0400. It is never under `public/`.
    - Reuse is safe because an R2 key never gets new bytes: `R2.make_key` adds a random token to every upload, a deferred retry uses the key chosen at deferral, and an S3 migration copies the same bytes to the same key. A custom `r2_key_generator` must keep this rule; yardi_reports' does (DL-1).
  - **Lifecycle methods act on R2, never on the cache:**
    - `validate_file_on_disk`, `exists_on_disk`, `get_content` and `get_raw_content` are overridden. A save makes no download.
    - `on_rollback` cleans up the server copy under its local URL (`doc.flags.r2_uploaded_from`) and drops any cached copy. **This fixes a leak:** before, the server copy of a rolled-back upload stayed on disk.
    - Delete and trash: `delete_from_r2` removes the cached copy after commit once no other File uses the object, whatever the trash setting. Moving back to local (`r2_to_local`) removes it too.
  - **Cleanup:** `tasks.daily` runs `cache.sweep()`.
    - It removes copies unused for *Remove Copies Unused For (days)* (default 7) and temp files over a day old. It then removes the least recently used copies until the cache is under *Cache Size Limit (MB)* (default 2048). `0` turns a limit off.
    - `bench --site <site> execute frappe_cloudflare_r2.cache.clear` empties it.
    - Patch `set_cache_defaults` writes the defaults on sites that already saved their settings. It leaves sites that never saved them alone; Frappe fills in the defaults when the settings load.
  - **Writes into the path (decision):** keep the known writers local, and make the cache read-only. Re-uploading changed copies is not done.
    - Re-uploading would need either to change bytes under an existing key, which breaks the reuse rule and other servers' caches, or to give the File a new key and URL. The second would break the `reposting_data_file` link that `stock_ledger.py` stores, and any other stored link, and a change made between checks could still be lost.
    - Instead, `WRITE_BACK_DOCTYPES` (*Repost Item Valuation*, `stock_ledger.py:516`; *Bank Statement Import*, `write_files()`) are always kept on the server disk, whatever *Keep Local for DocTypes* says. A default in that setting wouldn't reach sites that already saved it, and removing it there would silently break reposting.
    - Any other write into a cached copy fails with `PermissionError`, because the copy is 0400, instead of being lost silently. If the bench runs as root, the permission is ignored.
    - Files of those doctypes already in R2 can't be written back; their next write fails loudly. Staging has none.
  - **B:** `local_copy()` yields `get_full_path()`: the file's own path, or the cached copy. It refuses folders and remote URLs that are not R2.
  - **Every caller of `get_full_path()` in Frappe's `File` (v16.53.0):**

    | Core caller | For an R2 file |
    |---|---|
    | `validate()` → `validate_file_on_disk()` | Overridden: returns `True` with no download. Runs on every save. |
    | `validate_file_path()` | Returns early for remote files (R2 URLs are remote). |
    | `exists_on_disk()` | Overridden: `True`, with no download. |
    | `get_content()` | Overridden: reads from R2. |
    | `write_file()`, `save_file()` | Return early for remote files. |
    | `on_rollback()` | Overridden (see above). The `original_content` branch can't be reached for R2. |
    | `unzip()` | Core's `unzip()` reads the cached copy. The children are uploaded to R2, and the zip's copy is removed when the zip File is deleted. |
    | `_delete_file_on_disk()` | Doesn't use the path. The R2 object and the cached copy are removed by the `on_trash` hook. |
    | `make_thumbnail()` | Uses the URL, not the path. **Private R2 image:** skipped and logged, because core writes thumbnails under `public/`. **Public R2 image:** unchanged; core fetches it over HTTP. |
    | `optimize_file()` | Refused with a clear message. Saving optimised bytes would mean re-uploading under a new key (see the write decision). Before, it reported success but saved nothing. |
  - **Now working outside `File`, through `get_full_path()`:**
    - Frappe: `xlsxutils.read_xlsx_file_from_attached_file(file_url=…)`.
    - ERPNext: `chart_of_accounts_importer.generate_data_from_csv`, `import_supplier_invoice` (zip) and `transaction_deletion_record` (CSV).
    - Other apps: `supasign/documents.py:read_bytes`, `lms/course_import_export.py` and `lms/api.py`.
    - GrowthOS.
  - **Not covered: these build the path without `File.get_full_path()`.** This app can't reach them without monkeypatching core.
    - `frappe.utils.file_manager.get_file_path()` and `get_file()`: they look up `file_url` and map only `/files/` and `/private/files/`. For an R2 URL they throw "There is some problem with the file url". Their callers:
      - `frappe.utils.data.pdf_to_base64` (also exposed to server scripts);
      - `erpnext/accounts/doctype/bank_statement_import/bank_statement_import.py:175` (`convert_mt940_to_csv`; Bank Statement Import is now kept local, so its new files are covered);
      - `erpnext/accounts/doctype/chart_of_accounts_importer/chart_of_accounts_importer.py`, which uses its own `get_file()` built on `File` and so is covered.
    - `frappe/core/doctype/package_import/package_import.py:56`: `get_files_path(attachment.file_name, …)`.
    - `erpnext/regional/italy/utils.py:134` (`download_zip`): `get_files_path(file.file_name, …)`.
    - Read and found not affected, because they only handle `/files/` or `/private/files/` URLs and treat others as remote:
      - Frappe: `utils/data.py:1783` (inline image base64), `utils/typst_emitter.py:989`, `email/email_body.py:649`, `utils/pdf_generator/page.py:127`;
      - other apps: `crm/api/activities.py:621`.
    - Not File lookups: `lms/course_import_export.py:266` and the yardi_reports evaluation and simulation paths write their own files.
  - **Tests:** `TestLocalFileAccess` in `tests/test_cloudflare_r2.py` covers:
    - the GrowthOS pattern;
    - privacy and read-only mode;
    - reuse;
    - the write-back decision;
    - delete, shared delete, rollback, sweep and clear;
    - save without download;
    - unzip;
    - thumbnails and optimisation.
  - **Test suite safety:** the suite is safe to run on a site that already uses R2 (it was run on staging). It restores the site's settings, removes only its own rows and cached copies, and skips the five tests that act on every File on the site unless run in CI or with `allow_site_wide_r2_tests`.

## RCA-1: Private R2 files could be served to guests
- **Root cause:** `download_file` serves an object if **any** File row with its address is public. Also, `key_from_url` accepts variant addresses.
- **Fix plan:** `docs/plans/private-file-exposure-fix.md` on branch `fix/private-file-exposure` (parked).
