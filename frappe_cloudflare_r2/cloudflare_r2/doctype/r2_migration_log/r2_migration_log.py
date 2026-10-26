# Copyright (c) 2026, Supagigs and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class R2MigrationLog(Document):
	pass


def on_doctype_update():
	import frappe

	frappe.db.add_index("R2 Migration Log", ["source_key(255)"])
