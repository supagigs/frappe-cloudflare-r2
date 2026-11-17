# Copyright (c) 2026, Supagigs and contributors
# For license information, please see license.txt
"""
Review fixes on the R2 cache (1.0.1): grace rule for size eviction, removing both copies, the
write-back patch, fixed visibility for R2 files, and the size check when a copy is added.

    bench --site test_site run-tests --module frappe_cloudflare_r2.tests.test_r2_cache_fixes

Same moto server, settings snapshot and clean-up as test_cloudflare_r2 (its module fixtures run here).
"""

import os
import time
from unittest import mock

import frappe
import requests

from frappe_cloudflare_r2 import cache, controller, migration
from frappe_cloudflare_r2.patches import move_write_back_files_to_local as write_back_patch
from frappe_cloudflare_r2.patches import set_cache_defaults
from frappe_cloudflare_r2.storage import R2, key_from_url, key_is_private
from frappe_cloudflare_r2.tests import test_cloudflare_r2 as base
from frappe_cloudflare_r2.tests.test_cloudflare_r2 import R2TestCase, age, commit, configure, make_file

HOUR = 60 * 60


def setUpModule():
	base.setUpModule()


def tearDownModule():
	base.tearDownModule()


def hours_ago(path, hours):
	stamp = time.time() - hours * HOUR
	os.utime(path, (stamp, stamp))


class TestR2CacheFixes(R2TestCase):
	def r2_file(self, name, content=None, is_private=1):
		doc = make_file(name, content, is_private=is_private)
		commit()
		self.assertTrue(key_from_url(doc.file_url))
		return frappe.get_doc("File", doc.name)

	def unique(self, name):
		base_name, ext = os.path.splitext(name)
		return f"{base_name}-{frappe.generate_hash(length=6)}{ext}"

	# -- F1: size eviction spares copies used in the last hour ----------------------------

	def test_sweep_never_evicts_a_copy_used_in_the_last_hour(self):
		path = self.r2_file(self.unique("fresh.bin"), os.urandom(1200 * 1024)).get_full_path()
		with mock.patch.object(cache, "logger") as logger:
			cache.sweep(retention_days=7, max_size_mb=1)
		self.assertTrue(os.path.exists(path))  # handed out moments ago: kept
		logger().warning.assert_called_once()
		self.assertIn("over its size limit", logger().warning.call_args[0][0])

	def test_sweep_evicts_copies_older_than_an_hour_over_the_limit(self):
		old = self.r2_file(self.unique("old.bin"), os.urandom(700 * 1024)).get_full_path()
		fresh = self.r2_file(self.unique("new.bin"), os.urandom(700 * 1024)).get_full_path()
		hours_ago(old, 2)
		cache.sweep(retention_days=7, max_size_mb=1)
		self.assertFalse(os.path.exists(old))
		self.assertTrue(os.path.exists(fresh))

	# -- F2: remove() deletes both the private and the public copy ------------------------

	def test_delete_removes_private_and_public_copies_after_visibility_change(self):
		doc = self.r2_file(self.unique("both-sides.txt"), frappe.generate_hash().encode())
		key = key_from_url(doc.file_url)
		private_copy = doc.get_full_path()
		# a row changed before 1.0.1 (validate now refuses it), so bypass validate
		frappe.db.set_value("File", doc.name, "is_private", 0, update_modified=False)
		commit()
		self.assertEqual(frappe.get_doc("File", doc.name).get_full_path(), private_copy)  # side from the key
		# a public-side copy left by the row's flag before 1.0.1 read the bucket from the row
		public_copy = cache.cached_copy(key, 0, doc.file_name)
		self.assertNotEqual(private_copy, public_copy)
		self.assertTrue(os.path.exists(private_copy) and os.path.exists(public_copy))

		frappe.delete_doc("File", doc.name)
		commit()
		self.assertFalse(os.path.exists(private_copy))
		self.assertFalse(os.path.exists(public_copy))
		self.assertFalse(R2().exists(key, True))

	def test_remove_never_creates_the_folder(self):
		with mock.patch.object(cache.os, "makedirs") as makedirs:
			cache.remove("testsite/private/not-cached.txt", 1, "not-cached.txt")
		makedirs.assert_not_called()

	# -- F4: size check when a copy is added -----------------------------------------------

	def test_adding_a_copy_evicts_older_copies_over_the_limit(self):
		first = self.r2_file(self.unique("lru-a.bin"), os.urandom(700 * 1024))
		second = self.r2_file(self.unique("lru-b.bin"), os.urandom(700 * 1024))
		configure(cache_max_size_mb=1)
		old = first.get_full_path()
		hours_ago(old, 2)
		new = second.get_full_path()
		self.assertFalse(os.path.exists(old))
		self.assertTrue(os.path.exists(new))

	def test_adding_a_copy_keeps_copies_used_in_the_last_hour(self):
		first = self.r2_file(self.unique("grace-a.bin"), os.urandom(700 * 1024))
		second = self.r2_file(self.unique("grace-b.bin"), os.urandom(700 * 1024))
		configure(cache_max_size_mb=1)
		recent = first.get_full_path()
		age(recent, 10 / (24 * 60))  # 10 minutes ago
		new = second.get_full_path()
		self.assertTrue(os.path.exists(recent) and os.path.exists(new))

	def test_adding_a_copy_under_the_limit_evicts_nothing(self):
		doc = self.r2_file(self.unique("small.txt"), b"small")
		with mock.patch.object(cache, "_evict_over_limit") as evict:
			doc.get_full_path()
		evict.assert_not_called()

	# -- F7: sweep() defaults come from the settings ------------------------------------------

	def test_sweep_reads_its_limits_from_settings(self):
		path = self.r2_file(self.unique("settings.txt"), b"x").get_full_path()
		age(path, 4)
		configure(cache_retention_days=7, cache_max_size_mb=50)
		with mock.patch.object(cache, "_evict_over_limit", wraps=cache._evict_over_limit) as evict:
			cache.sweep()
		self.assertTrue(os.path.exists(path))  # 4 days < 7
		self.assertEqual(evict.call_args[0][1], 50)

		configure(cache_retention_days=3, cache_max_size_mb=50)
		cache.sweep()
		self.assertFalse(os.path.exists(path))  # 4 days > 3

	# -- F6: R2 files keep their visibility ---------------------------------------------------

	def test_visibility_change_on_r2_file_is_refused_for_admin(self):
		doc = self.r2_file(self.unique("fixed.txt"))
		doc.is_private = 0
		with self.assertRaises(frappe.ValidationError) as ctx:
			doc.save()
		self.assertIn("Change visibility by re-uploading", str(ctx.exception))
		self.assertEqual(frappe.db.get_value("File", doc.name, "is_private"), 1)

	def test_other_edits_on_r2_file_still_save(self):
		doc = self.r2_file(self.unique("rename.txt"))
		doc.file_name = self.unique("renamed.txt")
		doc.save()

	def test_visibility_change_on_local_file_still_allowed(self):
		configure(enabled=0)
		doc = frappe.get_doc("File", make_file(self.unique("local.txt"), b"local").name)
		doc.is_private = 0
		doc.save()
		self.assertEqual(frappe.db.get_value("File", doc.name, "is_private"), 0)

	def test_guest_cannot_download_private_object_through_public_row(self):
		doc = self.r2_file(self.unique("was-private.txt"))
		frappe.db.set_value("File", doc.name, "is_private", 0, update_modified=False)  # pre-1.0.1 row
		commit()
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			controller.download_file(key=key_from_url(doc.file_url))

	def test_download_signs_for_the_bucket_named_by_the_key(self):
		configure(public_bucket_name=base.PUBLIC_BUCKET)
		doc = self.r2_file(self.unique("was-public.txt"), b"in public bucket", is_private=0)
		frappe.db.set_value("File", doc.name, "is_private", 1, update_modified=False)  # pre-1.0.1 row
		commit()
		frappe.local.response = frappe._dict()
		controller.download_file(key=key_from_url(doc.file_url))
		self.assertEqual(requests.get(frappe.local.response["location"]).content, b"in public bucket")

		frappe.set_user("Guest")  # every row is private now, so Guest needs permission
		with self.assertRaises(frappe.PermissionError):
			controller.download_file(key=key_from_url(doc.file_url))

	def test_key_is_private(self):
		self.assertEqual(key_is_private("site/private/2026/11/17/File/x.txt"), 1)
		self.assertEqual(key_is_private("site/public/2026/11/17/File/x.txt"), 0)
		self.assertEqual(key_is_private("public/private/x.txt"), 1)
		self.assertIsNone(key_is_private("custom/x.txt"))

	# -- F3: write-back files already in R2 are moved back to the server --------------------

	def seed_write_back_file(self, content):
		doc = self.r2_file(self.unique("statement.csv"), content)
		# uploads for write-back doctypes stay local now, so attach it after the upload
		frappe.db.set_value(
			"File",
			doc.name,
			{"attached_to_doctype": "Bank Statement Import", "attached_to_name": "R2-TEST-BSI"},
			update_modified=False,
		)
		commit()
		return doc

	def only(self, doc):
		"""Limit the patch to this test's file: the site may hold real write-back files in R2."""
		original = write_back_patch._candidates
		return mock.patch.object(
			write_back_patch, "_candidates", lambda s: [r for r in original(s) if r.file == doc.name]
		)

	def tearDown(self):
		if base.CREATED_FILES:
			frappe.db.delete(
				migration.LOG,
				{"run_id": ("like", "write-back-to-local-%"), "file": ("in", base.CREATED_FILES)},
			)
		super().tearDown()

	def test_write_back_patch_moves_r2_file_to_local(self):
		content = b"date,amount\n2026-11-17,10\n"
		doc = self.seed_write_back_file(content)
		key = key_from_url(doc.file_url)
		with self.only(doc):
			write_back_patch.execute()
			commit()
			url = frappe.db.get_value("File", doc.name, "file_url")
			self.assertTrue(url.startswith("/private/files/"), url)
			self.assertEqual(frappe.get_doc("File", doc.name).get_raw_content(), content)
			self.assertFalse(R2().exists(key, True))

			write_back_patch.execute()  # idempotent: nothing left to move
			commit()
		self.assertEqual(frappe.db.get_value("File", doc.name, "file_url"), url)
		self.assertEqual(frappe.db.count(migration.LOG, {"file": doc.name, "status": "Migrated"}), 1)

	def test_write_back_patch_logs_each_failure_and_leaves_file_in_r2(self):
		doc = self.seed_write_back_file(b"x")
		with (
			self.only(doc),
			mock.patch.object(migration, "_r2_to_local", side_effect=RuntimeError("R2 down")),
			mock.patch.object(write_back_patch.frappe, "log_error") as log_error,
		):
			write_back_patch.execute()
		log_error.assert_called_once()
		self.assertEqual(log_error.call_args.kwargs["title"], write_back_patch.ERROR_TITLE)
		self.assertEqual(log_error.call_args.kwargs["reference_name"], doc.name)
		self.assertEqual(frappe.db.get_value("File", doc.name, "file_url"), doc.file_url)

	# -- settings patch clears the cached settings ------------------------------------------

	def test_set_cache_defaults_clears_cached_settings(self):
		with mock.patch.object(set_cache_defaults.frappe, "clear_document_cache") as clear:
			set_cache_defaults.execute()
		clear.assert_called_once_with(set_cache_defaults.SETTINGS, set_cache_defaults.SETTINGS)


class TestBucketFromKey(R2TestCase):
	"""Rows whose is_private changed before 1.0.1: the key, not the row, names the bucket."""

	def toggled_file(self, content):
		configure(public_bucket_name=base.PUBLIC_BUCKET)  # a wrong-bucket read would 404
		doc = make_file(f"toggled-{frappe.generate_hash(length=6)}.txt", content)
		commit()
		frappe.db.set_value("File", doc.name, "is_private", 0, update_modified=False)
		commit()
		doc = frappe.get_doc("File", doc.name)
		self.assertEqual(key_is_private(key_from_url(doc.file_url)), 1)
		self.assertEqual(doc.is_private, 0)
		return doc

	def test_toggled_row_reads_caches_and_deletes_in_the_keys_bucket(self):
		import hashlib

		content = frappe.generate_hash().encode()
		doc = self.toggled_file(content)
		key = key_from_url(doc.file_url)
		self.assertEqual(doc.get_raw_content(), content)
		self.assertEqual(doc.get_content(), content.decode())
		path = doc.get_full_path()
		private_name = hashlib.sha256(f"private:{key}".encode()).hexdigest()
		self.assertTrue(os.path.basename(path).startswith(private_name))
		with open(path, "rb") as fh:
			self.assertEqual(fh.read(), content)

		frappe.delete_doc("File", doc.name)
		commit()
		r2 = R2()
		self.assertFalse(r2.exists(key, True))  # moved to trash in the private bucket
		# list, don't rebuild trash_key(): its stamp is per second, so it changes if the clock ticks
		trashed = [o["Key"] for o in r2._iter_objects(base.R2_BUCKET, r2.trash_prefix()) if o["Key"].endswith(key)]
		self.assertEqual(len(trashed), 1)
		self.assertFalse(os.path.exists(path))

	def test_r2_to_local_puts_a_private_key_under_private_files(self):
		from frappe.utils import CallbackManager

		from frappe_cloudflare_r2.storage import get_settings

		content = frappe.generate_hash().encode()
		doc = self.toggled_file(content)
		row = frappe._dict(file_url=doc.file_url, is_private=0, file_name=doc.file_name, file=doc.name)
		cleanup = CallbackManager()  # not run: the File row still points at R2 in this test
		new_url, _extra = migration._r2_to_local(row, R2(), get_settings(), after_commit=cleanup)
		path = controller.local_path(new_url)
		try:
			self.assertTrue(new_url.startswith("/private/files/"), new_url)
			private_files = os.path.abspath(frappe.get_site_path("private", "files"))
			self.assertEqual(os.path.dirname(os.path.abspath(path)), private_files)
			with open(path, "rb") as fh:
				self.assertEqual(fh.read(), content)
			self.assertEqual(frappe.db.get_value("File", doc.name, "is_private"), 1)
		finally:
			os.remove(path)

	def test_key_without_a_side_falls_back_to_the_row_flag(self):
		from frappe_cloudflare_r2.storage import DOWNLOAD_PREFIX, side_for

		self.assertEqual(side_for("yardi/reports/2026/rent-roll.pdf", 1), 1)
		self.assertEqual(side_for("yardi/reports/2026/rent-roll.pdf", 0), 0)
		self.assertEqual(side_for("site/private/x.pdf", 0), 1)
		self.assertEqual(side_for("site/public/x.pdf", 1), 0)
		for is_private in (1, 0):
			doc = frappe.get_doc(
				{
					"doctype": "File",
					"file_url": DOWNLOAD_PREFIX + "yardi/reports/rent-roll.pdf",
					"file_name": "rent-roll.pdf",
					"is_private": is_private,
				}
			)
			with mock.patch.object(R2, "get_bytes", return_value=b"%PDF") as get_bytes:
				doc.get_raw_content()
			self.assertEqual(get_bytes.call_args[0], ("yardi/reports/rent-roll.pdf", is_private))

	def test_compat_checks_installed_apps_before_querying(self):
		from frappe_cloudflare_r2 import compat

		with (
			mock.patch.object(compat.frappe, "get_installed_apps", return_value=["frappe"]),
			mock.patch.object(compat.frappe.db, "sql") as sql,
		):
			self.assertIsNone(compat.r2_file("anything.pdf"))
		sql.assert_not_called()

	def test_write_back_patch_undoes_a_failure_after_download(self):
		doc = make_file(f"statement-{frappe.generate_hash(length=6)}.csv", b"a,b\n")
		commit()
		frappe.db.set_value(
			"File", doc.name, {"attached_to_doctype": "Bank Statement Import"}, update_modified=False
		)
		commit()
		key = key_from_url(doc.file_url)
		original = write_back_patch._candidates
		with (
			mock.patch.object(
				write_back_patch, "_candidates", lambda s: [r for r in original(s) if r.file == doc.name]
			),
			mock.patch.object(write_back_patch, "_repoint", side_effect=RuntimeError("log insert failed")),
			mock.patch.object(write_back_patch.frappe, "log_error") as log_error,
			mock.patch("builtins.print") as printed,
		):
			write_back_patch.execute()
		commit()
		log_error.assert_called_once()
		self.assertIn("moved 0, failed 1; see Error Log", printed.call_args[0][0])
		self.assertEqual(frappe.db.get_value("File", doc.name, "file_url"), doc.file_url)
		self.assertTrue(R2().exists(key, True))  # clean-up was never queued
		private_files = frappe.get_site_path("private", "files")
		self.assertFalse(
			any(name.startswith(os.path.splitext(doc.file_name)[0]) for name in os.listdir(private_files))
		)
