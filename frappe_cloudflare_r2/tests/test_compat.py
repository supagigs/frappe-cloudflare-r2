# Copyright (c) 2026, Supagigs and contributors
# For license information, please see license.txt
"""
frappe.utils.file_manager.get_file()/get_file_path() on R2 files (compat.py), against moto.

Same setup and clean-up as test_cloudflare_r2 (the site's settings are saved and put back, only
rows and cached copies made here are removed).

    bench --site test_site run-tests --app frappe_cloudflare_r2
"""

import base64
import os
import types
from unittest import mock

import frappe
from frappe.utils import file_manager
from frappe.utils.data import pdf_to_base64

from frappe_cloudflare_r2 import compat, controller
from frappe_cloudflare_r2.storage import key_from_url
from frappe_cloudflare_r2.tests import test_cloudflare_r2 as base


def setUpModule():
	base.setUpModule()


def tearDownModule():
	base.tearDownModule()


class TestFileManagerCompat(base.R2TestCase):
	def r2_file(self, name, content, is_private=1):
		doc = base.make_file(name, content, is_private=is_private)
		base.commit()
		self.assertTrue(key_from_url(doc.file_url))
		return frappe.get_doc("File", doc.name)

	def unique(self, stem, ext):
		return f"{stem}-{frappe.generate_hash(length=8)}.{ext}"

	def test_frappe_source_unchanged(self):
		"""Fails when Frappe changes get_file_path(): review the new source, then update SOURCE_SHA256."""
		self.assertEqual(compat.source_sha256(compat._orig_get_file_path), compat.SOURCE_SHA256)
		self.assertEqual(int(frappe.__version__.split(".")[0]), compat.FRAPPE_MAJOR)

	def test_patch_is_in_place_once_and_original_reachable(self):
		self.assertTrue(getattr(file_manager.get_file_path, compat.WRAPPED, False))
		self.assertEqual(compat._orig_get_file_path.__module__, "frappe.utils.file_manager")
		self.assertFalse(getattr(compat._orig_get_file_path, compat.WRAPPED, False))
		wrapper = file_manager.get_file_path
		self.assertTrue(compat.apply())  # idempotent
		self.assertIs(file_manager.get_file_path, wrapper)
		self.assertFalse(getattr(compat._orig_get_file_path, compat.WRAPPED, False))

	def test_get_file_path_of_r2_file_returns_cached_copy(self):
		content = frappe.generate_hash().encode()
		doc = self.r2_file(self.unique("fm", "txt"), content)
		for arg in (doc.file_name, doc.name):
			path = file_manager.get_file_path(arg)
			self.assertEqual(path, doc.get_full_path())
			with open(path, "rb") as fh:
				self.assertEqual(fh.read(), content)

	def test_get_file_of_r2_file_returns_content(self):
		# get_file() names the result after the path's last part: the cached copy's name
		text = frappe.generate_hash()
		doc = self.r2_file(self.unique("fm", "txt"), text.encode())
		cached_name = os.path.basename(doc.get_full_path())
		self.assertEqual(file_manager.get_file(doc.file_name), [cached_name, text])

		binary = base.png_bytes()
		img = self.r2_file(self.unique("fm", "png"), binary)
		name, content = file_manager.get_file(img.name)
		self.assertEqual(content, binary)
		self.assertTrue(name.endswith(".png"))

	def test_public_r2_file(self):
		base.configure(public_base_url="https://files.example.com", public_bucket_name=base.PUBLIC_BUCKET)
		content = frappe.generate_hash().encode()
		doc = self.r2_file(self.unique("pub", "txt"), content, is_private=0)
		self.assertEqual(file_manager.get_file_path(doc.file_name), doc.get_full_path())
		self.assertEqual(file_manager.get_file(doc.file_name)[1], content.decode())

	def test_pdf_to_base64_reads_r2_file(self):
		from io import BytesIO

		from pypdf import PdfWriter

		writer, buf = PdfWriter(), BytesIO()
		writer.add_blank_page(width=72, height=72)
		writer.write(buf)
		content = buf.getvalue()
		doc = self.r2_file(self.unique("doc", "pdf"), content)
		self.assertEqual(pdf_to_base64(doc.file_name), base64.b64encode(content))

	def test_local_file_unchanged(self):
		base.configure(enabled=0)
		content = frappe.generate_hash().encode()
		doc = base.make_file(self.unique("plain", "txt"), content)
		self.assertFalse(key_from_url(doc.file_url))
		path = file_manager.get_file_path(doc.file_name)
		self.assertEqual(path, compat._orig_get_file_path(doc.file_name))
		self.assertEqual(os.path.realpath(path), os.path.realpath(controller.local_path(doc.file_url)))
		self.assertEqual(file_manager.get_file(doc.name)[1], content.decode())

	def test_unknown_and_unsafe_names_go_to_core(self):
		self.assertIsNone(file_manager.get_file_path("../etc/passwd"))
		missing = self.unique("missing", "txt")
		self.assertEqual(file_manager.get_file_path(missing), compat._orig_get_file_path(missing))
		self.assertIsNone(compat.r2_file(None))

	def fake_module(self):
		def get_file_path(file_name):
			return "original"

		return types.SimpleNamespace(__name__="fake_file_manager", get_file_path=get_file_path)

	def test_guard_skips_other_source_and_logs(self):
		fake = self.fake_module()
		original, real = fake.get_file_path, compat._orig_get_file_path
		with mock.patch.object(compat, "_logger") as logger:
			self.assertFalse(compat.apply(fake))
		self.assertIs(fake.get_file_path, original)
		logger.return_value.warning.assert_called_once()
		self.assertIs(compat._orig_get_file_path, real)  # the real original is untouched

	def test_guard_skips_other_major_version(self):
		fake = self.fake_module()
		original = fake.get_file_path
		with (
			mock.patch.object(compat, "SOURCE_SHA256", compat.source_sha256(original)),
			mock.patch.object(compat, "_logger") as logger,
		):
			self.assertFalse(compat.apply(fake, frappe_version="17.0.0"))
			self.assertIs(fake.get_file_path, original)
			logger.return_value.warning.assert_called_once()
			# same source on v16: wrapped, and a non-R2 name still reaches the original
			self.assertTrue(compat.apply(fake, frappe_version="16.1.0"))
		self.assertTrue(getattr(fake.get_file_path, compat.WRAPPED, False))
		self.assertEqual(fake.get_file_path("not-a-file"), "original")
		self.assertFalse(compat.guard_passes(original, "not-a-version"))
