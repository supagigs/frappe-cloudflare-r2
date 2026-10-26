<p align="center">
  <img src="frappe_cloudflare_r2/public/images/logo.png" width="96" alt="Supagigs">
</p>

<h1 align="center">Cloudflare R2 for Frappe</h1>

<p align="center">
Store Frappe / ERPNext attachments in <a href="https://developers.cloudflare.com/r2/">Cloudflare R2</a>, with zero egress fees,
CDN delivery for public files, permission-checked private files, a 30-day recycle bin and one-click migration
from local disk or the <a href="https://cloud.frappe.io/marketplace/apps/frappe_s3_attachment">Frappe S3 Attachment</a> app.
</p>

<p align="center">Frappe v15 · v16 · MIT · by <a href="https://supagigs.com">Supagigs</a></p>

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
| **S3 → R2** | One click. Reads settings from Frappe S3 Attachment automatically (editable). S3 originals are kept **30 days** (configurable), then deleted. Old S3 links keep working. |
| **References** | File records, every Attach / Attach Image field (all DocTypes, child tables, custom fields, Single settings like the website logo) and optionally links inside rich text are updated. |
| **Content access** | Code that reads file bytes (email attachments, Data Import, Prepared Reports) keeps working for files in R2. |
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

**Old links keep working.** URLs like `/api/method/frappe_s3_attachment.controller.generate_file?key=...` that are still embedded in sent emails, comments or print formats are answered by this app and redirected to the R2 copy, with permissions checked.

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

To restore a deleted file within the window, copy it back from the `_trash/` prefix in the Cloudflare dashboard (or with any S3 tool) to its original key.

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
| Keep Local for DocTypes | One per line. `site_config.json` key `ignore_r2_upload_for_doctype` (list) is also honoured. |
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

## Development

```bash
bench get-app /path/to/frappe-cloudflare-r2
bench --site test.local install-app frappe_cloudflare_r2
./env/bin/pip install "moto[server]"
bench --site test.local set-config allow_tests true
bench --site test.local run-tests --app frappe_cloudflare_r2
```

The tests run against a local S3-compatible server (moto), covering uploads, multipart uploads, signed downloads, permissions, trash and purge, and all three migrations including S3 retention and old S3 links.

## Credits & License

MIT © 2026 Supagigs. Inspired by [Frappe S3 Attachment](https://github.com/zerodha/frappe-attachments-s3) by Zerodha (MIT).

Support: **support@supagigs.com** · Issues: [GitHub](https://github.com/supagigs/frappe-cloudflare-r2/issues)
