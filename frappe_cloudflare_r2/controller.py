# Copyright (c) 2026, Supagigs and contributors
# For license information, please see license.txt
"""File hooks and HTTP endpoints."""

import os
import secrets

import frappe
from frappe import _

from frappe_cloudflare_r2.file_log import log, r2_location
from frappe_cloudflare_r2.storage import (
	DEFAULT_IGNORED_DOCTYPES,
	R2,
	get_settings,
	is_enabled,
	is_local_url,
	key_from_url,
	private_url_for,
)


def ignored_doctypes(settings=None):
	settings = settings or get_settings()
	configured = [d.strip() for d in (settings.ignored_doctypes or "").splitlines() if d.strip()]
	from_conf = frappe.conf.get("ignore_r2_upload_for_doctype") or []
	return set(configured or DEFAULT_IGNORED_DOCTYPES) | set(from_conf)


def local_path(file_url):
	"""Absolute path on disk for a /files/ or /private/files/ URL."""
	if file_url.startswith("/private/files/"):
		return frappe.get_site_path("private", "files", file_url[len("/private/files/") :])
	return frappe.get_site_path("public", "files", file_url[len("/files/") :])


# ---------------------------------------------------------------------------
# doc_events
# ---------------------------------------------------------------------------


def upload_to_r2(doc, method=None):
	"""File.after_insert: move the freshly written local file to R2."""
	if doc.is_folder or doc.flags.ignore_r2 or not is_local_url(doc.file_url):
		return
	if not is_enabled():
		# configured but switched off: record it, so "why is this file not in R2?" has an answer
		if frappe.db.get_single_value("Cloudflare R2 Settings", "bucket_name"):
			log(
				"Skipped",
				"Skipped",
				doc,
				from_location=doc.file_url,
				error="Cloudflare R2 is disabled in Cloudflare R2 Settings",
			)
		return

	settings = get_settings()
	if (doc.attached_to_doctype or "File") in ignored_doctypes(settings):
		log(
			"Skipped",
			"Skipped",
			doc,
			from_location=doc.file_url,
			error=f"{doc.attached_to_doctype or 'File'} is in 'Keep Local for DocTypes'",
		)
		return

	path = local_path(doc.file_url)
	if not os.path.isfile(path):
		log("Skipped", "Skipped", doc, from_location=doc.file_url, error="File not found on server disk")
		return

	old_url = doc.file_url
	size = os.path.getsize(path)
	r2 = R2(settings)
	bucket = r2.bucket_for(doc.is_private)
	key = r2.make_key(doc.file_name, doc.is_private, doc.attached_to_doctype, doc.attached_to_name)
	try:
		r2.upload_file(path, key, doc.is_private, doc.file_name)
	except Exception as e:
		reason = f"{type(e).__name__}: {e}"
		frappe.log_error(title="Cloudflare R2: upload failed")
		# the request is about to roll back, so record the failure once it has
		frappe.db.after_rollback.add(
			lambda: log(
				"Upload Failed",
				"Failed",
				doc,
				commit=True,
				from_location=old_url,
				to_location=r2_location(bucket, key),
				bucket=bucket,
				r2_key=key,
				size=size,
				error=reason,
			)
		)
		frappe.throw(_("Could not upload the file to Cloudflare R2. Please try again."))

	new_url = r2.url_for(key, doc.is_private)

	# Point every File row sharing this blob (Frappe de-duplicates by content hash) at R2.
	frappe.db.sql(
		"update `tabFile` set file_url=%s where file_url=%s and is_folder=0",
		(new_url, old_url),
	)
	doc.file_url = new_url
	repoint_attached_field(doc, old_url, new_url)
	log(
		"Uploaded",
		"Success",
		doc,
		from_location=old_url,
		to_location=r2_location(bucket, key),
		bucket=bucket,
		r2_key=key,
		size=size,
		note="Server copy removed after commit" if settings.delete_local_after_upload else "Server copy kept",
	)

	def _rolled_back():
		# The File row (and the log above) disappear with the rollback, so remove the object too.
		_safe(r2.client.delete_object, Bucket=bucket, Key=key)
		log(
			"Rolled Back",
			"Failed",
			doc,
			commit=True,
			from_location=r2_location(bucket, key),
			bucket=bucket,
			r2_key=key,
			size=size,
			note="Upload succeeded but the request failed later; R2 object removed",
		)

	frappe.db.after_rollback.add(_rolled_back)
	if settings.delete_local_after_upload:
		frappe.db.after_commit.add(lambda: _safe(os.remove, path))


def repoint_attached_field(doc, old_url, new_url):
	"""Update the Attach / Attach Image field on the parent document, if it points at old_url."""
	if not (doc.attached_to_doctype and doc.attached_to_name):
		return
	try:
		meta = frappe.get_meta(doc.attached_to_doctype)
	except frappe.DoesNotExistError:
		return
	fields = {f for f in (doc.attached_to_field, meta.image_field) if f and meta.has_field(f)}
	if meta.issingle:
		for f in fields:
			if frappe.db.get_single_value(doc.attached_to_doctype, f) == old_url:
				frappe.db.set_single_value(doc.attached_to_doctype, f, new_url)
		return
	for f in fields:
		if frappe.db.get_value(doc.attached_to_doctype, doc.attached_to_name, f) == old_url:
			frappe.db.set_value(
				doc.attached_to_doctype, doc.attached_to_name, f, new_url, update_modified=False
			)


def delete_from_r2(doc, method=None):
	"""File.on_trash: delete (or move to trash) the object once the deletion commits."""
	if doc.is_folder or not doc.file_url:
		return
	settings = get_settings()
	key = key_from_url(doc.file_url, settings)
	if not key:
		return
	r2 = R2(settings)
	bucket = r2.bucket_for(doc.is_private)
	where = r2_location(bucket, key)
	if not settings.delete_from_r2_on_trash:
		log(
			"Kept",
			"Skipped",
			doc,
			from_location=where,
			bucket=bucket,
			r2_key=key,
			note="'Delete From R2 When File Is Deleted' is off",
		)
		return
	if frappe.db.exists("File", {"file_url": doc.file_url, "name": ("!=", doc.name)}):
		log(
			"Kept",
			"Skipped",
			doc,
			from_location=where,
			bucket=bucket,
			r2_key=key,
			note="Another File record still uses this object",
		)
		return

	is_private = doc.is_private
	days = settings.trash_retention_days or 0
	trash_key = r2.trash_key(key) if days > 0 else None
	# Logged inside the transaction: if the deletion is rolled back, so is this row.
	if trash_key:
		log(
			"Moved to Trash",
			"Success",
			doc,
			from_location=where,
			to_location=r2_location(bucket, trash_key),
			bucket=bucket,
			r2_key=key,
			note=f"Permanently deleted after {days} days",
		)
	else:
		log("Deleted", "Success", doc, from_location=where, bucket=bucket, r2_key=key)

	def _delete():
		try:
			r2.delete(key, is_private, trash_key=trash_key)
		except Exception as e:
			frappe.log_error(title=f"Cloudflare R2: could not delete {key}")
			log(
				"Delete Failed",
				"Failed",
				doc,
				commit=True,
				from_location=where,
				bucket=bucket,
				r2_key=key,
				error=f"{type(e).__name__}: {e}",
			)

	frappe.db.after_commit.add(_delete)


def _safe(fn, *args, **kwargs):
	try:
		fn(*args, **kwargs)
	except Exception:
		pass


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@frappe.whitelist(allow_guest=True, methods=["GET"])
def download_file(key=None, file_name=None, download=None):
	"""Serve an R2 object through a short-lived signed URL after checking File permissions."""
	if not key:
		raise frappe.DoesNotExistError(_("File not found"))

	rows = frappe.get_all(
		"File",
		filters={"file_url": private_url_for(key)},
		fields=["name", "is_private", "file_name"],
		ignore_permissions=True,
	)
	if not rows:
		raise frappe.DoesNotExistError(_("File not found"))

	allowed = next((r for r in rows if not r.is_private), None)
	if not allowed and frappe.session.user != "Guest":
		allowed = next((r for r in rows if frappe.has_permission("File", "read", doc=r.name)), None)
	if not allowed:
		raise frappe.PermissionError(_("You are not permitted to access this file"))

	_redirect(
		R2().presigned_url(
			key,
			file_name=file_name or allowed.file_name,
			is_private=allowed.is_private,
			inline=not frappe.utils.cint(download),
		)
	)


def _redirect(url):
	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = url


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------


@frappe.whitelist()
def test_connection():
	"""Write, read, sign and delete a probe object; check the public domain if set."""
	frappe.only_for("System Manager")
	import requests

	settings = frappe.get_doc("Cloudflare R2 Settings")
	r2 = R2(settings)
	results, ok = [], True
	token = secrets.token_hex(8)
	probe = f"frappe-cloudflare-r2 connection test {token}".encode()
	private_key = "/".join(p for p in (r2.folder, "private", "_connection_test", f"{token}.txt") if p)
	public_key = "/".join(p for p in (r2.folder, "public", "_connection_test", f"{token}.txt") if p)

	def step(label, fn):
		nonlocal ok
		try:
			fn()
			results.append(f"✅ {label}")
			return True
		except Exception as e:
			ok = False
			results.append(f"❌ {label}: {frappe.utils.cstr(e)[:300]}")
			return False

	def check_signed():
		resp = requests.get(r2.presigned_url(private_key), timeout=20)
		if resp.status_code != 200 or resp.content != probe:
			raise Exception(f"HTTP {resp.status_code}")

	if step(
		f"Upload to private bucket '{r2.bucket}'",
		lambda: r2.client.put_object(Bucket=r2.bucket, Key=private_key, Body=probe, ContentType="text/plain"),
	):
		step("Read back with the API token", lambda: r2.get_bytes(private_key, True))
		step("Download through a signed URL", check_signed)

	if r2.public_base_url:

		def check_public():
			r2.client.put_object(
				Bucket=r2.public_bucket, Key=public_key, Body=probe, ContentType="text/plain"
			)
			resp = requests.get(r2.url_for(public_key, False), timeout=20)
			if resp.status_code != 200 or resp.content != probe:
				raise Exception(
					f"HTTP {resp.status_code} from {r2.public_base_url}. Is the custom domain connected to bucket '{r2.public_bucket}'?"
				)

		step(f"Public file reachable via {r2.public_base_url}", check_public)

		if r2.public_bucket == r2.bucket:
			try:
				leaked = requests.get(r2.url_for(private_key, False), timeout=20).status_code == 200
			except Exception:
				leaked = False
			if leaked:
				results.append(
					"⚠️ Private files are also reachable through the public domain (same bucket). "
					"Use a separate Public Bucket, or add a Cloudflare WAF rule that only allows paths "
					"under /public/ on this domain. Private keys are random, but should not rely on secrecy."
				)

	for b, k in ((r2.bucket, private_key), (r2.public_bucket, public_key)):
		_safe(r2.client.delete_object, Bucket=b, Key=k)

	status = "\n".join(results)
	frappe.db.set_single_value("Cloudflare R2 Settings", "connection_status", status)
	return {"ok": ok, "status": status}


@frappe.whitelist()
def load_s3_attachment_settings(overwrite=1):
	"""Copy bucket, region and keys from the Frappe S3 Attachment app's settings."""
	frappe.only_for("System Manager")
	if not frappe.db.table_exists("S3 File Attachment"):
		frappe.throw(_("The Frappe S3 Attachment app is not installed on this site."))

	src = frappe.get_single("S3 File Attachment")
	mapping = {
		"s3_bucket_name": src.get("bucket_name"),
		"s3_region": src.get("region_name"),
		"s3_access_key_id": src.get("aws_key"),
		"s3_secret_access_key": src.get("aws_secret"),
	}
	settings = frappe.get_single("Cloudflare R2 Settings")
	for field, value in mapping.items():
		if value and (frappe.utils.cint(overwrite) or not settings.get(field)):
			settings.set(field, value)
	settings.save()
	return {k: (v if "secret" not in k else bool(v)) for k, v in mapping.items()}
