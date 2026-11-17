<p align="center">
  <img src="frappe_cloudflare_r2/public/images/logo.png" width="96" alt="Supagigs">
</p>

<h1 align="center">Cloudflare R2 for Frappe</h1>

<p align="center">
Store Frappe / ERPNext attachments in <a href="https://developers.cloudflare.com/r2/">Cloudflare R2</a>, with zero egress fees,
CDN delivery for public files, permission-checked private files, a 30-day recycle bin and one-click migration
from local disk or the <a href="https://cloud.frappe.io/marketplace/apps/frappe_s3_attachment">Frappe S3 Attachment</a> app.
</p>

<p align="center">Frappe v16 · MIT · by <a href="https://supagigs.com">Supagigs</a></p>

---

## Contents

- [What it does](#what-it-does)
- [Install](#install)
- [Set up Cloudflare R2](#1-set-up-cloudflare-r2)
- [Configure Frappe](#2-configure-frappe)
- Use cases
  - [A. New site, nothing to migrate](#use-case-a--new-site)
  - [B. Move files from the server disk to R2](#use-case-b--move-files-from-the-server-disk-to-r2)
  - [C. Switch from Frappe S3 Attachment (Amazon S3) to R2](#use-case-c--switch-from-frappe-s3-attachment-to-r2)
  - [D. Leave R2 / uninstall](#use-case-d--move-back-to-the-server--uninstall)
- [Deletion and retention](#deletion-and-retention)
- [Settings reference](#settings-reference)
- [How it works](#how-it-works)
- [Security notes](#security-notes)
- [Limitations](#limitations)
- [Development](#development)

## What it does

| | |
|---|---|
| **Uploads** | Every new attachment (public or private) is uploaded to R2 and removed from the server disk. Large files use the S3 multipart API, so there is no 300 MB limit. |
| **Public files** | Served directly by Cloudflare from your custom domain (e.g. `https://files.erp.example.com/...`). |
| **Private files** | Served through Frappe: the user's File permission is checked, then they are redirected to a signed URL that expires after 2 minutes (configurable). |
| **Deletion** | Deleting a File in Frappe moves the object to a `_trash/` prefix in R2. A daily job permanently deletes it after **30 days** (configurable, `0` = immediately). |
| **Local → R2** | One click. Background, batched, resumable. Server copies are deleted after upload. |
| **S3 → R2** | One click. Reads settings from Frappe S3 Attachment automatically (editable). S3 originals are kept **30 days** (configurable), then deleted. Old S3 links stored in Frappe are rewritten. |
| **References** | File records, every Attach / Attach Image field (all DocTypes, child tables, custom fields, Single settings like the website logo) and optionally links inside rich text are updated. |
| **Content access** | Code that reads file bytes (email attachments, Data Import, Prepared Reports) keeps working for files in R2. Code that opens files by their disk path keeps working too ([details](#for-app-developers-reading-r2-files)). |
| **Exit path** | One click moves everything back to the server disk. |

## Install

**Frappe Cloud:** add *Cloudflare R2* from the Marketplace to your bench group, then install it on the site.

**Self-hosted:**

```bash
bench get-app https://github.com/supagigs/frappe-cloudflare-r2
bench --site your.site install-app frappe_cloudflare_r2
bench --site your.site migrate
```

Then open **Cloudflare R2 Settings** (search bar, or the app icon on `/apps`).

## 1. Set up Cloudflare R2

1. Cloudflare dashboard → **R2 Object Storage** → **Create bucket**, e.g. `erp-files`.
   Optional but recommended if you will use a public domain: a second bucket for public files, e.g. `erp-public`.
2. Copy the **Account ID** from *Account Details* on the R2 overview page.
3. **Manage API Tokens → Create Account API Token**
   - Permissions: **Object Read & Write**
   - Specify bucket(s): only the bucket(s) above
   - Copy the **Access Key ID** and **Secret Access Key** (shown once).
4. Public files (recommended): bucket (the public one, if you made two) → **Settings → Custom Domains → Connect Domain**, e.g. `files.erp.example.com`. The domain must be on Cloudflare DNS.

> Using `r2.dev` public URLs also works for testing, but Cloudflare rate-limits them and does not recommend them for production.

## 2. Configure Frappe

Open **Cloudflare R2 Settings**:

1. **Account ID**, **Bucket Name**, **Access Key ID**, **Secret Access Key**.
2. **Public Base URL**: `https://files.erp.example.com` (leave empty to serve public files through Frappe as well).
   **Public Bucket**: the second bucket, if you created one.
3. **Folder (key prefix)**: optional, e.g. your site name when several sites share one bucket.
4. Click **Test Connection**. It uploads, reads, signs and deletes a probe object and checks the public domain. It also warns you if private files would be reachable through the public domain.
5. Tick **Enable Cloudflare R2** and **Save**.

## Use case A: new site

Nothing else to do. Every file uploaded from now on goes to R2.

## Use case B: move files from the server disk to R2

1. Complete *Configure Frappe* and enable R2.
2. Optional: tick **Also Rewrite Links Inside Text Fields** if images were pasted into notes, emails, web pages or item descriptions.
3. Take a backup: `bench --site your.site backup --with-files`.
4. Click **Migrate Local Files → R2** and confirm.
5. Watch progress in the form or open **Migration Log** for per-file results (`Migrated`, `Skipped` such as a file missing on disk or an ignored DocType, and `Failed` with the error).
6. Re-run the button at any time. Already-migrated files are no longer local, so they are skipped automatically.

Files attached to DocTypes listed in **Keep Local for DocTypes** (default `Data Import`) stay on the server, the same as in Frappe S3 Attachment.

## Use case C: switch from Frappe S3 Attachment to R2

1. Install this app alongside `frappe_s3_attachment`.
2. Configure R2 (above). Don't uninstall the S3 app yet.
3. Open the **Migrate from Amazon S3** section. Bucket, region and access key are pre-filled from *S3 File Attachment*. Click **Load From S3 Attachment App** to copy the secret too, or type different credentials (yours always win).
4. Tick **Enable Cloudflare R2**, then **Save**.
5. Click **Migrate S3 Files → R2**. Each object is streamed from S3 to R2 (multipart for large files), File records and all Attach fields are re-pointed, and every file gets a row in the *Migration Log*.
6. When it finishes: `bench --site your.site uninstall-app frappe_s3_attachment`.

**Links are updated.** Old S3 links stored inside Frappe (attachment fields, notes, comments, communications, web pages) are rewritten to the R2 copies automatically. Links in emails already sent outside Frappe point to the S3 app and stop working once it is uninstalled.

**S3 retention.** S3 originals are kept for **Keep S3 Originals For (days)** (default 30) as a rollback window, then deleted by a daily job. Set `0` to never delete them. Objects that are still referenced by a File record are never deleted. The S3 key needs `s3:DeleteObject` for this; otherwise the deletion is logged and skipped.

## Use case D: move back to the server / uninstall

1. Untick **Enable Cloudflare R2** and **Save** (new uploads stay local).
2. Click **Move R2 Files Back to Server**. Files are downloaded, references re-pointed, and R2 objects go to the trash (purged after the retention period).
3. Uninstall: `bench --site your.site uninstall-app frappe_cloudflare_r2`.

> Uninstalling without step 2 leaves File records pointing at an endpoint that no longer exists.

## Deletion and retention

| Setting | Default | Effect |
|---|---|---|
| Delete From R2 When File Is Deleted | ✔ | Off = objects are never removed from R2. |
| Keep Deleted Files For (days) | 30 | Deleted objects are copied to `<folder>/_trash/<timestamp>/<key>` and purged daily after N days. `0` = delete immediately. |
| Keep S3 Originals For (days) | 30 | After S3 → R2 migration. `0` = keep forever. |
| Delete Server Copy After Upload | ✔ | Local copy removed after the upload commits. |
| Keep File Log For (days) | 180 | Retention of the R2 File Log. `0` = keep forever. |

To restore a deleted file within the window, copy it back from the `_trash/` prefix in the Cloudflare dashboard (or with any S3 tool) to its original key.

## File Log

**Cloudflare R2 Settings → Logs → File Log** (DocType *R2 File Log*) records every file movement, with user, size, the attached document and the exact locations (`/private/files/…` → `r2://bucket/key`):

| Event | Meaning |
|---|---|
| Uploaded | Moved from the server disk to R2 |
| Skipped | Not uploaded: R2 disabled, DocType kept local, or file missing on disk (reason in *Error*) |
| Upload Failed | R2 rejected the upload; the user saw an error and nothing was saved |
| Rolled Back | Upload worked but the request failed later, so the R2 object was removed again |
| Moved to Trash / Deleted | File deleted in Frappe (trash location shown) |
| Kept | Deleted in Frappe but the object is still used by another File, or deletion from R2 is off |
| Delete Failed | R2 deletion failed (see *Error*) |
| Purged | Trash older than the retention period permanently deleted |

Rows older than **Keep File Log For (days)** (default 180) are removed daily. Bulk migrations are recorded in the separate *R2 Migration Log*.

## Settings reference

| Field | Notes |
|---|---|
| Account ID / Jurisdiction | Builds `https://<account>.r2.cloudflarestorage.com` (`.eu.` / `.fedramp.` for jurisdictional buckets). |
| S3 API Endpoint (override) | Use the full S3 API URL instead. |
| Bucket Name | Private files, and public files unless a Public Bucket is set. |
| Public Base URL | Custom domain for public files. |
| Public Bucket | Separate bucket served by the public domain. |
| Signed URL Expiry | Seconds a private download link is valid (default 120, max 7 days). |
| Folder | Key prefix. |
| Keep Local for DocTypes | One per line. `site_config.json` key `ignore_r2_upload_for_doctype` (list) is also honoured. Repost Item Valuation and Bank Statement Import are always kept local. |
| Server Cache | Private on-disk copies of R2 files for code that opens them by path. Unused copies are removed after N days (default 7), and the total is kept under a limit (default 2048 MB). `0` turns a limit off. The size limit is soft: a copy used in the last hour is never removed for size, so the folder can stay over the limit for a while (logged as a warning). |
| Multipart Threshold / Part Size / Parallel Parts | Upload tuning for large files. |

Object keys look like `<folder>/<public|private>/<yyyy>/<mm>/<dd>/<DocType>/<random>_<file-name>`. Private keys contain 128 random bits.

Custom key layout: in another app's `hooks.py`, set `r2_key_generator = "my_app.storage.make_key"` with the signature `make_key(file_name, is_private, parent_doctype, parent_name) -> str | None`.

## How it works

```
Upload  ─► Frappe writes the file ─► File.after_insert ─► upload to R2 ─► file_url = R2 URL
                                                                   └─► local copy removed after commit

Public file   ─► https://files.example.com/<key>                       (Cloudflare CDN)
Private file  ─► /api/method/frappe_cloudflare_r2.controller.download_file?key=<key>
                  └─► permission check ─► 302 to a signed URL (default 120 s)

Delete  ─► File.on_trash ─► after commit: copy to _trash/, delete original ─► daily purge after N days
                        └─► after commit: remove the server's cached copy

Path    ─► File.get_full_path() ─► private/r2_cache/<hash><ext> (read-only, downloaded on first use)
```

## Security notes

- Credentials are stored in Frappe Password fields (encrypted) and never sent to the browser.
- A private file is served only to users who can read its File record (Frappe's own rules: owner, shared, or read access to the attached document). Guests get HTTP 403.
- **R2 has no per-object ACLs.** A bucket connected to a public domain exposes *every* object in it. Use a separate **Public Bucket**, or add a Cloudflare WAF custom rule on the domain that blocks requests whose path doesn't start with `/<folder>/public/`. *Test Connection* warns you if private files are reachable.
- Settings, migrations and the log are restricted to **System Manager**.

## Limitations

- Private images in **PDFs**: wkhtmltopdf fetches images without the user's session, so private images in print formats won't render. Public images are fine. Frappe S3 Attachment has the same limitation.
- Links inside rich text are only rewritten when the option is ticked, and only where they are quoted (`"url"`, `'url'`, `(url)`).
- While Frappe S3 Attachment is still installed, its own upload hook also runs. Migrate, then uninstall it.
- Another app that also overrides the `File` DocType class may conflict with this one.

## For app developers: reading R2 files

The app is a drop-in for Frappe's local storage. Code written for files on the server disk keeps working when they are in R2:

```python
with open(file_doc.get_full_path(), "rb") as fh:   # works for local and R2 files
    data = fh.read()

content = file_doc.get_raw_content()   # exact bytes, without touching the disk cache

with file_doc.local_copy() as path:    # explicit form of get_full_path(); refuses folders and non-R2 URLs
    image = Image.open(path)
```

- **Paths come from a private cache.**
  - For an R2 file, `get_full_path()` returns a private cached copy in `sites/<site>/private/r2_cache/`, downloaded on first use and then reused. An R2 key never gets new bytes, so a copy can't go stale.
  - The folder is 0700 and each copy is read-only (0400).
  - A copy is removed when its last File is deleted. A daily job trims the cache (*Server Cache* settings: unused for 7 days, 2048 MB in total).
  - **Grace rule:** a copy used in the last hour is never removed for size, because a caller may still be reading it. When a new copy is added, one pass over the folder checks the total; only if it is over the limit are the least recently used copies older than an hour removed.
  - To empty the cache: `bench --site <site> execute frappe_cloudflare_r2.cache.clear`.
- **Writing into the path fails.** Writing into `get_full_path()` of an R2 file raises `PermissionError`, because the change would never reach R2. Two ERPNext doctypes write back into their attachment by path, so their files always stay on the server disk: *Repost Item Valuation* and *Bank Statement Import*. If your app does the same, add its DocType to *Keep Local for DocTypes*.
- **Permissions:** neither `get_raw_content()` nor `local_copy()` checks them. Check `file_doc.has_permission("read")` when the request comes from a user.
- **Legacy helpers:** `frappe.utils.file_manager.get_file_path()` and `get_file()` (and `pdf_to_base64()` through them) return the same private cached copy for an R2 file, through `compat.py` on Frappe 16. They don't check permissions, for R2 files just as for local files: callers must check `file_doc.has_permission("read")` themselves. Code that builds `private/files/<file_name>` itself (for example with `get_files_path()`) can't find R2 files. Use `frappe.get_doc("File", name).get_full_path()` or `.get_raw_content()`.
- **R2 files keep their visibility.** Changing *Is Private* on a File stored in R2 is refused for every user, Administrator included: "Change visibility by re-uploading; R2 files keep their visibility." The object stays in the bucket it was uploaded to. Downloads take private or public from the key's own folder (`/private/` or `/public/`), not from the File row.
- **Write-back files already in R2:** the `move_write_back_files_to_local` patch moves R2 files attached to *Repost Item Valuation* or *Bank Statement Import* back to the server disk on `bench migrate`. A file that can't be moved stays in R2 and gets an Error Log entry titled "Cloudflare R2: write-back file NOT moved back to the server". Run it again after fixing the cause: `bench --site <site> execute frappe_cloudflare_r2.patches.move_write_back_files_to_local.execute`.
- **In the File form:** Unzip works for R2 zip files. Optimize says it isn't available for R2 images. Private R2 images get no thumbnail, because Frappe would write it to the public folder.

## Development

```bash
bench get-app /path/to/frappe-cloudflare-r2
bench --site test.local install-app frappe_cloudflare_r2
./env/bin/pip install "moto[server]"
bench --site test.local set-config allow_tests true
bench --site test.local run-tests --app frappe_cloudflare_r2
```

The tests run against a local S3-compatible server (moto), covering uploads, multipart uploads, signed downloads, permissions, trash and purge, reading R2 files by path through the cache, and all three migrations including S3 retention and old S3 links.

The suite saves the site's Cloudflare R2 Settings and restores them afterwards, and removes only the records it creates. The migration, deferred-retry and log-purge tests act on every file on the site, so they run only in CI (`CI` set) or on a disposable site with `bench --site test.local set-config allow_site_wide_r2_tests 1`.

## Credits & License

MIT © 2026 Supagigs. Inspired by [Frappe S3 Attachment](https://github.com/zerodha/frappe-attachments-s3) by Zerodha (MIT).

Support: **support@supagigs.com** · Issues: [GitHub](https://github.com/supagigs/frappe-cloudflare-r2/issues)

### Trademarks

Cloudflare and Cloudflare R2 are trademarks of Cloudflare, Inc. This app is an independent integration by Supagigs and is not affiliated with, sponsored by or endorsed by Cloudflare, Inc. Frappe and ERPNext are trademarks of Frappe Technologies Pvt. Ltd.
