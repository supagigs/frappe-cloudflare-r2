# Copyright (c) 2026, Supagigs and contributors
# For license information, please see license.txt
"""
File subclass so code that reads attachment bytes (email attachments, Data Import,
Prepared Reports, zip downloads...) works for files stored in Cloudflare R2.
"""

from frappe.core.doctype.file.file import File

try:  # v15 and v16
	from frappe.core.doctype.file.utils import decode_file_content
except ImportError:  # pragma: no cover
	decode_file_content = None

OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
ENCODINGS = ("utf-8-sig", "utf-8", "windows-1250", "windows-1252")


class R2File(File):
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
