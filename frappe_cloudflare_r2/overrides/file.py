# Copyright (c) 2026, Supagigs and contributors
# For license information, please see license.txt
"""
Lets code that reads attachment bytes (email attachments, Data Import, Prepared Reports,
zip downloads...) work for files stored in Cloudflare R2.

* Frappe v16+: R2FileMixin is registered through the `extend_doctype_class` hook, so the
  core File class is extended, not replaced.
* Frappe v15 has no extension hook, so R2File (mixin + core File) is registered through
  `override_doctype_class` on v15 only (see hooks.py).
"""

from frappe.core.doctype.file.file import File

OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
ENCODINGS = ("utf-8-sig", "utf-8", "windows-1250", "windows-1252")


class R2FileMixin:
	def _r2_key(self):
		if self.is_folder or not self.file_url:
			return None
		from frappe_cloudflare_r2.storage import key_from_url

		return key_from_url(self.file_url)

	def get_content(self, *args, **kwargs):
		key = None if self.get("content") else self._r2_key()
		if not key:
			return super().get_content(*args, **kwargs)

		from frappe_cloudflare_r2.storage import R2

		content = R2().get_bytes(key, self.is_private)
		if not content.startswith(OLE_SIGNATURE):
			for encoding in kwargs.get("encodings") or ENCODINGS:
				try:
					content = content.decode(encoding)
					break
				except UnicodeDecodeError:
					continue
		self._content = content
		return content

	def exists_on_disk(self):
		if self._r2_key():
			return True
		return super().exists_on_disk()


class R2File(R2FileMixin, File):
	"""Frappe v15 only. v16+ uses R2FileMixin via extend_doctype_class."""
