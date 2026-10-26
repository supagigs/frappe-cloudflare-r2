# Copyright (c) 2026, Supagigs and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint


class CloudflareR2Settings(Document):
	def onload(self):
		# Pre-fill the S3 section from frappe_s3_attachment, without overwriting user input.
		if not self.s3_bucket_name and frappe.db.table_exists("S3 File Attachment"):
			src = frappe.db.get_singles_dict("S3 File Attachment")
			self.s3_bucket_name = src.get("bucket_name")
			self.s3_region = src.get("region_name")
			self.s3_access_key_id = src.get("aws_key")
			if src.get("aws_secret"):
				self.set_onload("s3_secret_from_app", 1)
		self.set_onload("s3_app_installed", "frappe_s3_attachment" in frappe.get_installed_apps())

	def validate(self):
		for field in ("account_id", "bucket_name", "public_bucket_name", "access_key_id", "s3_bucket_name"):
			if self.get(field):
				self.set(field, self.get(field).strip())
		for field in ("public_base_url", "endpoint_url", "s3_endpoint_url"):
			value = (self.get(field) or "").strip().rstrip("/")
			if value and not value.startswith("https://"):
				frappe.throw(_("{0} must start with https://").format(self.meta.get_label(field)))
			self.set(field, value)
		if self.folder_name:
			self.folder_name = "/".join(p for p in self.folder_name.strip().split("/") if p)

		if cint(self.multipart_chunksize_mb) and cint(self.multipart_chunksize_mb) < 5:
			frappe.throw(_("Part Size must be at least 5 MB (S3 API limit)."))
		for field in ("trash_retention_days", "s3_source_retention_days", "signed_url_expiry"):
			if cint(self.get(field)) < 0:
				frappe.throw(_("{0} cannot be negative").format(self.meta.get_label(field)))
		if cint(self.signed_url_expiry) > 7 * 24 * 3600:
			frappe.throw(_("Signed URL Expiry cannot exceed 7 days (604800 seconds)."))

		if self.enabled:
			missing = [
				self.meta.get_label(f)
				for f in ("account_id", "bucket_name", "access_key_id")
				if not self.get(f) and not (f == "account_id" and self.endpoint_url)
			]
			if not self.get_password("secret_access_key", raise_exception=False):
				missing.append(self.meta.get_label("secret_access_key"))
			if missing:
				frappe.throw(_("Please set {0} before enabling Cloudflare R2.").format(", ".join(missing)))

	def on_update(self):
		if (
			self.enabled
			and self.has_value_changed("enabled")
			and "frappe_s3_attachment" in frappe.get_installed_apps()
		):
			frappe.msgprint(
				_(
					"The Frappe S3 Attachment app is still installed, so new uploads may also be sent to S3. "
					"Run 'Migrate S3 Files → R2', then uninstall frappe_s3_attachment. Old S3 links keep working."
				),
				title=_("Uninstall S3 Attachment after migrating"),
				indicator="orange",
			)
