# Cloudflare R2 app: root-cause analyses (RCA)

## RCA-2: "[Errno 2] No such file or directory: '/api/method/frappe_cloudflare_r2.controller.download_file?key=…'" (17 Nov 2026)
- **Symptom:** GrowthOS image upload (`growthos.api.images.upload`) returned 500, and LinkedIn publish failed with "The image is missing or changed after approval". The error was `[Errno 2] No such file or directory: '/api/method/frappe_cloudflare_r2.controller.download_file?key=private/…png'`.
- **Root cause:**
  1. After a file is moved to R2, its local copy is removed and `file_url` becomes the R2 download address.
  2. Frappe's `File.get_full_path()` (`frappe/core/doctype/file/file.py:751-784`) has no case for that address and returns it unchanged as if it were a path on disk.
  3. This app makes `get_content()`, `get_raw_content()` and `exists_on_disk()` R2-aware (`overrides/file.py`). It doesn't cover `get_full_path()` or the legacy helpers `frappe.utils.file_manager.get_file()` and `get_file_path()`.
  4. Any code that does `open(file.get_full_path())` or `get_file(...)` on an R2 file therefore fails. GrowthOS reads the image by path, so upload processing and the publish-time check both fail.
- **Also affected:** any app or Frappe feature that reads files by path: thumbnails, image optimisation, legacy file helpers.
- **Proposed fix (not built yet):**
  - **A, in callers:** read bytes with `file_doc.get_raw_content()` instead of a path. Do this now in GrowthOS.
  - **B, in this app:** add a supported temporary local copy, `with file_doc.local_copy() as path:`, for libraries that need a real path.
  - **D, in this app:** make path-based helpers fail with a clear message ("stored in R2; use get_raw_content()").
  - **C, not recommended without a design:** make `get_full_path()` return a cached copy. Frappe also uses that path to delete and validate files.

## RCA-1: Private R2 files could be served to guests
- **Root cause:** `download_file` serves an object if **any** File row with its address is public. Also, `key_from_url` accepts variant addresses.
- **Fix plan:** `docs/plans/private-file-exposure-fix.md` on branch `fix/private-file-exposure` (parked).
