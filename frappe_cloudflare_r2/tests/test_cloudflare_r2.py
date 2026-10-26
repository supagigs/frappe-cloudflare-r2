# Copyright (c) 2026, Supagigs and contributors
# For license information, please see license.txt
"""
Integration tests against a local S3-compatible server (moto) standing in for R2 and S3.

    bench --site test_site run-tests --app frappe_cloudflare_r2

Requires `pip install "moto[server]"` in the bench env.
"""

import datetime
import os
from urllib.parse import parse_qs, urlparse

import boto3
import frappe
import requests
from frappe.tests.utils import FrappeTestCase

from frappe_cloudflare_r2 import controller, migration
from frappe_cloudflare_r2.storage import (
	DOWNLOAD_PREFIX,
	R2,
	key_from_url,
	legacy_s3_key,
)

R2_BUCKET, PUBLIC_BUCKET, S3_BUCKET = "r2-private", "r2-public", "legacy-s3"


def setUpModule():
	from moto.server import ThreadedMotoServer

	global SERVER, ENDPOINT
	SERVER = ThreadedMotoServer(port=0, verbose=False)
	SERVER.start()
	host, port = SERVER.get_host_and_port()
	ENDPOINT = f"http://{host}:{port}"
	os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
	import logging

	logging.getLogger("werkzeug").setLevel(logging.ERROR)


def tearDownModule():
	SERVER.stop()


def raw_client():
	return boto3.client(
		"s3", endpoint_url=ENDPOINT, aws_access_key_id="k", aws_secret_access_key="s", region_name="us-east-1"
	)


def configure(**overrides):
	values = dict(
		enabled=1,
		account_id="test-account",
		endpoint_url=ENDPOINT,
		bucket_name=R2_BUCKET,
		public_bucket_name="",
		access_key_id="k",
		public_base_url="",
		folder_name="testsite",
		ignored_doctypes="Data Import",
		delete_local_after_upload=1,
		delete_from_r2_on_trash=1,
		trash_retention_days=30,
		trash_prefix="_trash",
		signed_url_expiry=120,
		multipart_threshold_mb=100,
		multipart_chunksize_mb=16,
		max_concurrency=4,
		rewrite_text_links=1,
		s3_bucket_name=S3_BUCKET,
		s3_region="us-east-1",
		s3_endpoint_url=ENDPOINT,
		s3_access_key_id="k",
		s3_source_retention_days=30,
	)
	values.update(overrides)
	# db writes bypass validate(), which (correctly) insists on https endpoints
	for k, v in values.items():
		frappe.db.set_single_value("Cloudflare R2 Settings", k, v)
	from frappe.utils.password import set_encrypted_password

	set_encrypted_password("Cloudflare R2 Settings", "Cloudflare R2 Settings", "s", "secret_access_key")
	set_encrypted_password("Cloudflare R2 Settings", "Cloudflare R2 Settings", "s", "s3_secret_access_key")
	frappe.clear_document_cache("Cloudflare R2 Settings", "Cloudflare R2 Settings")


def make_file(name, content=None, is_private=1, **kw):
	content = content if content is not None else frappe.generate_hash().encode()
	doc = frappe.get_doc(
		{"doctype": "File", "file_name": name, "content": content, "is_private": is_private, **kw}
	).insert(ignore_permissions=True)
	return doc


def commit():
	frappe.db.commit()  # runs after_commit hooks (local delete, R2 delete)


class TestCloudflareR2(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# moto storage is in-memory, so forget R2 files left in the DB by earlier runs
		for pattern in (
			DOWNLOAD_PREFIX + "%",
			"https://files.example.com/%",
			"/api/method/frappe_s3_attachment%",
		):
			frappe.db.delete("File", {"file_url": ("like", pattern)})
		frappe.db.commit()
		c = raw_client()
		for b in (R2_BUCKET, PUBLIC_BUCKET, S3_BUCKET):
			try:
				c.create_bucket(Bucket=b)
			except c.exceptions.BucketAlreadyOwnedByYou:
				pass

	def setUp(self):
		configure()
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.set_user("Administrator")
		configure(enabled=0)
		commit()

	# -- uploads ---------------------------------------------------------------

	def test_private_upload_goes_to_r2_and_local_copy_removed(self):
		doc = make_file("r2-private.txt", b"secret bytes")
		local = frappe.get_site_path("private", "files", "r2-private.txt")
		commit()

		self.assertTrue(doc.file_url.startswith(DOWNLOAD_PREFIX))
		key = key_from_url(doc.file_url)
		self.assertTrue(key.startswith("testsite/private/"))
		self.assertEqual(R2().get_bytes(key), b"secret bytes")
		self.assertEqual(frappe.db.get_value("File", doc.name, "file_url"), doc.file_url)
		self.assertFalse(os.path.exists(local))
		# content still readable through the File API (email attachments etc.)
		self.assertEqual(frappe.get_doc("File", doc.name).get_content(), "secret bytes")

	def test_public_upload_uses_public_domain_and_bucket(self):
		configure(public_base_url="https://files.example.com", public_bucket_name=PUBLIC_BUCKET)
		doc = make_file("logo.png", is_private=0)
		commit()
		self.assertTrue(doc.file_url.startswith("https://files.example.com/testsite/public/"))
		key = key_from_url(doc.file_url)
		raw_client().head_object(Bucket=PUBLIC_BUCKET, Key=key)

	def test_ignored_doctype_stays_local(self):
		configure(ignored_doctypes="ToDo")
		todo = frappe.get_doc({"doctype": "ToDo", "description": "x"}).insert()
		doc = make_file(
			"keep-local.txt",
			frappe.generate_hash().encode(),
			attached_to_doctype="ToDo",
			attached_to_name=todo.name,
		)
		commit()
		self.assertTrue(doc.file_url.startswith("/private/files/"))

	def test_disabled_does_nothing(self):
		configure(enabled=0)
		doc = make_file("disabled.txt")
		self.assertTrue(doc.file_url.startswith("/private/files/"))

	def test_multipart_upload(self):
		configure(multipart_threshold_mb=5, multipart_chunksize_mb=5)
		big = os.urandom(12 * 1024 * 1024)
		doc = make_file("big.bin", big)
		commit()
		self.assertEqual(R2().get_bytes(key_from_url(doc.file_url)), big)

	# -- downloads ---------------------------------------------------------------

	def test_download_redirects_to_working_signed_url(self):
		doc = make_file("signed.txt", b"via signed url")
		commit()
		key = key_from_url(doc.file_url)
		frappe.local.response = frappe._dict()
		controller.download_file(key=key)
		self.assertEqual(frappe.local.response["type"], "redirect")
		resp = requests.get(frappe.local.response["location"])
		self.assertEqual(resp.content, b"via signed url")
		self.assertIn("signed.txt", resp.headers.get("Content-Disposition", ""))

	def test_guest_cannot_download_private(self):
		doc = make_file("guest-blocked.txt")
		commit()
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			controller.download_file(key=key_from_url(doc.file_url))

	def test_guest_can_download_public_without_domain(self):
		doc = make_file("public-proxy.txt", b"pub", is_private=0)
		commit()
		frappe.set_user("Guest")
		frappe.local.response = frappe._dict()
		controller.download_file(key=key_from_url(doc.file_url))
		self.assertEqual(requests.get(frappe.local.response["location"]).content, b"pub")

	def test_unknown_key_404(self):
		with self.assertRaises(frappe.DoesNotExistError):
			controller.download_file(key="testsite/private/nope")

	# -- deletion & retention ----------------------------------------------------

	def test_delete_moves_to_trash_then_purge(self):
		doc = make_file("to-delete.txt", b"bye")
		commit()
		key = key_from_url(doc.file_url)
		doc.delete()
		commit()
		r2 = R2()
		self.assertFalse(r2.exists(key))
		trashed = [o["Key"] for o in r2._iter_objects(R2_BUCKET, r2.trash_prefix()) if o["Key"].endswith(key)]
		self.assertEqual(len(trashed), 1)

		self.assertEqual(r2.purge_trash(30), 0)  # still inside retention window
		self.assertGreaterEqual(r2.purge_trash(-1), 1)  # everything older than "tomorrow"
		self.assertFalse(list(r2._iter_objects(R2_BUCKET, r2.trash_prefix())))

	def test_delete_immediately_when_retention_zero(self):
		configure(trash_retention_days=0)
		doc = make_file("hard-delete.txt", b"x")
		commit()
		key = key_from_url(doc.file_url)
		doc.delete()
		commit()
		r2 = R2()
		self.assertFalse(r2.exists(key))
		self.assertFalse(
			[o for o in r2._iter_objects(R2_BUCKET, r2.trash_prefix()) if o["Key"].endswith(key)]
		)

	def test_shared_object_not_deleted_while_referenced(self):
		doc = make_file("shared.txt", b"shared-content")
		commit()
		todo = frappe.get_doc({"doctype": "ToDo", "description": "x"}).insert()
		copy = frappe.get_doc(
			{
				"doctype": "File",
				"file_url": doc.file_url,
				"file_name": "shared.txt",
				"is_private": 1,
				"attached_to_doctype": "ToDo",
				"attached_to_name": todo.name,
			}
		).insert(ignore_permissions=True)
		commit()
		key = key_from_url(doc.file_url)
		doc.delete()
		commit()
		self.assertTrue(R2().exists(key))
		copy.delete()
		commit()
		self.assertFalse(R2().exists(key))

	# -- migrations ----------------------------------------------------------------

	def _run(self, direction):
		state = {
			"run_id": f"t-{direction}-{frappe.generate_hash(length=6)}",
			"direction": direction,
			"total": 0,
			"done": 0,
			"migrated": 0,
			"skipped": 0,
			"failed": 0,
		}
		migration.run_batch(state, after="", batch_no=1)
		frappe.cache.delete_value(migration.LOCK_KEY)
		return state

	def test_local_to_r2_migration_repoints_references(self):
		configure(enabled=0)
		f1 = make_file("mig-public.png", b"img", is_private=0)
		f2 = make_file("mig-private.txt", b"doc")
		old_public, old_private = f1.file_url, f2.file_url
		commit()

		# references in an Attach Image field, a Single and rich text
		lh = frappe.get_doc(
			{
				"doctype": "Letter Head",
				"letter_head_name": f"LH {frappe.generate_hash(length=5)}",
				"source": "Image",
				"image": old_public,
			}
		).insert()
		frappe.db.set_single_value("Website Settings", "banner_image", old_public)
		note = frappe.get_doc(
			{
				"doctype": "Note",
				"title": frappe.generate_hash(length=6),
				"content": f'<p><img src="{old_public}"> <a href="{old_private}">doc</a> /private{old_public}</p>',
			}
		).insert()
		commit()

		configure(enabled=1)
		state = self._run("local_to_r2")
		self.assertGreaterEqual(state["migrated"], 2)

		new_public = frappe.db.get_value("File", f1.name, "file_url")
		new_private = frappe.db.get_value("File", f2.name, "file_url")
		self.assertTrue(new_public.startswith(DOWNLOAD_PREFIX))
		self.assertEqual(frappe.db.get_value("Letter Head", lh.name, "image"), new_public)
		self.assertEqual(frappe.db.get_single_value("Website Settings", "banner_image"), new_public)
		content = frappe.db.get_value("Note", note.name, "content")
		self.assertIn(f'src="{new_public}"', content)
		self.assertIn(f'href="{new_private}"', content)
		self.assertIn(f"/private{old_public}", content)  # unquoted lookalike untouched
		self.assertFalse(os.path.exists(frappe.get_site_path("public", "files", "mig-public.png")))
		self.assertEqual(R2().get_bytes(key_from_url(new_private)), b"doc")

	def test_s3_to_r2_migration_and_legacy_links(self):
		configure(enabled=0)
		c = raw_client()
		c.put_object(
			Bucket=S3_BUCKET,
			Key="2024/01/01/File/ABCD1234_old.txt",
			Body=b"from s3",
			ContentType="text/plain",
		)
		c.put_object(Bucket=S3_BUCKET, Key="2024/01/01/File/PUB_old.png", Body=b"png")
		private_url = (
			"/api/method/frappe_s3_attachment.controller.generate_file?key=2024/01/01/File/ABCD1234_old.txt"
		)
		public_url = f"{ENDPOINT}/{S3_BUCKET}/2024/01/01/File/PUB_old.png"
		fp = frappe.get_doc(
			{"doctype": "File", "file_name": "old.txt", "file_url": private_url, "is_private": 1}
		).insert()
		fpub = frappe.get_doc(
			{"doctype": "File", "file_name": "old.png", "file_url": public_url, "is_private": 0}
		).insert()
		commit()

		self.assertEqual(legacy_s3_key(private_url), "2024/01/01/File/ABCD1234_old.txt")
		self.assertEqual(legacy_s3_key(public_url), "2024/01/01/File/PUB_old.png")

		configure(enabled=1)
		state = self._run("s3_to_r2")
		self.assertEqual(state["failed"], 0)
		new_private = frappe.db.get_value("File", fp.name, "file_url")
		self.assertEqual(R2().get_bytes(key_from_url(new_private)), b"from s3")
		self.assertTrue(key_from_url(frappe.db.get_value("File", fpub.name, "file_url")))

		# old private S3 link now resolves to R2
		frappe.local.response = frappe._dict()
		controller.legacy_s3_file(key="2024/01/01/File/ABCD1234_old.txt")
		self.assertEqual(requests.get(frappe.local.response["location"]).content, b"from s3")

		# S3 original kept during retention, deleted after it
		migration.purge_s3_sources()
		c.head_object(Bucket=S3_BUCKET, Key="2024/01/01/File/ABCD1234_old.txt")
		frappe.db.sql(
			"update `tabR2 Migration Log` set migrated_on=%s where run_id=%s",
			(datetime.datetime.now() - datetime.timedelta(days=31), state["run_id"]),
		)
		migration.purge_s3_sources()
		with self.assertRaises(c.exceptions.ClientError):
			c.head_object(Bucket=S3_BUCKET, Key="2024/01/01/File/ABCD1234_old.txt")

	def test_r2_to_local_exit_path(self):
		doc = make_file("roundtrip.txt", b"round trip")
		commit()
		key = key_from_url(doc.file_url)
		configure(enabled=0)
		state = self._run("r2_to_local")
		self.assertGreaterEqual(state["migrated"], 1)
		url = frappe.db.get_value("File", doc.name, "file_url")
		self.assertTrue(url.startswith("/private/files/"))
		self.assertEqual(frappe.get_doc("File", doc.name).get_content(), "round trip")
		self.assertFalse(R2().exists(key))  # moved to trash per retention

	def test_url_helpers(self):
		self.assertIsNone(key_from_url("/files/a.png"))
		self.assertIsNone(key_from_url("https://elsewhere.com/x"))
		self.assertEqual(key_from_url(DOWNLOAD_PREFIX + "a/b c.txt"), "a/b c.txt")
		q = parse_qs(urlparse(DOWNLOAD_PREFIX + "a/b").query)
		self.assertEqual(q["key"], ["a/b"])
