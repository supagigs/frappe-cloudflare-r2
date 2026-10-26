# Copyright (c) 2026, Supagigs and contributors
# For license information, please see license.txt
"""
One-click migrations, run as resumable background jobs:

* local_to_r2  - files on the server disk  -> R2   (local copy deleted after commit)
* s3_to_r2     - Frappe S3 Attachment files -> R2   (S3 copy kept N days, then deleted)
* r2_to_local  - R2 -> server disk (exit path / uninstall)

Files are processed by distinct file_url in batches. After each file is copied every
reference is re-pointed: File rows, Attach / Attach Image fields on every DocType
(including child tables and Single DocTypes) and, optionally, links inside rich text.
"""

import os
import re

import frappe
from frappe import _
from frappe.utils import add_days, cint, now_datetime

from frappe_cloudflare_r2.controller import ignored_doctypes, local_path
from frappe_cloudflare_r2.storage import (
	DOWNLOAD_PREFIX,
	LEGACY_S3_METHOD,
	R2,
	S3Source,
	get_settings,
	guess_content_type,
	is_local_url,
	key_from_url,
	legacy_s3_key,
	slug,
)

BATCH_SIZE = 100
LOCK_KEY = "frappe_cloudflare_r2:migration_lock"
LOG = "R2 Migration Log"
DIRECTIONS = {
	"local_to_r2": "Local disk → R2",
	"s3_to_r2": "Amazon S3 → R2",
	"r2_to_local": "R2 → Local disk",
}
TEXT_FIELDTYPES = ("Text Editor", "HTML Editor", "Markdown Editor", "Long Text", "Text")
SKIP_TEXT_DOCTYPES = {
	"Version",
	"Error Log",
	"Deleted Document",
	LOG,
	"Email Queue",
	"Scheduled Job Log",
	"Access Log",
	"Activity Log",
	"Route History",
	"Prepared Report",
	"Data Import",
}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


@frappe.whitelist()
def start_migration(direction):
	frappe.only_for("System Manager")
	if direction not in DIRECTIONS:
		frappe.throw(_("Unknown migration"))

	settings = frappe.get_single("Cloudflare R2 Settings")
	if direction in ("local_to_r2", "s3_to_r2") and not settings.enabled:
		frappe.throw(_("Enable Cloudflare R2 and save before migrating files to it."))
	if direction == "r2_to_local" and settings.enabled:
		frappe.throw(_("Disable Cloudflare R2 and save first, so new uploads stay on the server."))
	if direction == "s3_to_r2" and not settings.s3_bucket_name:
		frappe.throw(_("Fill in the Amazon S3 bucket details first."))
	R2(settings)  # validates configuration
	if frappe.cache.get_value(LOCK_KEY):
		frappe.throw(_("A migration is already running. Please wait for it to finish."))

	run_id = f"{direction}-{now_datetime().strftime('%Y%m%d%H%M%S')}"
	total = len(_candidates(direction, settings, after="", limit=None))
	state = {
		"run_id": run_id,
		"direction": direction,
		"total": total,
		"done": 0,
		"migrated": 0,
		"skipped": 0,
		"failed": 0,
	}
	frappe.cache.set_value(LOCK_KEY, run_id, expires_in_sec=6 * 3600)
	_set_status(state, "Queued")
	frappe.enqueue(
		"frappe_cloudflare_r2.migration.run_batch",
		queue="long",
		timeout=3600,
		state=state,
		after="",
		batch_no=1,
		enqueue_after_commit=True,
	)
	return state


def run_batch(state, after, batch_no):
	settings = get_settings()
	direction = state["direction"]
	try:
		candidates = _candidates(direction, settings, after=after, limit=BATCH_SIZE)
		r2 = R2(settings)
		s3 = S3Source(settings) if direction == "s3_to_r2" else None
		for row in candidates:
			result = _migrate_one(direction, row, settings, r2, s3, state["run_id"], batch_no)
			state[result] += 1
			state["done"] += 1
			frappe.db.commit()
			if result == "migrated" and direction == "local_to_r2" and settings.delete_local_after_upload:
				_remove(local_path(row.file_url))

		_rewrite_references(state["run_id"], batch_no, rewrite_text=cint(settings.rewrite_text_links))
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.cache.delete_value(LOCK_KEY)
		frappe.log_error(title=f"Cloudflare R2 migration {state['run_id']} stopped")
		_set_status(state, "Stopped with an error (see Error Log). Run it again to resume")
		frappe.db.commit()
		return

	if len(candidates) == BATCH_SIZE:
		_set_status(state, "Running")
		frappe.db.commit()
		frappe.enqueue(
			"frappe_cloudflare_r2.migration.run_batch",
			queue="long",
			timeout=3600,
			state=state,
			after=candidates[-1].file_url,
			batch_no=batch_no + 1,
		)
	else:
		frappe.cache.delete_value(LOCK_KEY)
		_set_status(state, "Completed")
		frappe.db.commit()


def _set_status(state, label):
	text = (
		f"{DIRECTIONS[state['direction']]}: {label}\n"
		f"Processed {state['done']} of {state['total']} · migrated {state['migrated']} · "
		f"skipped {state['skipped']} · failed {state['failed']}\n"
		f"Run: {state['run_id']} · {now_datetime().strftime('%Y-%m-%d %H:%M:%S')}"
	)
	frappe.db.set_single_value("Cloudflare R2 Settings", "migration_status", text, update_modified=False)
	frappe.publish_realtime(
		"r2_migration_progress", {**state, "label": label, "text": text}, user=frappe.session.user
	)


# ---------------------------------------------------------------------------
# Candidate selection
# ---------------------------------------------------------------------------


def _candidates(direction, settings, after, limit):
	"""Distinct file URLs to migrate, ordered by URL so batches can resume with a cursor."""
	if direction == "local_to_r2":
		where = "(file_url like '/files/%%' or file_url like '/private/files/%%')"
	elif direction == "s3_to_r2":
		where = f"(file_url like '/api/method/{LEGACY_S3_METHOD}%%' or file_url like 'http%%')"
	else:
		base = (settings.public_base_url or "").rstrip("/")
		where = "(file_url like %(dl)s" + (" or file_url like %(base)s)" if base else ")")

	params = {
		"after": after or "",
		"dl": DOWNLOAD_PREFIX + "%",
		"base": (settings.public_base_url or "").rstrip("/") + "/%",
	}
	rows = frappe.db.sql(
		f"""
		select file_url, max(is_private) as is_private, min(file_name) as file_name,
			min(attached_to_doctype) as attached_to_doctype, min(name) as file
		from `tabFile`
		where is_folder=0 and {where} and file_url > %(after)s
		group by file_url
		order by file_url
		{f"limit {int(limit)}" if limit else ""}
		""",
		params,
		as_dict=True,
	)
	ignored = ignored_doctypes(settings) if direction == "local_to_r2" else set()
	for r in rows:
		if direction == "s3_to_r2":
			r.s3_key = legacy_s3_key(r.file_url, settings)
			r.ignored = not r.s3_key
		else:
			r.ignored = (r.attached_to_doctype or "File") in ignored
	if limit is None:
		# counting only; ignored rows are still returned in batches so the cursor advances
		rows = [r for r in rows if not r.ignored]
	return rows


# ---------------------------------------------------------------------------
# Per-file work
# ---------------------------------------------------------------------------


def _migrate_one(direction, row, settings, r2, s3, run_id, batch_no):
	if row.get("ignored"):
		return "skipped"
	log = {
		"doctype": LOG,
		"run_id": run_id,
		"batch_no": batch_no,
		"direction": DIRECTIONS[direction],
		"file": row.file,
		"file_name": row.file_name,
		"old_url": row.file_url,
		"is_private": cint(row.is_private),
	}
	try:
		if direction == "local_to_r2":
			new_url, extra = _local_to_r2(row, r2)
		elif direction == "s3_to_r2":
			new_url, extra = _s3_to_r2(row, r2, s3)
		else:
			new_url, extra = _r2_to_local(row, r2, settings)
	except _Skip as e:
		frappe.get_doc({**log, "status": "Skipped", "error": str(e)}).insert(ignore_permissions=True)
		return "skipped"
	except Exception as e:
		frappe.db.rollback()
		frappe.get_doc(
			{
				**log,
				"status": "Failed",
				"error": frappe.utils.cstr(e)[:1000] or frappe.get_traceback()[-1000:],
			}
		).insert(ignore_permissions=True)
		return "failed"

	frappe.db.sql(
		"update `tabFile` set file_url=%s where file_url=%s and is_folder=0", (new_url, row.file_url)
	)
	frappe.get_doc(
		{**log, **extra, "new_url": new_url, "status": "Migrated", "migrated_on": now_datetime()}
	).insert(ignore_permissions=True)
	return "migrated"


class _Skip(Exception):
	pass


def _local_to_r2(row, r2):
	path = local_path(row.file_url)
	if not os.path.isfile(path):
		raise _Skip("File is missing on disk")
	key = r2.make_key(row.file_name, cint(row.is_private), row.attached_to_doctype)
	r2.upload_file(path, key, cint(row.is_private), row.file_name)
	return r2.url_for(key, cint(row.is_private)), {"source": "Local", "r2_key": key}


def _s3_to_r2(row, r2, s3):
	from botocore.exceptions import ClientError

	is_private = cint(row.is_private) or row.file_url.startswith("/api/method/")
	try:
		obj = s3.open(row.s3_key)
	except ClientError as e:
		if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
			raise _Skip(f"Object {row.s3_key} not found in S3") from e
		raise
	key = "/".join(p for p in (r2.folder, "private" if is_private else "public", row.s3_key) if p)
	r2.upload_fileobj(
		obj["Body"],
		key,
		is_private,
		row.file_name,
		obj.get("ContentType") or guess_content_type(row.file_name),
	)
	if is_private and not cint(row.is_private):
		frappe.db.sql("update `tabFile` set is_private=1 where file_url=%s", row.file_url)
	return r2.url_for(key, is_private), {
		"source": "Amazon S3",
		"source_bucket": s3.bucket,
		"source_key": row.s3_key,
		"r2_key": key,
	}


def _r2_to_local(row, r2, settings):
	key = key_from_url(row.file_url, settings)
	if not key:
		raise _Skip("Not an R2 URL")
	is_private = cint(row.is_private)
	folder = frappe.get_site_path("private" if is_private else "public", "files")
	os.makedirs(folder, exist_ok=True)
	name = slug(row.file_name, "file")
	base, ext = os.path.splitext(name)
	n = 0
	while os.path.exists(os.path.join(folder, name)):
		n += 1
		name = f"{base}-{n}{ext}"
	path = os.path.join(folder, name)
	r2.download_to(key, path, is_private)
	frappe.db.after_commit.add(lambda: _try(r2.delete, key, is_private))
	return ("/private/files/" if is_private else "/files/") + name, {"source": "Cloudflare R2", "r2_key": key}


def _remove(path):
	try:
		os.remove(path)
	except FileNotFoundError:
		pass


def _try(fn, *args):
	try:
		fn(*args)
	except Exception:
		frappe.log_error(title="Cloudflare R2: cleanup failed")


# ---------------------------------------------------------------------------
# Re-pointing references
# ---------------------------------------------------------------------------


def _rewrite_references(run_id, batch_no, rewrite_text=False):
	"""Replace old URLs with new ones everywhere they are stored, one statement per field."""
	filters = {"run_id": run_id, "batch_no": batch_no, "status": "Migrated"}
	if not frappe.db.exists(LOG, filters):
		return
	if frappe.db.db_type == "postgres":
		_rewrite_references_row_by_row(filters)
	else:
		for doctype, field in _attach_fields():
			try:
				frappe.db.sql(
					f"""update `tab{doctype}` t join `tab{LOG}` l on t.`{field}`=l.old_url
					set t.`{field}`=l.new_url
					where l.run_id=%(run)s and l.batch_no=%(b)s and l.status='Migrated'""",
					{"run": run_id, "b": batch_no},
				)
			except Exception:
				frappe.log_error(title=f"Cloudflare R2: could not update {doctype}.{field}")

		# Single DocTypes (e.g. Website Settings logo) live in tabSingles
		frappe.db.sql(
			f"""update `tabSingles` s join `tab{LOG}` l on s.value=l.old_url
			set s.value=l.new_url
			where l.run_id=%(run)s and l.batch_no=%(b)s and l.status='Migrated'
			and s.doctype!='Cloudflare R2 Settings'""",
			{"run": run_id, "b": batch_no},
		)

	if rewrite_text:
		mappings = frappe.get_all(LOG, filters=filters, fields=["old_url", "new_url"])
		_rewrite_text_links(mappings)


def _rewrite_references_row_by_row(filters):
	mappings = frappe.get_all(LOG, filters=filters, fields=["old_url", "new_url"])
	fields = _attach_fields()
	for m in mappings:
		for doctype, field in fields:
			frappe.db.sql(
				f'update "tab{doctype}" set "{field}"=%s where "{field}"=%s', (m.new_url, m.old_url)
			)
		frappe.db.sql(
			"""update "tabSingles" set value=%s where value=%s and doctype!='Cloudflare R2 Settings'""",
			(m.new_url, m.old_url),
		)


def _attach_fields():
	rows = frappe.db.sql(
		"""
		select df.parent, df.fieldname from `tabDocField` df
		join `tabDocType` dt on dt.name=df.parent
		where df.fieldtype in ('Attach', 'Attach Image') and dt.issingle=0 and ifnull(dt.is_virtual,0)=0
		union
		select cf.dt, cf.fieldname from `tabCustom Field` cf
		join `tabDocType` dt on dt.name=cf.dt
		where cf.fieldtype in ('Attach', 'Attach Image') and dt.issingle=0 and ifnull(dt.is_virtual,0)=0
		"""
	)
	return [(dt, f) for dt, f in rows if frappe.db.table_exists(dt)]


def _text_fields():
	ft = ", ".join(f"'{t}'" for t in TEXT_FIELDTYPES)
	rows = frappe.db.sql(
		f"""
		select df.parent, df.fieldname from `tabDocField` df join `tabDocType` dt on dt.name=df.parent
		where df.fieldtype in ({ft}) and dt.issingle=0 and ifnull(dt.is_virtual,0)=0
		union
		select cf.dt, cf.fieldname from `tabCustom Field` cf join `tabDocType` dt on dt.name=cf.dt
		where cf.fieldtype in ({ft}) and dt.issingle=0 and ifnull(dt.is_virtual,0)=0
		"""
	)
	return [(dt, f) for dt, f in rows if dt not in SKIP_TEXT_DOCTYPES and frappe.db.table_exists(dt)]


def _rewrite_text_links(mappings):
	"""
	Replace links inside HTML / Markdown. Only quoted or bracketed occurrences are
	replaced ("url, 'url, (url) so '/files/a.png' never matches inside '/private/files/a.png'.
	"""
	site = frappe.utils.get_url().rstrip("/")
	fields = _text_fields()
	for m in mappings:
		olds = [m.old_url]
		if is_local_url(m.old_url):
			olds.insert(0, site + m.old_url)
		for doctype, field in fields:
			expr = f"`{field}`"
			params = {"like": "%" + _like_escape(m.old_url) + "%"}
			i = 0
			for old in olds:
				for q in ('"', "'", "("):
					params[f"o{i}"], params[f"n{i}"] = q + old, q + m.new_url
					expr = f"replace({expr}, %(o{i})s, %(n{i})s)"
					i += 1
			try:
				frappe.db.sql(
					f"update `tab{doctype}` set `{field}`={expr} where `{field}` like %(like)s",
					params,
				)
			except Exception:
				frappe.log_error(title=f"Cloudflare R2: could not rewrite links in {doctype}.{field}")


def _like_escape(value):
	return re.sub(r"([%_\\])", r"\\\1", value)


# ---------------------------------------------------------------------------
# Scheduled retention jobs
# ---------------------------------------------------------------------------


def purge_r2_trash():
	"""Daily: permanently remove R2 objects deleted more than N days ago."""
	settings = get_settings()
	days = cint(settings.trash_retention_days)
	if not (settings.access_key_id and settings.bucket_name) or days <= 0:
		return
	try:
		count = R2(settings).purge_trash(days)
		if count:
			frappe.logger("frappe_cloudflare_r2").info(
				f"Purged {count} trashed objects older than {days} days"
			)
	except Exception:
		frappe.log_error(title="Cloudflare R2: trash purge failed")


def purge_s3_sources():
	"""Daily: delete migrated S3 originals after the configured retention period."""
	settings = get_settings()
	days = cint(settings.s3_source_retention_days)
	if days <= 0 or not settings.s3_bucket_name:
		return
	logs = frappe.get_all(
		LOG,
		filters={
			"source": "Amazon S3",
			"status": "Migrated",
			"source_deleted": 0,
			"migrated_on": ("<", add_days(now_datetime(), -days)),
		},
		fields=["name", "old_url", "source_key", "source_bucket"],
		limit=5000,
	)
	if not logs:
		return
	s3 = S3Source(settings)
	for log in logs:
		if log.source_bucket and log.source_bucket != s3.bucket:
			continue
		if frappe.db.exists("File", {"file_url": log.old_url}):
			continue  # something still points at S3, keep it
		try:
			s3.delete(log.source_key)
			frappe.db.set_value(LOG, log.name, {"source_deleted": 1, "source_deleted_on": now_datetime()})
		except Exception:
			frappe.log_error(title=f"Cloudflare R2: could not delete S3 object {log.source_key}")
	frappe.db.commit()
