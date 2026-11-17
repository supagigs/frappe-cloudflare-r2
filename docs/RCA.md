# Cloudflare R2 app: root-cause analyses (RCA)

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
- **Resolution (1.0.1, branch `fix/r2-local-file-access`): B and D built; C not built. A is still needed in GrowthOS.**
  - **B:** `R2FileMixin.local_copy()` (`overrides/file.py`). It's a context manager. A local file yields its own path. An R2 file is streamed into a `tempfile.mkstemp` file in the system temp folder (mode 0600, same extension), and the path is yielded. The file is always deleted afterwards, even if the block or the download raises. Permissions are the caller's job, as with `get_raw_content()`.
  - **D:** `get_full_path()` raises `R2FileNotOnDiskError` (a `frappe.ValidationError`) for an R2 file: "File X is stored in Cloudflare R2, not on disk. Use file_doc.get_raw_content() or `with file_doc.local_copy() as path:`." A local file's path is unchanged.
  - **Every caller of `get_full_path()` in Frappe's `File` (v16.53.0), and how each is handled for an R2 file:**

    | Core caller | For an R2 file |
    |---|---|
    | `validate()` → `validate_file_on_disk()` | Overridden: returns `True` without calling `get_full_path()`, as core already did for URLs. Runs on every save. |
    | `validate_file_path()` | No change: returns early for remote files. R2 URLs are remote (`/api/method/` or `https://`). |
    | `exists_on_disk()` | Already overridden: `True`. |
    | `get_content()` | Already overridden: reads from R2. With `content` set, core returns before calling `get_full_path()`. |
    | `write_file()`, `save_file()` | No change: both return early for remote files. |
    | `on_rollback()` | Overridden. `finish_upload` records the server copy's URL in `doc.flags.r2_uploaded_from`. On rollback, core's cleanup runs against that URL. **This fixes a leak:** before, core looked for the R2 URL, and the server copy of a rolled-back upload stayed on disk. The `original_content` branch can't be reached for an R2 file, because `save_file()` returns early. |
    | `unzip()` | Overridden: permission check, then core's `unzip()` runs on a `local_copy()`. `get_full_path()` returns the temporary path only while it runs. The children are uploaded to R2 as usual. |
    | `_delete_file_on_disk()` (`on_trash`, rollback) | Doesn't call `get_full_path()`. No change. The R2 object is removed by the `on_trash` hook. |
    | `make_thumbnail()` | Doesn't call `get_full_path()`. **Private R2 image:** skipped with a log line, because core would write a private image's thumbnail into the public files folder. **Public R2 image:** unchanged; core fetches it over HTTP. |
    | `optimize_file()` | Doesn't call `get_full_path()`. Before, core optimised the bytes but saved nothing for a remote file, while the form said "Image optimized". Now it is refused with a clear message, and the file is unchanged. The only caller is the form's Optimize button. Upload-time optimisation happens before the upload and is unaffected. |
  - **Outside `File`:** `frappe.utils.xlsxutils.read_xlsx_file_from_attached_file(file_url=…)` and the ERPNext callers below now get the clear error instead of `[Errno 2]`. They failed before as well. The ERPNext callers are `stock_ledger.py` (Repost Item Valuation data file), `bank_statement_import.write_files`, `chart_of_accounts_importer.generate_data_from_csv`, `import_supplier_invoice` and `transaction_deletion_record`. Other apps on the bench: `supasign/documents.py:read_bytes` and `lms/course_import_export.py`, `lms/api.py`.
  - **`frappe.utils.file_manager.get_file()` / `get_file_path()`:** not changed. They are module functions with no hook, and changing them would mean monkeypatching core. For an R2 URL, `get_file_path()` still returns the URL and `get_file()` still raises `[Errno 2]`. This is documented in the README. Use `frappe.get_doc("File", name).get_raw_content()` instead.
  - **Tests:** `TestLocalFileAccess` in `tests/test_cloudflare_r2.py`.
  - **Test suite safety:** the suite is now safe to run on a site that already uses R2 (it was run on staging). It restores the site's settings, removes only its own rows, and skips the five tests that act on every File on the site unless run in CI or with `allow_site_wide_r2_tests`.

## RCA-1: Private R2 files could be served to guests
- **Root cause:** `download_file` serves an object if **any** File row with its address is public. Also, `key_from_url` accepts variant addresses.
- **Fix plan:** `docs/plans/private-file-exposure-fix.md` on branch `fix/private-file-exposure` (parked).
