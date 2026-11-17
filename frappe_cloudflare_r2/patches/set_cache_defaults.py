# Copyright (c) 2026, Supagigs and contributors
# For license information, please see license.txt
"""Give existing sites the Server Cache defaults (7 days, 2048 MB).

New fields on a Single don't get their defaults on sites that already saved the Single, and 0
would turn both limits off. Only fields that have never been set are written, so re-running is safe.
A site that never saved the Single is left alone: Frappe fills every default when it loads, and
writing two rows would stop that for all the other fields (trash retention would become 0).
"""

import frappe

SETTINGS = "Cloudflare R2 Settings"
DEFAULTS = {"cache_retention_days": 7, "cache_max_size_mb": 2048}


def execute():
	Singles = frappe.qb.DocType("Singles")
	saved = set(
		frappe.qb.from_(Singles).select(Singles.field).where(Singles.doctype == SETTINGS).run(pluck=True)
	)
	if not saved:
		return
	for field, value in DEFAULTS.items():
		if field not in saved:
			frappe.db.set_single_value(SETTINGS, field, value)
	frappe.clear_document_cache(SETTINGS, SETTINGS)  # get_settings() is cached
