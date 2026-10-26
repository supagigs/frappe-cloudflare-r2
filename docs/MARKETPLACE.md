# Frappe Cloud Marketplace listing

Copy these values into **Frappe Cloud → Dashboard → Marketplace → New App** (publisher: Supagigs).

| Field | Value |
|---|---|
| Repository | `https://github.com/supagigs/frappe-cloudflare-r2` |
| Branch(es) | `version-15` and `version-16` (both point to the same code; keep `main` in sync) |
| App name (slug) | `frappe-cloudflare-r2` |
| Title | Cloudflare R2 |
| Category | Integrations (secondary: Utilities) |
| Pricing | Free |
| Logo | `frappe_cloudflare_r2/public/images/logo.png` (512×512 PNG) |
| Website | https://supagigs.com |
| Support | support@supagigs.com |
| Documentation | https://github.com/supagigs/frappe-cloudflare-r2#readme |
| Privacy policy | https://supagigs.com/privacy *(publish this page before submitting)* |
| Terms of service | https://supagigs.com/terms *(publish this page before submitting)* |

## Short description (≤ 140 characters)

> Store Frappe & ERPNext attachments in Cloudflare R2: zero egress fees, CDN delivery, secure private files, one-click migration from S3.

## Long description

```markdown
**Move your Frappe / ERPNext attachments to Cloudflare R2, with zero egress fees.**

Every file your team uploads is stored in your own R2 bucket instead of the server disk. Public files are served from Cloudflare's CDN on your own domain. Private files stay private: users get them only after a permission check, through a link that expires in two minutes.

### Features
- Automatic upload of every new attachment (public and private)
- Public files on your custom domain (e.g. files.yourcompany.com) via Cloudflare CDN
- Private files: Frappe permission check + short-lived signed URL
- Large files: S3 multipart upload, no 300 MB limit
- 30-day recycle bin for deleted files (configurable)
- **One-click migration** from the server disk to R2
- **One-click migration from Frappe S3 Attachment / Amazon S3**: settings imported automatically, S3 copies kept 30 days, old S3 links keep working
- Updates every Attach / Attach Image field, Single settings (logos) and, optionally, links inside notes, emails and web pages
- Email attachments, Data Import and Prepared Reports work with files in R2
- One-click exit back to local storage
- Test Connection button that also checks your public domain and warns if private files are exposed
- Frappe v15 and v16

### Setup in 3 minutes
1. Create an R2 bucket and an API token (Object Read & Write) in Cloudflare.
2. Open **Cloudflare R2 Settings**, paste Account ID, bucket and keys, click **Test Connection**.
3. Tick **Enable**, then click **Migrate Local Files → R2** if you have existing files.

Made by Supagigs · MIT licensed · support@supagigs.com
```

## Screenshots to add (1280×800)

1. Cloudflare R2 Settings: connection section with a green *Test Connection* result
2. Migration in progress (progress bar + Migration Status)
3. R2 Migration Log list
4. A document with an attachment opened from R2

## Pre-submission checklist

- [ ] Code pushed to `main`; branches `version-15` and `version-16` created from it
- [ ] GitHub Actions CI is green on both versions
- [ ] Tagged release `v1.0.0`
- [ ] Privacy and terms pages live on supagigs.com
- [ ] Installed and tested on a Frappe Cloud staging site with a real R2 bucket
- [ ] Screenshots uploaded
