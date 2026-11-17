# Plan: Stop private R2 files from being served to guests

Status: **DRAFT v1. Parked:** the user will pick this up after the Timely screenshot-to-R2 work is finished. Do not implement yet.
Branch: `fix/private-file-exposure` (created from `pipeline-support` on 2026-11-16).
Router: planning is Rule 1, with Fable advisor reviews before and after (both still to do). Building it is **Rule 4**, because it covers file access permissions and storage.
Found by: Timely plan step 1 (backend-engineer), 2026-11-16. The Lead checked it in the code.

---

## 1. Problem (verified)

`download_file` (`frappe_cloudflare_r2/controller.py:353-380`) looks up **every** File row whose `file_url` is the private download URL for a key. It then serves the object if **any** of those rows is public (line 367), with no permission check and to guests too:

```python
allowed = next((r for r in rows if not r.is_private), None)   # any public row wins
```

So a private object becomes readable by anyone in two ways:

1. **Public copy.** A user who can read a private R2 File creates a *new* File with the same `file_url` (`/api/method/frappe_cloudflare_r2.controller.download_file?key=…`) and `is_private=0`. Frappe allows this, because the user owns the new row. From then on, `download_file` serves the object to anyone, guests included.
2. **Flipping `is_private`.** A user with write on a private R2 File, for example its owner, sets `is_private=0`. The object stays under the private key, and the row now counts as public.

Whether the presigned URL then works depends on the bucket. `presigned_url` signs against `bucket_for(allowed.is_private)` (`storage.py:202-203`), which is the **public** bucket for a public row.
- `public_bucket` falls back to the private bucket when "Public Bucket Name" is empty (`storage.py:131`). **With one bucket (the default), the exposure is real.**
- With separate buckets, the signed URL points at a key that doesn't exist in the public bucket and returns 404. That is luck, not protection.

**Who is affected:** every private file stored on R2, on every site with this app, for example Yardi data-pipeline files and HR documents. Timely screenshots are already protected by Timely's own `guard_file` hook (Timely plan step 1), but that only covers Timely's own files.

## 2. Goal

A private R2 object is served only to a logged-in user who has read permission on a **private** File row for that key. No File row a user can create or edit can widen access to an object they don't own.

## 3. Proposed fix

1. **`download_file` decides from the key, not from the most permissive row.**
   - The key's own folder (`/private/` versus `/public/`, from `make_key`, `storage.py:170-190`) decides whether the object is private.
   - **Private key:**
     - Ignore every public row.
     - Serve only if the user has File read on at least one *private* row.
     - Guests always get 403.
   - **Public key:** keep today's behaviour.
   - Sign against `bucket_for(key_is_private)`, not against the row's flag.
2. **Block public rows that point at private keys.** In `R2FileMixin.validate`, which runs on insert and update, reject a File whose `file_url` is an R2 private download URL unless `is_private=1`. This closes way 1 at the source and keeps the data clean.
3. **Block `is_private` flips on R2-stored files.**
   - Reject changing `is_private` on a File that is already on R2.
   - A legitimate change of visibility needs a re-upload, or a System Manager action that moves the object to the right key and bucket and updates `file_url`. That second option is open question O2.
4. **Audit (report only).** A whitelisted, System Manager only method lists:
   - keys that have both public and private rows
   - public rows that point at private keys
   - private keys that have rows attached to more than one parent document

   Run it on every site before and after the fix, and decide case by case. Nothing is deleted automatically.
5. **Stopgap until this ships:** turn on the System Setting "Only Allow System Managers to Upload Public Files". It narrows way 1 to System Managers. It doesn't close way 2.

### Related performance follow-up (from the Timely code review, 2026-11-16)

- **`File.file_url` index:** the index only covers the first 100 characters of `file_url`. For R2 private URLs those characters are the same for every file uploaded on the same day (up to `…/timely-scree`). So `download_file` and `delete_from_r2` scan every one of that day's Files on each lookup:
  - about 20,000 rows a day at 1,000 users
  - about 100,000 rows a day at 5,000 users
- **Fix:** look rows up by a dedicated, indexed `r2_key` column or a hash of the key, not by `file_url`.
- This goes with the R2 File Log index and boto3 client reuse already listed in the Timely plan §5.

## 4. Open questions

- **O1:** Frappe itself copies File rows (`file_url` included) in some flows, such as copying attachments to an amended or derived document.
  - Those copies are private when the original is, so fix 2 allows them. But each copy lets a different audience read the object: whoever can read the new parent.
  - Is that acceptable? It matches Frappe's local-file behaviour. Or should private copies need read access on an existing row at insert time?
- **O2:** Do we need a supported "make this R2 file public" action, or is "re-upload as public" enough?
- **O3:** Should the audit run as a one-off patch, or as a weekly report?
- **O4:** Which branches get the fix (`pipeline-support`, `main`, `version-16`), and in what order?

## 5. Validation (staging only)

- **Way 1:** a user who can read a private R2 file inserts a public File with the same URL through `/api/resource/File`. The insert is rejected. If a row is inserted directly in the database, `download_file` still returns 403 to a guest and to an unrelated user.
- **Way 2:** the owner flips `is_private` on an R2 file. Rejected.
- Private file: a user with read access gets a 302 to a presigned GET that expires; a guest and a user without access get 403.
- Public file: unchanged behaviour.
- One bucket, and separate buckets: both covered.
- The audit lists seeded bad rows and changes nothing.
- **Regression:** the existing R2 tests, migration, trash, U1 rollback, `r2_key_generator` sites (Yardi) unchanged, and Timely's `guard_file` still works.

## 6. Steps (after approval)

| # | Step | Owner | Router |
|---|---|---|---|
| 1 | Domain input: affected flows, O1 to O4 | r2-sme | opus / high |
| 2 | Plan review | fable-advisor (before) | — |
| 3 | Fixes 1 to 3, plus tests | backend-engineer | Rule 4: opus / high |
| 4 | Audit method (fix 4) | backend-engineer | Rule 2: sonnet / medium |
| 5 | Tests on staging | tester | Rule 7: sonnet / low |
| 6 | Code review | frappe-expert-consultant | sonnet / high (opus-written code) |
| 7 | Security audit | frappe-expert-consultant + fable-advisor | Rule 9: opus / high |
| 8 | Rollout plan: stopgap setting, audit run, release per branch | devops-engineer | needs user approval |

## Advisor consultations

_None yet._
