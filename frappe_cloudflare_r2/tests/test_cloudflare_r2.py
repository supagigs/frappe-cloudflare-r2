# Copyright (c) 2026, Supagigs and contributors
# For license information, please see license.txt
"""
Integration tests against a local S3-compatible server (moto) standing in for R2 and S3.

    bench --site test_site run-tests --app frappe_cloudflare_r2

Requires `pip install "moto[server]"` in the bench env.

Safe on a site that already uses R2: the site's Cloudflare R2 Settings are saved first and put back
at the end, and only the rows these tests create are removed (see remove_created_records). Tests marked
@site_wide run a migration, retry or purge over every File or log row on the site, so they only
run in CI or where site_config.json sets "allow_site_wide_r2_tests": 1 (a disposable site).
"""

import contextlib
import datetime
import functools
import os
from unittest import mock
from urllib.parse import parse_qs, urlparse

import boto3
import frappe
import requests
from botocore.exceptions import ClientError
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime, time_diff_in_seconds

from frappe_cloudflare_r2 import controller, file_log, migration
from frappe_cloudflare_r2.file_log import deferred_uploads
from frappe_cloudflare_r2.storage import (
	DOWNLOAD_PREFIX,
	R2,
	is_local_url,
	key_from_url,
	legacy_s3_key,
)

R2_BUCKET, PUBLIC_BUCKET, S3_BUCKET = "r2-private", "r2-public", "legacy-s3"
SETTINGS = "Cloudflare R2 Settings"
TEST_FOLDER = "testsite"
CREATED_FILES, CREATED_TODOS = [], []


def setUpModule():
	global SAVED_SETTINGS, STARTED
	SAVED_SETTINGS = snapshot_settings()
	STARTED = now_datetime()

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
	try:
		remove_created_records()
	finally:
		restore_settings(SAVED_SETTINGS)
		SERVER.stop()


def snapshot_settings():
	"""The site's own R2 settings rows, encrypted secrets included, exactly as stored."""
	singles = frappe.db.sql("select field, value from `tabSingles` where doctype=%s", SETTINGS)
	auth = frappe.db.sql(
		"select fieldname, password, encrypted from `__Auth` where doctype=%s and name=%s",
		(SETTINGS, SETTINGS),
	)
	return singles, auth


def restore_settings(snapshot):
	singles, auth = snapshot
	frappe.db.rollback()
	frappe.db.sql("delete from `tabSingles` where doctype=%s", SETTINGS)
	for field, value in singles:
		frappe.db.sql(
			"insert into `tabSingles` (doctype, field, value) values (%s, %s, %s)", (SETTINGS, field, value)
		)
	frappe.db.sql("delete from `__Auth` where doctype=%s and name=%s", (SETTINGS, SETTINGS))
	for fieldname, password, encrypted in auth:
		frappe.db.sql(
			"insert into `__Auth` (doctype, name, fieldname, password, encrypted) values (%s, %s, %s, %s, %s)",
			(SETTINGS, SETTINGS, fieldname, password, encrypted),
		)
	frappe.db.commit()
	frappe.clear_document_cache(SETTINGS, SETTINGS)


def remove_created_records():
	"""Delete what these tests made: File and ToDo rows, server copies, comments, log and error rows.

	Rows are deleted directly (no hooks), so nothing is sent to the site's real bucket. Log and error
	rows are matched on this run's start time and the test folder, records and bucket only.
	"""
	frappe.db.rollback()
	for name in CREATED_FILES:
		url = frappe.db.get_value("File", name, "file_url")
		frappe.db.delete("File", {"name": name})
		if url and is_local_url(url) and not frappe.db.exists("File", {"file_url": url}):
			with contextlib.suppress(OSError):
				os.remove(controller.local_path(url))
	if CREATED_TODOS:
		frappe.db.delete("ToDo", {"name": ("in", CREATED_TODOS)})
		frappe.db.delete("Comment", {"reference_doctype": "ToDo", "reference_name": ("in", CREATED_TODOS)})

	Log = frappe.qb.DocType("R2 File Log")
	ours = Log.r2_key.like(f"{TEST_FOLDER}/%")
	if CREATED_FILES:
		ours = ours | Log.file.isin(CREATED_FILES)
	# modified, not creation: some tests backdate creation, and set_value moves modified to now
	frappe.qb.from_(Log).delete().where((Log.modified >= STARTED) & ours).run()

	Err = frappe.qb.DocType("Error Log")
	frappe.qb.from_(Err).delete().where(
		(Err.creation >= STARTED)
		& Err.method.like("Cloudflare R2:%")
		& (Err.method.like(f"%{TEST_FOLDER}/%") | Err.error.like("%bucket-that-does-not-exist%"))
	).run()
	frappe.db.commit()


def site_wide(test):
	"""Skip a test that migrates, retries or purges every File or log row on the site."""

	@functools.wraps(test)
	def wrapper(self, *args, **kwargs):
		if not (os.environ.get("CI") or frappe.conf.get("allow_site_wide_r2_tests")):
			self.skipTest("acts on every File on the site; set allow_site_wide_r2_tests on a disposable site")
		return test(self, *args, **kwargs)

	return wrapper


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
		folder_name=TEST_FOLDER,
		ignored_doctypes="Data Import",
		delete_local_after_upload=1,
		keep_file_on_upload_failure=0,
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
	CREATED_FILES.append(doc.name)
	return doc


def make_todo():
	todo = frappe.get_doc({"doctype": "ToDo", "description": "x"}).insert()
	CREATED_TODOS.append(todo.name)
	return todo


def commit():
	frappe.db.commit()
	# v16's test runner does not fire after_commit callbacks on commit(); run them explicitly
	frappe.db.after_commit.run()


class R2TestCase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# moto storage is in-memory, so forget R2 files left in the DB by earlier runs.
		# Only this suite's own keys: the site may hold real R2 files.
		for pattern in (
			f"{DOWNLOAD_PREFIX}{TEST_FOLDER}/%",
			"https://files.example.com/%",
			"/api/method/frappe_s3_attachment.controller.generate_file?key=2024/01/01/File/%",
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
		# committed, so a test's rollback can never fall back to the site's real settings
		frappe.db.commit()
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.set_user("Administrator")
		configure(enabled=0)
		commit()


class TestCloudflareR2(R2TestCase):
	# -- uploads ---------------------------------------------------------------

	def test_private_upload_goes_to_r2_and_local_copy_removed(self):
		name = f"r2-private-{frappe.generate_hash(length=6)}.txt"
		doc = make_file(name, b"secret bytes")
		local = frappe.get_site_path("private", "files", name)
		commit()

		self.assertTrue(doc.file_url.startswith(DOWNLOAD_PREFIX))
		key = key_from_url(doc.file_url)
		self.assertTrue(key.startswith("testsite/private/"))
		self.assertEqual(R2().get_bytes(key), b"secret bytes")
		self.assertEqual(frappe.db.get_value("File", doc.name, "file_url"), doc.file_url)
		self.assertFalse(os.path.exists(local))
		# content still readable through the File API (email attachments etc.)
		self.assertEqual(frappe.get_doc("File", doc.name).get_content(), "secret bytes")

	def test_raw_content_returns_exact_bytes(self):
		pdf = b"\x00\xff%PDF-like binary \xe2\xe3\xcf\xd3" + os.urandom(64)
		doc = make_file("contract.bin", pdf)
		commit()
		self.assertTrue(key_from_url(doc.file_url))
		fresh = frappe.get_doc("File", doc.name)
		self.assertEqual(fresh.get_raw_content(), pdf)
		self.assertIsInstance(fresh.get_raw_content(), bytes)

	def test_public_upload_uses_public_domain_and_bucket(self):
		configure(public_base_url="https://files.example.com", public_bucket_name=PUBLIC_BUCKET)
		doc = make_file("logo.png", is_private=0)
		commit()
		self.assertTrue(doc.file_url.startswith("https://files.example.com/testsite/public/"))
		key = key_from_url(doc.file_url)
		raw_client().head_object(Bucket=PUBLIC_BUCKET, Key=key)

	def test_ignored_doctype_stays_local(self):
		configure(ignored_doctypes="ToDo")
		todo = make_todo()
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
		name = f"disabled-{frappe.generate_hash(length=6)}.txt"
		doc = make_file(name)
		self.assertTrue(doc.file_url.startswith("/private/files/"))
		self.assertIn("disabled", self._log(name, "Skipped")[0].error)

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
		doc = make_file("to-delete.txt")
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
		doc = make_file("hard-delete.txt")
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
		todo = make_todo()
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
		CREATED_FILES.append(copy.name)
		commit()
		key = key_from_url(doc.file_url)
		doc.delete()
		commit()
		self.assertTrue(R2().exists(key))
		copy.delete()
		commit()
		self.assertFalse(R2().exists(key))

	# -- file log --------------------------------------------------------------------

	def _log(self, file_name, event):
		return frappe.get_all(
			"R2 File Log",
			filters={"file_name": file_name, "event": event},
			fields=[
				"name",
				"event",
				"status",
				"from_location",
				"to_location",
				"bucket",
				"r2_key",
				"size",
				"error",
				"note",
			],
			order_by="creation desc",
			limit=1,
		)

	def test_file_log_upload_skip_trash_delete(self):
		name = f"log-{frappe.generate_hash(length=6)}.txt"
		doc = make_file(name, b"logged")
		commit()
		up = self._log(name, "Uploaded")[0]
		self.assertEqual(up.status, "Success")
		self.assertTrue(up.from_location.startswith("/private/files/"))
		self.assertEqual(up.to_location, f"r2://{R2_BUCKET}/{key_from_url(doc.file_url)}")
		self.assertEqual(up.size, 6)

		doc.delete()
		commit()
		trash = self._log(name, "Moved to Trash")[0]
		self.assertIn("/_trash/", trash.to_location)

		configure(trash_retention_days=0)
		name2 = f"log-{frappe.generate_hash(length=6)}.txt"
		doc2 = make_file(name2)
		commit()
		doc2.delete()
		commit()
		self.assertEqual(self._log(name2, "Deleted")[0].status, "Success")

		configure(ignored_doctypes="ToDo")
		todo = make_todo()
		name3 = f"log-{frappe.generate_hash(length=6)}.txt"
		make_file(name3, attached_to_doctype="ToDo", attached_to_name=todo.name)
		commit()
		self.assertIn("Keep Local", self._log(name3, "Skipped")[0].error)

	def test_file_log_records_failed_upload_after_rollback(self):
		configure(bucket_name="bucket-that-does-not-exist")
		name = f"fail-{frappe.generate_hash(length=6)}.txt"
		with self.assertRaises(frappe.ValidationError):
			make_file(name)
		frappe.db.rollback()
		configure()
		failed = self._log(name, "Upload Failed")
		self.assertTrue(failed)
		self.assertEqual(failed[0].status, "Failed")
		self.assertIn("NoSuchBucket", failed[0].error)

	def test_failed_delete_after_rollback_is_logged_as_kept(self):
		# A bucket lock refuses the delete, so the object must be logged as kept, not removed.
		class LockedR2(R2):
			def __init__(self, settings=None):
				super().__init__(settings)
				self.client = mock.Mock(wraps=self.client)
				self.client.delete_object.side_effect = ClientError(
					{"Error": {"Code": "AccessDenied", "Message": "object is locked"}}, "DeleteObject"
				)

		name = f"locked-{frappe.generate_hash(length=6)}.txt"
		with mock.patch.object(controller, "R2", LockedR2):
			make_file(name, b"locked bytes")
			frappe.db.rollback()
		kept = self._log(name, "Kept")
		self.assertTrue(kept)
		self.assertEqual(kept[0].status, "Failed")
		self.assertIn("AccessDenied", kept[0].error)
		self.assertIn("kept", kept[0].note)
		self.assertFalse(self._log(name, "Rolled Back"))
		# the object is still in the bucket
		raw_client().head_object(Bucket=R2_BUCKET, Key=kept[0].r2_key)

	@site_wide
	def test_deferred_upload_retries_after_outage_and_reports_age(self):
		configure(keep_file_on_upload_failure=1, bucket_name="bucket-that-does-not-exist")
		name = f"deferred-{frappe.generate_hash(length=6)}.txt"
		# unique bytes: Frappe de-duplicates Files by content, and committed rows persist between runs
		content = f"deferred bytes {name}".encode()
		doc = make_file(name, content)
		commit()

		# the email is kept: the File still points at the server copy, no exception was raised
		self.assertTrue(doc.file_url.startswith("/private/files/"))
		self.assertTrue(os.path.isfile(frappe.get_site_path("private", "files", name)))
		row = self._log(name, "Upload Deferred")[0]
		self.assertEqual(row.status, "Deferred")
		self.assertEqual(row.from_location, doc.file_url)
		self.assertTrue(row.r2_key)

		# the age is reported, and a 60 minute alert only lists files that have waited that long
		self.assertEqual([d["file"] for d in deferred_uploads()].count(doc.name), 1)
		self.assertEqual(frappe.db.get_value("R2 File Log", row.name, "status"), "Deferred")
		self.assertFalse([d for d in deferred_uploads(older_than_minutes=60) if d["file"] == doc.name])
		frappe.db.set_value("R2 File Log", row.name, "creation", add_to_date(now_datetime(), minutes=-90))
		commit()
		old = [d for d in deferred_uploads(older_than_minutes=60) if d["file"] == doc.name]
		self.assertEqual(len(old), 1)
		self.assertGreaterEqual(old[0]["age_minutes"], 89)

		# still down: the retry leaves the file deferred
		controller.retry_deferred_uploads()
		commit()
		self.assertEqual(frappe.db.get_value("File", doc.name, "file_url"), doc.file_url)
		self.assertEqual(frappe.db.get_value("R2 File Log", row.name, "status"), "Deferred")

		# outage over: the next retry uploads it under the key chosen at deferral
		configure(keep_file_on_upload_failure=1, bucket_name=R2_BUCKET)
		controller.retry_deferred_uploads()
		commit()
		new_url = frappe.db.get_value("File", doc.name, "file_url")
		self.assertTrue(new_url.startswith(DOWNLOAD_PREFIX))
		self.assertEqual(key_from_url(new_url), row.r2_key)
		body = raw_client().get_object(Bucket=R2_BUCKET, Key=row.r2_key)["Body"].read()
		self.assertEqual(body, content)
		self.assertEqual(frappe.db.get_value("R2 File Log", row.name, "status"), "Success")
		self.assertEqual(self._log(name, "Uploaded")[0].note, "Uploaded on retry after an R2 outage")
		self.assertFalse([d for d in deferred_uploads() if d["file"] == doc.name])

	def test_file_log_purge(self):
		doc = make_file(f"purge-{frappe.generate_hash(length=6)}.txt")
		commit()
		key = key_from_url(doc.file_url)
		doc.delete()
		commit()
		migration.purge_r2_trash(older_than_days=-1)  # everything in trash counts as expired
		self.assertTrue(frappe.db.exists("R2 File Log", {"event": "Purged", "r2_key": ("like", f"%{key}")}))

	@site_wide
	def test_file_log_purge_keeps_deferred_rows(self):
		frappe.db.set_single_value("Cloudflare R2 Settings", "file_log_retention_days", 180)
		tag = frappe.generate_hash(length=6)
		url = f"/private/files/purge-deferred-{tag}.txt"
		old = add_to_date(now_datetime(), days=-200)

		file_log.log("Upload Deferred", status="Deferred", from_location=url, r2_key=f"testsite/{tag}.txt")
		file_log.log("Uploaded", status="Success", from_location=url, r2_key=f"testsite/{tag}-done.txt")
		rows = {
			r.event: r.name
			for r in frappe.get_all(
				"R2 File Log", filters={"from_location": url}, fields=["name", "event"]
			)
		}
		self.assertEqual(set(rows), {"Upload Deferred", "Uploaded"})
		for name in rows.values():
			frappe.db.set_value("R2 File Log", name, "creation", old)
		commit()

		file_log.purge_old_logs()
		commit()

		# the 200-day-old deferred row survives and is still listed for retry
		self.assertEqual(frappe.db.get_value("R2 File Log", rows["Upload Deferred"], "status"), "Deferred")
		self.assertTrue([d for d in deferred_uploads(older_than_minutes=60) if d["file_url"] == url])
		# the 200-day-old non-deferred row is purged
		self.assertFalse(frappe.db.exists("R2 File Log", rows["Uploaded"]))

	def test_deferred_uploads_one_entry_per_file_with_oldest_deferral(self):
		tag = frappe.generate_hash(length=6)
		url = f"/private/files/dedupe-{tag}.txt"
		file_log.log("Upload Deferred", status="Deferred", from_location=url, r2_key=f"testsite/{tag}-a.txt")
		file_log.log("Upload Deferred", status="Deferred", from_location=url, r2_key=f"testsite/{tag}-b.txt")
		names = frappe.get_all(
			"R2 File Log", filters={"from_location": url}, pluck="name", order_by="creation asc"
		)
		self.assertEqual(len(names), 2)
		older = add_to_date(now_datetime(), minutes=-120)
		frappe.db.set_value("R2 File Log", names[0], "creation", older)
		frappe.db.set_value("R2 File Log", names[1], "creation", add_to_date(now_datetime(), minutes=-10))
		commit()

		entries = [d for d in deferred_uploads() if d["file_url"] == url]
		self.assertEqual(len(entries), 1)
		self.assertLess(abs(time_diff_in_seconds(entries[0]["deferred_at"], older)), 1)
		self.assertGreaterEqual(entries[0]["age_minutes"], 119)
		self.assertEqual(entries[0]["log"], names[0])
		self.assertEqual(len([d for d in deferred_uploads(older_than_minutes=60) if d["file_url"] == url]), 1)

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

	@site_wide
	def test_local_to_r2_migration_repoints_references(self):
		configure(enabled=0)
		pub_name = f"mig-public-{frappe.generate_hash(length=6)}.png"
		f1 = make_file(pub_name, b"img", is_private=0)
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
		self.assertFalse(os.path.exists(frappe.get_site_path("public", "files", pub_name)))
		self.assertEqual(R2().get_bytes(key_from_url(new_private)), b"doc")

	@site_wide
	def test_s3_to_r2_migration_rewrites_links(self):
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

		note = frappe.get_doc(
			{
				"doctype": "Note",
				"title": frappe.generate_hash(length=6),
				"content": f'<a href="{private_url}">contract</a>',
			}
		).insert()
		commit()

		self.assertEqual(legacy_s3_key(private_url), "2024/01/01/File/ABCD1234_old.txt")
		self.assertEqual(legacy_s3_key(public_url), "2024/01/01/File/PUB_old.png")

		configure(enabled=1, rewrite_text_links=0)
		state = self._run("s3_to_r2")
		self.assertEqual(state["failed"], 0)
		new_private = frappe.db.get_value("File", fp.name, "file_url")
		self.assertEqual(R2().get_bytes(key_from_url(new_private)), b"from s3")
		self.assertTrue(key_from_url(frappe.db.get_value("File", fpub.name, "file_url")))

		# old S3 links inside rich text are rewritten to the R2 copy
		self.assertIn(f'href="{new_private}"', frappe.db.get_value("Note", note.name, "content"))

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

	@site_wide
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


# -- reading R2 files through a disk path (RCA-2) -----------------------------------


def png_bytes(size=(40, 30)):
	from io import BytesIO

	from PIL import Image

	buf = BytesIO()
	Image.new("RGB", size, color=tuple(os.urandom(3))).save(buf, format="PNG")
	return buf.getvalue()


def zip_bytes(members):
	from io import BytesIO
	from zipfile import ZipFile

	buf = BytesIO()
	with ZipFile(buf, "w") as z:
		for name, content in members.items():
			z.writestr(name, content)
	return buf.getvalue()


class TempPaths:
	"""Records every temporary file local_copy() creates."""

	def __init__(self):
		self.paths = []

	def mkstemp(self, *args, **kwargs):
		import tempfile

		fd, path = tempfile.mkstemp(*args, **kwargs)
		self.paths.append(path)
		return fd, path

	def patch(self):
		# replaces the module reference in overrides/file.py only, not the global tempfile module
		return mock.patch("frappe_cloudflare_r2.overrides.file.tempfile", self)


class TestLocalFileAccess(R2TestCase):
	def r2_file(self, name, content, is_private=1):
		doc = make_file(name, content, is_private=is_private)
		commit()
		self.assertTrue(key_from_url(doc.file_url))
		return frappe.get_doc("File", doc.name)

	# -- local_copy -------------------------------------------------------------------

	def test_local_copy_of_r2_file_is_private_temp_file_removed_after(self):
		import stat
		import tempfile

		content = png_bytes()
		doc = self.r2_file(f"copy-{frappe.generate_hash(length=6)}.png", content)
		with doc.local_copy() as path:
			self.assertTrue(path.endswith(".png"))
			self.assertEqual(os.path.dirname(path), tempfile.gettempdir())
			self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
			with open(path, "rb") as fh:
				self.assertEqual(fh.read(), content)
		self.assertFalse(os.path.exists(path))

	def test_local_copy_of_local_file_is_its_own_path_and_kept(self):
		configure(enabled=0)
		content = frappe.generate_hash().encode()
		doc = make_file(f"local-{frappe.generate_hash(length=6)}.txt", content)
		self.assertTrue(doc.file_url.startswith("/private/files/"))
		with frappe.get_doc("File", doc.name).local_copy() as path:
			self.assertEqual(os.path.realpath(path), os.path.realpath(controller.local_path(doc.file_url)))
			with open(path, "rb") as fh:
				self.assertEqual(fh.read(), content)
		self.assertTrue(os.path.exists(path))

	def test_local_copy_removed_when_the_block_raises(self):
		doc = self.r2_file("raises.txt", frappe.generate_hash().encode())
		with self.assertRaises(ZeroDivisionError):
			with doc.local_copy() as path:
				self.assertTrue(os.path.exists(path))
				1 / 0  # noqa: B018
		self.assertFalse(os.path.exists(path))

	def test_local_copy_removed_when_the_download_fails(self):
		doc = self.r2_file("download-fails.txt", frappe.generate_hash().encode())
		temp = TempPaths()
		with (
			temp.patch(),
			mock.patch.object(R2, "download_fileobj", side_effect=ClientError({}, "GetObject")),
		):
			with self.assertRaises(ClientError):
				with doc.local_copy():
					self.fail("the block must not run")
		self.assertEqual(len(temp.paths), 1)
		self.assertFalse(os.path.exists(temp.paths[0]))

	# -- get_full_path: a clear error instead of the URL ---------------------------------

	def test_get_full_path_on_r2_file_raises_clear_error(self):
		from frappe_cloudflare_r2.overrides.file import R2FileNotOnDiskError

		configure(public_base_url="https://files.example.com", public_bucket_name=PUBLIC_BUCKET)
		for is_private in (1, 0):
			doc = self.r2_file(f"path-{is_private}.txt", frappe.generate_hash().encode(), is_private)
			with self.assertRaises(R2FileNotOnDiskError) as cm:
				open(doc.get_full_path(), "rb")  # the pattern that failed with [Errno 2]
			self.assertIsInstance(cm.exception, frappe.ValidationError)
			self.assertIn("stored in Cloudflare R2, not on disk", str(cm.exception))
			self.assertIn("get_raw_content()", str(cm.exception))
			self.assertIn("local_copy()", str(cm.exception))

	def test_get_full_path_of_local_file_unchanged(self):
		configure(enabled=0)
		doc = make_file(f"plain-{frappe.generate_hash(length=6)}.txt")
		path = frappe.get_doc("File", doc.name).get_full_path()
		self.assertEqual(os.path.realpath(path), os.path.realpath(controller.local_path(doc.file_url)))

	# -- core File flows still work for R2 files ------------------------------------------

	def test_validate_and_save_r2_file(self):
		doc = self.r2_file("save-me.txt", frappe.generate_hash().encode())
		self.assertTrue(doc.exists_on_disk())
		doc.file_name = "renamed.txt"
		doc.save()  # validate() calls validate_file_on_disk() -> get_full_path() in core
		commit()
		self.assertEqual(frappe.db.get_value("File", doc.name, "file_name"), "renamed.txt")

	def test_delete_r2_file_loaded_fresh_moves_object_to_trash(self):
		doc = self.r2_file("delete-fresh.txt", frappe.generate_hash().encode())
		key = key_from_url(doc.file_url)
		frappe.delete_doc("File", doc.name)
		commit()
		self.assertFalse(frappe.db.exists("File", doc.name))
		r2 = R2()
		self.assertFalse(r2.exists(key))
		self.assertTrue([o for o in r2._iter_objects(R2_BUCKET, r2.trash_prefix()) if o["Key"].endswith(key)])

	def test_rollback_after_upload_removes_object_and_server_copy(self):
		doc = make_file(f"rollback-{frappe.generate_hash(length=6)}.txt", frappe.generate_hash().encode())
		key = key_from_url(doc.file_url)
		server_copy = frappe.get_site_path("private", "files", doc.file_name)
		self.assertTrue(os.path.exists(server_copy))
		self.assertTrue(R2().exists(key))
		frappe.db.rollback()
		self.assertFalse(frappe.db.exists("File", doc.name))
		self.assertFalse(R2().exists(key))
		self.assertFalse(os.path.exists(server_copy))
		self.assertEqual(key_from_url(doc.file_url), key)  # the in-memory doc is left as it was

	# -- thumbnails, optimisation, unzip ------------------------------------------------------

	def test_thumbnail_skipped_for_private_r2_image(self):
		doc = self.r2_file(f"thumb-{frappe.generate_hash(length=6)}.png", png_bytes())
		with mock.patch("frappe.core.doctype.file.file.get_web_image") as fetch:
			self.assertIsNone(doc.make_thumbnail())
		fetch.assert_not_called()
		self.assertFalse(frappe.db.get_value("File", doc.name, "thumbnail_url"))

	def test_optimize_refused_for_r2_image(self):
		content = png_bytes((400, 300))
		doc = self.r2_file(f"optimize-{frappe.generate_hash(length=6)}.png", content)
		with self.assertRaises(frappe.ValidationError) as cm:
			doc.optimize_file()
		self.assertIn("not available for files stored in Cloudflare R2", str(cm.exception))
		self.assertEqual(frappe.get_doc("File", doc.name).get_raw_content(), content)

	def test_unzip_r2_zip_through_temporary_copy(self):
		members = {"a.txt": frappe.generate_hash().encode(), "b.bin": os.urandom(32)}
		doc = self.r2_file(f"bundle-{frappe.generate_hash(length=6)}.zip", zip_bytes(members))
		temp = TempPaths()
		with temp.patch():
			children = doc.unzip()
		CREATED_FILES.extend(c.name for c in children)
		commit()
		self.assertEqual({c.file_name for c in children}, set(members))
		for child in children:
			fresh = frappe.get_doc("File", child.name)
			self.assertTrue(key_from_url(fresh.file_url))
			self.assertEqual(fresh.get_raw_content(), members[child.file_name])
		self.assertFalse(frappe.db.exists("File", doc.name))
		self.assertEqual(len(temp.paths), 1)
		self.assertFalse(os.path.exists(temp.paths[0]))
		self.assertFalse(doc.flags.r2_local_path)

	# -- the GrowthOS pattern ---------------------------------------------------------------

	def test_growthos_image_read_through_local_copy_and_raw_content(self):
		from io import BytesIO

		from PIL import Image

		doc = self.r2_file(f"post-{frappe.generate_hash(length=6)}.png", png_bytes((64, 48)))
		with doc.local_copy() as path:
			with Image.open(path) as image:
				image.load()
				self.assertEqual(image.size, (64, 48))
		with Image.open(BytesIO(doc.get_raw_content())) as image:
			self.assertEqual(image.size, (64, 48))
