# Copyright (c) 2026, Supagigs and contributors
# For license information, please see license.txt
"""
Lets code that reads attachment bytes (email attachments, Data Import, Prepared Reports,
zip downloads...) work for files stored in Cloudflare R2.

* Frappe v16+: R2FileMixin is registered through the `extend_doctype_class` hook, so the
  core File class is extended, not replaced.
* Frappe v15 has no extension hook, so R2File (mixin + core File) is registered through
  `override_doctype_class` on v15 only (see hooks.py).

A file in R2 has no path on this server. Read it with `get_raw_content()`, or, for a library that
needs a path (PIL, PDF tools, zipfile), with `with file_doc.local_copy() as path:`.
`get_full_path()` raises R2FileNotOnDiskError for such a file instead of returning its URL.
"""

import contextlib
import os
import tempfile

import frappe
from frappe import _
from frappe.core.doctype.file.file import File
from frappe.utils import escape_html

OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
ENCODINGS = ("utf-8-sig", "utf-8", "windows-1250", "windows-1252")


class R2FileNotOnDiskError(frappe.ValidationError):
	"""Code asked for the disk path of a file that is stored in Cloudflare R2."""


def logger():
	return frappe.logger("frappe_cloudflare_r2")


class R2FileMixin:
	def _r2_key(self):
		from frappe_cloudflare_r2.storage import is_local_url, key_from_url

		if self.is_folder or not self.file_url or is_local_url(self.file_url):
			return None
		return key_from_url(self.file_url)

	def get_full_path(self):
		"""Path on this server's disk. Raises R2FileNotOnDiskError for a file stored in R2.

		Frappe would return the R2 download URL unchanged, and opening it fails with
		"[Errno 2] No such file or directory: '/api/method/...'".
		"""
		if self.flags.r2_local_path:
			return self.flags.r2_local_path  # set only while unzip() works on a temporary copy
		if self._r2_key():
			frappe.throw(
				_(
					"File {0} is stored in Cloudflare R2, not on disk. "
					"Use file_doc.get_raw_content() or `with file_doc.local_copy() as path:`."
				).format(escape_html(self.file_name or self.name)),
				exc=R2FileNotOnDiskError,
				title=_("File is in Cloudflare R2"),
			)
		return super().get_full_path()

	@contextlib.contextmanager
	def local_copy(self):
		"""Yield a real path on this server's disk holding the file's bytes.

		The supported way to hand a file to a library that needs a path (PIL, PDF tools, zipfile):

		    with file_doc.local_copy() as path:
		        image = Image.open(path)

		* Local file: yields its own path. Don't change or delete it.
		* R2 file: downloads it to a private temporary file in the system temp folder (mode 0600,
		  same extension), yields that path and always deletes it afterwards, even if the block raises.

		Like get_raw_content(), this does not check permissions; the caller must.
		"""
		if self.is_folder:
			frappe.throw(_("Cannot get file contents of a Folder"))
		key = self._r2_key()
		if not key:
			if self.is_remote_file:
				frappe.throw(
					_("File {0} is not stored on this server or in Cloudflare R2.").format(
						escape_html(self.file_name or self.name)
					)
				)
			self.validate_file_path()
			yield self.get_full_path()
			return

		from frappe_cloudflare_r2.storage import R2

		suffix = os.path.splitext(self.file_name or key)[1]
		fd, path = tempfile.mkstemp(prefix="r2-", suffix=suffix)  # created with mode 0600
		try:
			with os.fdopen(fd, "wb") as fh:
				R2().download_fileobj(key, fh, self.is_private)
			yield path
		finally:
			with contextlib.suppress(FileNotFoundError):
				os.remove(path)

	def get_raw_content(self) -> bytes:
		"""Exact bytes of the file wherever it is stored (R2 or local disk), never decoded.

		Use this instead of get_content() or open(get_full_path()) for binary files
		such as PDFs and images.
		"""
		key = self._r2_key()
		if key:
			from frappe_cloudflare_r2.storage import R2

			return R2().get_bytes(key, self.is_private)
		if self.get("content"):
			content = self.content
			return content.encode() if isinstance(content, str) else content
		with open(self.get_full_path(), "rb") as fh:
			return fh.read()

	def get_content(self, *args, **kwargs):
		key = None if self.get("content") else self._r2_key()
		if not key:
			return super().get_content(*args, **kwargs)

		from frappe_cloudflare_r2.storage import R2

		content = R2().get_bytes(key, self.is_private)
		encodings = kwargs.get("encodings")
		if encodings is None:
			encodings = ENCODINGS
		if not content.startswith(OLE_SIGNATURE):
			for encoding in encodings:
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

	def validate_file_on_disk(self):
		# Runs on every save. Frappe skips URLs here too; checking R2 would cost a request per save.
		if self._r2_key():
			return True
		return super().validate_file_on_disk()

	def on_rollback(self):
		"""Undo an insert whose file was uploaded to R2 in the same request.

		The upload changed file_url to the R2 URL, so Frappe's cleanup would look for the wrong
		file and leave the server copy behind. Run it against the server copy's URL instead.
		The R2 object itself is removed by the upload's own rollback callback (controller.py).
		"""
		local_url = self.flags.r2_uploaded_from
		if not (local_url and self._r2_key()):
			return super().on_rollback()
		r2_url, self.file_url = self.file_url, local_url
		try:
			return super().on_rollback()
		finally:
			self.file_url = r2_url

	def unzip(self):
		if not self._r2_key():
			return super().unzip()
		if not self.file_url.endswith(".zip"):
			frappe.throw(_("{0} is not a zip file").format(escape_html(self.file_name)))
		self.check_permission("read")  # before downloading anything
		with self.local_copy() as path:
			self.flags.r2_local_path = path
			try:
				return super().unzip()
			finally:
				self.flags.r2_local_path = None

	def make_thumbnail(self, *args, **kwargs):
		# A private image's thumbnail would be written to the public files folder, so skip it.
		# Public R2 images keep Frappe's behaviour (it fetches them over HTTP).
		if self.is_private and self._r2_key():
			logger().info(f"Thumbnail skipped for File {self.name}: private file stored in Cloudflare R2")
			return None
		return super().make_thumbnail(*args, **kwargs)

	@frappe.whitelist()
	def optimize_file(self):
		# For a remote file Frappe optimises the bytes, then saves nothing, and the form still
		# reports "Image optimized". Say so instead.
		if self._r2_key():
			logger().info(f"Optimize refused for File {self.name}: stored in Cloudflare R2")
			frappe.throw(
				_(
					"Image optimisation is not available for files stored in Cloudflare R2. "
					"The file was not changed."
				),
				title=_("File is in Cloudflare R2"),
			)
		return super().optimize_file()


class R2File(R2FileMixin, File):
	"""Frappe v15 only. v16+ uses R2FileMixin via extend_doctype_class."""
