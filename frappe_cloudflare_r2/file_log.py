# Copyright (c) 2026, Supagigs and contributors
# For license information, please see license.txt
"""
R2 File Log: one row per file movement (upload, skip, delete, trash, purge, rollback).

Logging must never break the file operation it describes, so every write is wrapped.
"""

import frappe
from frappe.utils import add_days, cint, now_datetime, time_diff_in_seconds

DOCTYPE = "R2 File Log"


def r2_location(bucket, key):
	return f"r2://{bucket}/{key}" if bucket and key else None


def log(event, status="Success", doc=None, commit=False, **fields):
	"""Insert a log row. Pass doc (a File) to fill file details automatically."""
	try:
		row = {
			"doctype": DOCTYPE,
			"event": event,
			"status": status,
			"user": frappe.session.user if getattr(frappe.local, "session", None) else None,
		}
		if doc is not None:
			row.update(
				{
					"file": doc.name,
					"file_name": doc.file_name,
					"file_url": doc.file_url,
					"is_private": cint(doc.is_private),
					"attached_to_doctype": doc.attached_to_doctype,
					"attached_to_name": doc.attached_to_name,
				}
			)
		row.update({k: v for k, v in fields.items() if v is not None})
		if row.get("error"):
			row["error"] = str(row["error"])[:2000]
		frappe.get_doc(row).insert(ignore_permissions=True)
		if commit:
			# Only used from after_commit / after_rollback callbacks, where the request
			# transaction has already ended and nothing else will commit this row.
			frappe.db.commit()  # nosemgrep
	except Exception:
		frappe.logger("frappe_cloudflare_r2").exception(f"Could not write {DOCTYPE} ({event})")


def deferred_uploads(older_than_minutes=0):
	"""Uploads waiting for R2 (kept on disk during an outage): one entry per file, oldest first.

	Keyed by file_url. A file with several deferred rows gives one entry, with the oldest
	deferral as deferred_at and age_minutes. Each item has file, file_name, file_url,
	deferred_at, age_minutes and log (the oldest row). Pass older_than_minutes=60 to list
	only files past the alert time.
	"""
	rows = frappe.get_all(
		DOCTYPE,
		filters={"event": "Upload Deferred", "status": "Deferred"},
		fields=["name", "file", "file_name", "from_location", "creation"],
		order_by="creation asc",
	)
	# rows are oldest first, so the first row seen for a file_url is its oldest deferral
	first_by_url = {}
	for row in rows:
		first_by_url.setdefault(row.from_location, row)
	now = now_datetime()
	out = []
	for row in first_by_url.values():
		age = time_diff_in_seconds(now, row.creation) / 60
		if age < older_than_minutes:
			continue
		out.append(
			{
				"log": row.name,
				"file": row.file,
				"file_name": row.file_name,
				"file_url": row.from_location,
				"deferred_at": row.creation,
				"age_minutes": round(age, 1),
			}
		)
	return out


def purge_old_logs():
	"""Daily: delete log rows older than the configured retention (0 keeps them forever).

	Deferred rows are never purged. They are the only record of a file waiting for R2, and
	the retry needs their r2_key, so they stay until the upload succeeds or is skipped.
	"""
	days = cint(frappe.db.get_single_value("Cloudflare R2 Settings", "file_log_retention_days"))
	if days <= 0:
		return
	frappe.db.delete(
		DOCTYPE,
		{
			"creation": ("<", add_days(now_datetime(), -days)),
			"status": ("!=", "Deferred"),
		},
	)
