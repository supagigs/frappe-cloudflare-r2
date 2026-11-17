# Changelog

## 1.0.1 (17 Nov 2026)

Code that reads R2 files through a disk path now works or fails clearly (RCA-2 in `docs/RCA.md`).

- **New:** `with file_doc.local_copy() as path:` gives a library that needs a real path (PIL, PDF tools, zipfile) a private temporary copy of an R2 file. The copy is deleted afterwards, even on error. For a local file it gives the file's own path.
- **Changed:** `File.get_full_path()` on a file stored in R2 raises `R2FileNotOnDiskError` (a `frappe.ValidationError`) and names the supported alternatives. Before, it returned the download URL, and opening that failed with `[Errno 2] No such file or directory: '/api/method/…'`.
- **Fixed:** when a request rolled back after uploading a new file to R2, the server copy was left on disk. It is now removed.
- **Fixed:** Unzip on a zip file stored in R2 works. It used to fail with `[Errno 2]`.
- **Changed:** Optimize on an image stored in R2 shows a clear message. Before, it changed nothing but reported success. Private R2 images get no thumbnail (logged), because the thumbnail would be written to the public folder.
- **Tests:** the suite can run on a site that already uses R2. It restores the site's Cloudflare R2 Settings and removes only its own records. Tests that migrate, retry or purge every file on the site run only in CI, or where `allow_site_wide_r2_tests` is set in `site_config.json`.

## 1.0.0

First release.
