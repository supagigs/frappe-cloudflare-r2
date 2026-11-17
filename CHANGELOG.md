# Changelog

## 1.0.1 (17 Nov 2026)

R2 is now a drop-in for Frappe's local storage: code that reads files by their disk path works the same when they are in R2 (RCA-2 in `docs/RCA.md`).

- **Fixed:** `File.get_full_path()` on an R2 file returns a real path to a private cached copy, downloaded on first use. `open(file_doc.get_full_path())`, PIL, PDF tools, openpyxl, zipfile and Frappe's Unzip now work. Before, it returned the download URL, and opening it failed with `[Errno 2] No such file or directory: '/api/method/…'`.
- **New: server cache.**
  - **Location and permissions:** copies are kept in `sites/<site>/private/r2_cache/`. The folder is 0700 and each copy is read-only (0400). Copies are written atomically.
  - **Cleanup:** a copy is removed when its last File is deleted. A daily job removes copies unused for 7 days and keeps the cache under 2048 MB. Both limits are new settings under *Server Cache* in Cloudflare R2 Settings.
  - **Emptying it by hand:** `bench --site <site> execute frappe_cloudflare_r2.cache.clear`.
- **New:** `with file_doc.local_copy() as path:` is the explicit form of the same thing. It refuses folders and non-R2 remote URLs.
- **Changed:** files attached to *Repost Item Valuation* and *Bank Statement Import* always stay on the server disk, because ERPNext writes back into them by path. Writing into any other cached copy fails with `PermissionError` instead of silently changing a copy that is never uploaded.
- **Fixed:** when a request rolled back after uploading a new file to R2, the server copy was left on disk. It is now removed.
- **Changed:** Optimize on an R2 image shows a clear message. Before, it changed nothing but reported success. Private R2 images get no thumbnail (logged), because the thumbnail would be written to the public folder.
- **Not covered:** `frappe.utils.file_manager.get_file()` and `get_file_path()` don't go through `File`, so they still fail for R2 files. So does code that builds `private/files/<name>` paths itself. See the README.
- **Cache grace rule:** a copy used in the last hour is never removed for size, by the daily job or when a copy is added. The size limit is soft: if only recent copies are left over it, a warning is logged. Adding a copy runs one pass over the folder and removes least recently used older copies only when over the limit.
- **Fixed:** deleting an R2 file removes both its private and public cached copies (a File whose *Is Private* changed could leave one behind).
- **Security (RCA-3):** changing *Is Private* on an R2 file is refused for every user: "Change visibility by re-uploading; R2 files keep their visibility." The download endpoint takes private or public from the key's folder, not the File row, so a private object is never served to Guest through a row marked public.
- **Patch:** `move_write_back_files_to_local` moves R2 files already attached to *Repost Item Valuation* or *Bank Statement Import* back to the server disk. Idempotent. Each failure is written to the Error Log and the file stays in R2.
- **Patch:** `set_cache_defaults` gives sites that already saved their settings the two new defaults, then clears the cached settings.
- **Tests:** the suite can run on a site that already uses R2. It restores the site's settings and removes only its own records. Tests that migrate, retry or purge every file on the site run only in CI, or where `allow_site_wide_r2_tests` is set in `site_config.json`.

## 1.0.0

First release.
