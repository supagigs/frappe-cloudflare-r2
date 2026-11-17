# Copyright (c) 2026, Supagigs and contributors
# For license information, please see license.txt
"""
R2 -> local moves never make bytes or rows less private, and clean up their own download on failure.

    bench --site test_site run-tests --module frappe_cloudflare_r2.tests.test_r2_move_privacy

Same moto server, settings snapshot and clean-up as test_cloudflare_r2 (its module fixtures run here).
"""

import os
from unittest import mock

import frappe
from frappe.utils import CallbackManager

from frappe_cloudflare_r2 import controller, migration
from frappe_cloudflare_r2.storage import R2, get_settings, key_from_url, key_is_private
from frappe_cloudflare_r2.tests import test_cloudflare_r2 as base
from frappe_cloudflare_r2.tests.test_cloudflare_r2 import R2TestCase, commit, make_file


def setUpModule():
	base.setUpModule()


def tearDownModule():
	base.tearDownModule()


class TestR2MovePrivacy(R2TestCase):
	def unique(self, prefix):
		return f"{prefix}-{frappe.generate_hash(length=6)}.txt"

	def second_row(self, doc, is_private):
		"""Another File row on the same R2 URL, as left by copies or toggles before 1.0.1."""
		dup = frappe.copy_doc(doc)
		dup.name = frappe.generate_hash(length=10)
		dup.is_private = is_private
		dup.db_insert()
		base.CREATED_FILES.append(dup.name)
		commit()
		return dup

	def move(self, doc, is_private, after_commit=None):
		# the patch's _candidates passes Max(is_private) over the rows for one URL
		row = frappe._dict(file_url=doc.file_url, is_private=is_private, file_name=doc.file_name, file=doc.name)
		return migration._r2_to_local(row, R2(), get_settings(), after_commit=after_commit or CallbackManager())

	def assert_under_private_files(self, new_url):
		self.assertTrue(new_url.startswith("/private/files/"), new_url)
		private_files = os.path.abspath(frappe.get_site_path("private", "files"))
		self.assertEqual(os.path.dirname(os.path.abspath(controller.local_path(new_url))), private_files)

	def test_rows_sharing_a_private_key_all_become_private(self):
		content = frappe.generate_hash().encode()
		doc = make_file(self.unique("shared"), content, is_private=1)
		commit()
		self.assertEqual(key_is_private(key_from_url(doc.file_url)), 1)
		dup = self.second_row(doc, is_private=0)

		new_url, _extra = self.move(doc, is_private=1)
		path = controller.local_path(new_url)
		try:
			self.assert_under_private_files(new_url)
			with open(path, "rb") as fh:
				self.assertEqual(fh.read(), content)
			self.assertEqual(frappe.db.get_value("File", doc.name, "is_private"), 1)
			self.assertEqual(frappe.db.get_value("File", dup.name, "is_private"), 1)
		finally:
			os.remove(path)

	def test_public_key_with_a_private_row_lands_in_private_files(self):
		content = frappe.generate_hash().encode()
		doc = make_file(self.unique("public-key"), content, is_private=0)
		commit()
		self.assertEqual(key_is_private(key_from_url(doc.file_url)), 0)
		frappe.db.set_value("File", doc.name, "is_private", 1, update_modified=False)
		dup = self.second_row(doc, is_private=0)

		new_url, _extra = self.move(doc, is_private=1)  # bytes come from the public bucket
		path = controller.local_path(new_url)
		try:
			self.assert_under_private_files(new_url)
			with open(path, "rb") as fh:
				self.assertEqual(fh.read(), content)
			self.assertEqual(frappe.db.get_value("File", doc.name, "is_private"), 1)
			self.assertEqual(frappe.db.get_value("File", dup.name, "is_private"), 1)
		finally:
			os.remove(path)

	def test_failure_after_download_leaves_no_local_file(self):
		doc = make_file(self.unique("fails"), is_private=1)
		commit()
		downloaded = []
		real_download = R2.download_to

		def recording_download(r2, key, path, is_private):
			downloaded.append(path)
			return real_download(r2, key, path, is_private)

		class Broken(CallbackManager):
			def add(self, func):
				raise RuntimeError("queue failed")

		with mock.patch.object(R2, "download_to", recording_download), self.assertRaises(RuntimeError):
			self.move(doc, is_private=1, after_commit=Broken())
		frappe.db.rollback()
		self.assertEqual(len(downloaded), 1)
		self.assertFalse(os.path.exists(downloaded[0]))

