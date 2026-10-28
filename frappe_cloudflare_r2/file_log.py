# Copyright (c) 2026, Supagigs and contributors
# For license information, please see license.txt
"""
R2 File Log: one row per file movement (upload, skip, delete, trash, purge, rollback).

Logging must never break the file operation it describes, so every write is wrapped.
"""

import frappe
from frappe.utils import add_days, cint, now_datetime

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


def purge_old_logs():
	"""Daily: delete log rows older than the configured retention (0 keeps them forever)."""
	days = cint(frappe.db.get_single_value("Cloudflare R2 Settings", "file_log_retention_days"))
	if days <= 0:
		return
	frappe.db.delete(DOCTYPE, {"creation": ("<", add_days(now_datetime(), -days))})
