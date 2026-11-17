# Copyright (c) 2026, Supagigs and contributors
# For license information, please see license.txt
"""Move R2 files attached to Repost Item Valuation and Bank Statement Import back to the server disk.

ERPNext writes back into these files by path (see storage.WRITE_BACK_DOCTYPES). New uploads for them
already stay local; this moves the ones uploaded to R2 before 1.0.1. Each file goes through the
R2 -> local exit path (migration._r2_to_local); File rows and Attach fields are re-pointed.

Idempotent: a moved file has a local URL, so a second run finds nothing. A file that can't be moved
is left in R2 and written to the Error Log; run this again once the cause is fixed:
    bench --site <site> execute frappe_cloudflare_r2.patches.move_write_back_files_to_local.execute
"""

import frappe
from frappe.query_builder.functions import Max, Min
from frappe.utils import now_datetime

from frappe_cloudflare_r2 import migration
from frappe_cloudflare_r2.storage import DOWNLOAD_PREFIX, R2, WRITE_BACK_DOCTYPES, get_settings

ERROR_TITLE = "Cloudflare R2: write-back file NOT moved back to the server"


def execute():
	settings = get_settings()
	rows = _candidates(settings)
	if not rows:
		return
	try:
		r2 = R2(settings)
	except Exception:
		frappe.log_error(
			title=ERROR_TITLE,
			message=f"R2 is not configured, so {len(rows)} file(s) attached to {', '.join(WRITE_BACK_DOCTYPES)} "
			f"stay in R2. ERPNext can't write back into them.\n\n{frappe.get_traceback()}",
		)
		return

	run_id = f"write-back-to-local-{now_datetime().strftime('%Y%m%d%H%M%S')}"
	moved = 0
	for row in rows:
		if _move(row, r2, settings, run_id):
			moved += 1
	if moved:
		migration._rewrite_references(run_id, 1)


def _candidates(settings):
	"""One row per R2 URL used by a File attached to a write-back DocType."""
	File = frappe.qb.DocType("File")
	is_r2 = File.file_url.like(DOWNLOAD_PREFIX + "%")
	base = (settings.public_base_url or "").rstrip("/")
	if base:
		is_r2 = is_r2 | File.file_url.like(base + "/%")
	return (
		frappe.qb.from_(File)
		.select(
			File.file_url,
			Max(File.is_private).as_("is_private"),
			Min(File.file_name).as_("file_name"),
			Min(File.attached_to_doctype).as_("attached_to_doctype"),
			Min(File.name).as_("file"),
		)
		.where((File.is_folder == 0) & File.attached_to_doctype.isin(WRITE_BACK_DOCTYPES) & is_r2)
		.groupby(File.file_url)
		.orderby(File.file_url)
	).run(as_dict=True)


def _move(row, r2, settings, run_id):
	try:
		new_url, extra = migration._r2_to_local(row, r2, settings)
	except Exception:
		frappe.log_error(
			title=ERROR_TITLE,
			message=f"File {row.file} ({row.file_url}) attached to {row.attached_to_doctype} is still in R2. "
			f"ERPNext can't write back into it until it is moved.\n\n{frappe.get_traceback()}",
			reference_doctype="File",
			reference_name=row.file,
		)
		return False

	File = frappe.qb.DocType("File")
	frappe.qb.update(File).set(File.file_url, new_url).where(
		(File.file_url == row.file_url) & (File.is_folder == 0)
	).run()
	frappe.get_doc(
		{
			"doctype": migration.LOG,
			"run_id": run_id,
			"batch_no": 1,
			"direction": migration.DIRECTIONS["r2_to_local"],
			"file": row.file,
			"file_name": row.file_name,
			"old_url": row.file_url,
			"new_url": new_url,
			"is_private": row.is_private,
			"status": "Migrated",
			"migrated_on": now_datetime(),
			**extra,
		}
	).insert()  # patches and bench execute run as Administrator
	return True
