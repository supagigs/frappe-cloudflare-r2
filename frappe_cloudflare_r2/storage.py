# Copyright (c) 2026, Supagigs and contributors
# For license information, please see license.txt
"""
Thin wrapper around boto3 for Cloudflare R2 (and, for migrations, Amazon S3).

Everything that talks to object storage goes through this module so the rest of
the app never builds boto3 clients or object keys itself.
"""

import datetime
import mimetypes
import re
import secrets
from urllib.parse import parse_qs, quote, unquote, urlparse

import frappe
from frappe import _

SETTINGS_DOCTYPE = "Cloudflare R2 Settings"
DOWNLOAD_METHOD = "frappe_cloudflare_r2.controller.download_file"
DOWNLOAD_PREFIX = f"/api/method/{DOWNLOAD_METHOD}?key="
LEGACY_S3_METHOD = "frappe_s3_attachment.controller.generate_file"

MB = 1024 * 1024
DEFAULT_IGNORED_DOCTYPES = ("Data Import",)


def _boto_config(**extra):
	from botocore.client import Config

	kwargs = dict(
		signature_version="s3v4",
		retries={"max_attempts": 5, "mode": "standard"},
		connect_timeout=10,
		read_timeout=120,
	)
	kwargs.update(extra)
	try:
		# boto3 >= 1.36 adds CRC checksums to every upload by default. Only send them
		# when the API requires it, which keeps us compatible with every R2 region and
		# with older S3-compatible endpoints.
		return Config(
			request_checksum_calculation="when_required",
			response_checksum_validation="when_required",
			**kwargs,
		)
	except TypeError:
		return Config(**kwargs)


def r2_endpoint(account_id, jurisdiction=None):
	account_id = (account_id or "").strip()
	if not account_id:
		return None
	sub = {"EU": ".eu", "FedRAMP": ".fedramp"}.get(jurisdiction or "", "")
	return f"https://{account_id}{sub}.r2.cloudflarestorage.com"


def get_settings():
	return frappe.get_cached_doc(SETTINGS_DOCTYPE, SETTINGS_DOCTYPE)


def is_enabled():
	try:
		return bool(frappe.db.get_single_value(SETTINGS_DOCTYPE, "enabled"))
	except Exception:
		# table missing during install / migrate
		return False


# ---------------------------------------------------------------------------
# URL <-> key helpers (pure functions, no network)
# ---------------------------------------------------------------------------


def is_local_url(file_url):
	return bool(file_url) and file_url.startswith(("/files/", "/private/files/"))


def private_url_for(key):
	return f"{DOWNLOAD_PREFIX}{quote(key, safe='/')}"


def key_from_url(file_url, settings=None):
	"""Return the R2 object key for a URL produced by this app, else None."""
	if not file_url:
		return None
	if file_url.startswith(f"/api/method/{DOWNLOAD_METHOD}"):
		qs = parse_qs(urlparse(file_url).query)
		return unquote(qs["key"][0]) if qs.get("key") else None
	settings = settings or get_settings()
	base = (settings.public_base_url or "").rstrip("/")
	if base and file_url.startswith(base + "/"):
		return unquote(file_url[len(base) + 1 :].split("?", 1)[0])
	return None


def is_r2_url(file_url, settings=None):
	return key_from_url(file_url, settings) is not None


def slug(value, default="File"):
	value = re.sub(r"[^0-9a-zA-Z._-]+", "_", (value or "").strip()).strip("._-")
	return value[:80] or default


def guess_content_type(file_name):
	return mimetypes.guess_type(file_name or "")[0] or "application/octet-stream"


def content_disposition(file_name, inline=True):
	file_name = file_name or "file"
	ascii_name = re.sub(r'[^\x20-\x7e]|["\\]', "_", file_name)
	kind = "inline" if inline else "attachment"
	return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(file_name)}"


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class R2:
	"""Storage operations against the configured R2 bucket(s)."""

	def __init__(self, settings=None):
		self.settings = settings or get_settings()
		s = self.settings
		self.endpoint = (s.endpoint_url or "").strip() or r2_endpoint(s.account_id, s.jurisdiction)
		self.bucket = (s.bucket_name or "").strip()
		self.public_bucket = (s.public_bucket_name or "").strip() or self.bucket
		self.folder = (s.folder_name or "").strip().strip("/")
		self.public_base_url = (s.public_base_url or "").strip().rstrip("/")
		if not (self.endpoint and self.bucket and s.access_key_id):
			frappe.throw(
				_("Cloudflare R2 is not configured. Set Account ID, Bucket and API token in {0}.").format(
					SETTINGS_DOCTYPE
				)
			)
		import boto3

		self.client = boto3.client(
			"s3",
			endpoint_url=self.endpoint,
			aws_access_key_id=s.access_key_id.strip(),
			aws_secret_access_key=s.get_password("secret_access_key", raise_exception=False),
			region_name="auto",
			config=_boto_config(max_pool_connections=max(10, (s.max_concurrency or 4) * 2)),
		)

	# -- configuration -----------------------------------------------------

	def transfer_config(self):
		from boto3.s3.transfer import TransferConfig

		s = self.settings
		chunk = max(5, s.multipart_chunksize_mb or 16) * MB  # S3 API minimum part size is 5 MiB
		return TransferConfig(
			multipart_threshold=max(5, s.multipart_threshold_mb or 100) * MB,
			multipart_chunksize=chunk,
			max_concurrency=max(1, s.max_concurrency or 4),
			use_threads=True,
		)

	def bucket_for(self, is_private):
		return self.bucket if is_private else self.public_bucket

	# -- keys and URLs ------------------------------------------------------

	def make_key(self, file_name, is_private, parent_doctype=None, parent_name=None):
		hook = frappe.get_hooks("r2_key_generator")
		if hook:
			key = frappe.get_attr(hook[-1])(
				file_name=file_name,
				is_private=is_private,
				parent_doctype=parent_doctype,
				parent_name=parent_name,
			)
			if key:
				return key.strip("/")

		today = datetime.date.today()
		# private keys get 128 bits of randomness so they cannot be guessed even if the
		# bucket is ever exposed publicly; public keys only need to avoid collisions.
		token = secrets.token_hex(16 if is_private else 6)
		parts = [
			self.folder,
			"private" if is_private else "public",
			today.strftime("%Y"),
			today.strftime("%m"),
			today.strftime("%d"),
			slug(parent_doctype or "File"),
			f"{token}_{slug(file_name, 'file')}",
		]
		return "/".join(p for p in parts if p)

	def url_for(self, key, is_private):
		if not is_private and self.public_base_url:
			return f"{self.public_base_url}/{quote(key, safe='/')}"
		return private_url_for(key)

	def presigned_url(self, key, file_name=None, is_private=True, inline=True, expires=None):
		params = {"Bucket": self.bucket_for(is_private), "Key": key}
		if file_name:
			params["ResponseContentDisposition"] = content_disposition(file_name, inline)
		return self.client.generate_presigned_url(
			"get_object",
			Params=params,
			ExpiresIn=int(expires or self.settings.signed_url_expiry or 120),
		)

	# -- object operations ---------------------------------------------------

	def upload_file(self, path, key, is_private, file_name=None, content_type=None):
		self.client.upload_file(
			path,
			self.bucket_for(is_private),
			key,
			ExtraArgs=self._extra_args(file_name, content_type),
			Config=self.transfer_config(),
		)

	def upload_fileobj(self, fileobj, key, is_private, file_name=None, content_type=None):
		self.client.upload_fileobj(
			fileobj,
			self.bucket_for(is_private),
			key,
			ExtraArgs=self._extra_args(file_name, content_type),
			Config=self.transfer_config(),
		)

	def _extra_args(self, file_name, content_type):
		extra = {"ContentType": content_type or guess_content_type(file_name)}
		if file_name:
			# S3 metadata must be ASCII
			extra["Metadata"] = {"file-name": quote(file_name)}
		return extra

	def get_bytes(self, key, is_private=True):
		return self.client.get_object(Bucket=self.bucket_for(is_private), Key=key)["Body"].read()

	def download_to(self, key, path, is_private=True):
		self.client.download_file(self.bucket_for(is_private), key, path, Config=self.transfer_config())

	def exists(self, key, is_private=True):
		from botocore.exceptions import ClientError

		try:
			self.client.head_object(Bucket=self.bucket_for(is_private), Key=key)
			return True
		except ClientError as e:
			if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
				return False
			raise

	def delete(self, key, is_private=True):
		"""Delete an object, honouring the retention policy (soft delete into trash)."""
		bucket = self.bucket_for(is_private)
		days = self.settings.trash_retention_days or 0
		if days > 0:
			self.client.copy(
				{"Bucket": bucket, "Key": key},
				bucket,
				self.trash_key(key),
				Config=self.transfer_config(),
			)
		self.client.delete_object(Bucket=bucket, Key=key)

	def trash_prefix(self):
		prefix = (self.settings.trash_prefix or "_trash").strip("/")
		return f"{self.folder}/{prefix}/" if self.folder else f"{prefix}/"

	def trash_key(self, key):
		stamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%S")
		return f"{self.trash_prefix()}{stamp}/{key}"

	def purge_trash(self, older_than_days):
		"""Permanently delete trashed objects older than N days. Returns count deleted."""
		cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=older_than_days)
		deleted = 0
		for bucket in {self.bucket, self.public_bucket}:
			batch = []
			for obj in self._iter_objects(bucket, self.trash_prefix()):
				if obj["LastModified"] < cutoff:
					batch.append({"Key": obj["Key"]})
				if len(batch) == 1000:
					deleted += self._delete_many(bucket, batch)
					batch = []
			if batch:
				deleted += self._delete_many(bucket, batch)
		return deleted

	def _delete_many(self, bucket, objects):
		self.client.delete_objects(Bucket=bucket, Delete={"Objects": objects, "Quiet": True})
		return len(objects)

	def _iter_objects(self, bucket, prefix):
		paginator = self.client.get_paginator("list_objects_v2")
		for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
			yield from page.get("Contents", [])


# ---------------------------------------------------------------------------
# Amazon S3 (migration source)
# ---------------------------------------------------------------------------


class S3Source:
	"""Read/delete access to the bucket used by the Frappe S3 Attachment app."""

	def __init__(self, settings=None):
		s = self.settings = settings or get_settings()
		self.bucket = (s.s3_bucket_name or "").strip()
		if not self.bucket:
			frappe.throw(_("Set the Amazon S3 bucket name in the 'Migrate from Amazon S3' section."))
		import boto3

		kwargs = dict(region_name=(s.s3_region or None), config=_boto_config())
		if (s.s3_endpoint_url or "").strip():
			kwargs["endpoint_url"] = s.s3_endpoint_url.strip()
		if s.s3_access_key_id:
			secret = s.get_password("s3_secret_access_key", raise_exception=False)
			if not secret and frappe.db.table_exists("S3 File Attachment"):
				# fall back to the S3 Attachment app's own secret when the user has not set one
				secret = frappe.db.get_single_value("S3 File Attachment", "aws_secret")
			kwargs["aws_access_key_id"] = s.s3_access_key_id.strip()
			kwargs["aws_secret_access_key"] = secret
		self.client = boto3.client("s3", **kwargs)

	def head(self, key):
		return self.client.head_object(Bucket=self.bucket, Key=key)

	def open(self, key):
		return self.client.get_object(Bucket=self.bucket, Key=key)

	def delete(self, key):
		self.client.delete_object(Bucket=self.bucket, Key=key)

	def presigned_url(self, key, file_name=None, expires=120):
		params = {"Bucket": self.bucket, "Key": key}
		if file_name:
			params["ResponseContentDisposition"] = content_disposition(file_name)
		return self.client.generate_presigned_url("get_object", Params=params, ExpiresIn=expires)


def legacy_s3_key(file_url, settings=None):
	"""
	Extract the object key from a URL written by the Frappe S3 Attachment app.

	Private: /api/method/frappe_s3_attachment.controller.generate_file?key=K[&file_name=..]
	Public:  https://<endpoint>/<bucket>/K  or  https://<bucket>.s3.<region>.amazonaws.com/K
	"""
	if not file_url:
		return None
	if file_url.startswith(f"/api/method/{LEGACY_S3_METHOD}"):
		qs = parse_qs(urlparse(file_url).query)
		return unquote(qs["key"][0]) if qs.get("key") else None

	if not file_url.startswith(("http://", "https://")):
		return None
	settings = settings or get_settings()
	bucket = (settings.s3_bucket_name or "").strip()
	if not bucket:
		return None
	parsed = urlparse(file_url)
	host, path = parsed.netloc.lower(), unquote(parsed.path.lstrip("/"))
	custom = (settings.s3_endpoint_url or "").strip().rstrip("/").lower()
	is_s3_host = host.endswith("amazonaws.com") or (custom and file_url.lower().startswith(custom))
	if not is_s3_host:
		return None
	if host.startswith(bucket.lower() + "."):
		return path or None  # virtual-hosted style
	if path.startswith(bucket + "/"):
		return path[len(bucket) + 1 :] or None  # path style (what the S3 app writes)
	return None
