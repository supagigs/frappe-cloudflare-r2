# Copyright (c) 2026, Supagigs and contributors
# For license information, please see license.txt
"""
Private on-disk copies of R2 objects, so File.get_full_path() returns a real path for an R2 file,
just as it does for a file on the server disk.

* Location: sites/<site>/private/r2_cache/<sha256 of bucket side + key><ext>. Never under public/.
* Files are read-only for the owner (0400): private, and a write into the path fails loudly
  instead of changing a copy that is never uploaded. The folder is 0700.
* Written atomically: downloaded to a temp file in the same folder, then renamed into place.
* Reused while present. An R2 key never gets new bytes (every upload gets a fresh random key,
  see R2.make_key), so a cached copy can't go stale.
* Removed when the last File using the object is deleted (both the private and public copy). A daily
  job removes copies not used for N days and keeps the folder under a size limit (Cloudflare R2
  Settings). `clear()` empties it:
      bench --site <site> execute frappe_cloudflare_r2.cache.clear
* Grace rule: a copy used in the last hour is never evicted for size, because a caller may still be
  reading the path it was handed. The size limit is soft: when only recent copies are left over it,
  the folder stays over the limit and a warning is logged.
"""

import contextlib
import hashlib
import os
import re
import tempfile
import time

import frappe
from frappe.utils import cint

FOLDER = "r2_cache"
TEMP_PREFIX = ".tmp-"
MB = 1024 * 1024
DAY = 24 * 60 * 60
GRACE_SECONDS = 60 * 60  # copies used this recently are never evicted for size
SIDES = ("private", "public")


def logger():
	return frappe.logger("frappe_cloudflare_r2")


def _folder_path():
	return os.path.abspath(frappe.get_site_path("private", FOLDER))


def folder():
	path = _folder_path()
	os.makedirs(path, mode=0o700, exist_ok=True)
	return path


def path_for(key, is_private, file_name=None):
	"""Where the cached copy of an object lives (whether or not it exists yet)."""
	return os.path.join(folder(), _name_for(key, "private" if cint(is_private) else "public", file_name))


def _name_for(key, side, file_name=None):
	digest = hashlib.sha256(f"{side}:{key}".encode()).hexdigest()
	# Keys end with the file name, so every File sharing an object gets the same path. Some
	# libraries (openpyxl) need the extension. It comes from user input: keep it only if plain.
	ext = ""
	for name in (key, file_name):
		candidate = os.path.splitext(name or "")[1]
		if re.fullmatch(r"\.[A-Za-z0-9]{1,16}", candidate):
			ext = candidate
			break
	return digest + ext


def cached_copy(key, is_private, file_name=None):
	"""Path of a local copy of the object, downloading it on first use."""
	path = path_for(key, is_private, file_name)
	if os.path.isfile(path):
		with contextlib.suppress(OSError):
			os.utime(path)  # last use, for the daily sweep
		return path

	from frappe_cloudflare_r2.storage import R2

	fd, temp = tempfile.mkstemp(prefix=TEMP_PREFIX, dir=os.path.dirname(path))  # mode 0600
	try:
		with os.fdopen(fd, "wb") as fh:
			R2().download_fileobj(key, fh, cint(is_private))
		os.chmod(temp, 0o400)
		os.replace(temp, path)  # atomic: readers never see a half-written copy
	except BaseException:
		with contextlib.suppress(FileNotFoundError):
			os.remove(temp)
		raise
	_check_size()
	return path


def remove(key, is_private=None, file_name=None):
	"""Remove the cached copies of an object: both the private and the public one.

	is_private is ignored. A File whose is_private changed may have left a copy under either side.
	"""
	path = _folder_path()
	for side in SIDES:
		with contextlib.suppress(OSError):
			os.remove(os.path.join(path, _name_for(key, side, file_name)))


def sweep(retention_days=None, max_size_mb=None):
	"""Daily: remove copies unused for N days, then the least recently used until under the size limit.

	0 turns either limit off. The size limit never removes a copy used in the last hour (soft limit).
	Returns the number of files removed.
	"""
	if retention_days is None or max_size_mb is None:
		from frappe_cloudflare_r2.storage import get_settings

		settings = get_settings()
		retention_days = cint(settings.cache_retention_days) if retention_days is None else retention_days
		max_size_mb = cint(settings.cache_max_size_mb) if max_size_mb is None else max_size_mb

	now = time.time()
	removed = 0
	kept = []
	for entry in _entries():
		stale_temp = entry.name.startswith(TEMP_PREFIX) and now - entry.mtime > DAY
		unused = retention_days > 0 and now - entry.mtime > retention_days * DAY
		if stale_temp or unused:
			removed += _remove(entry.path)
		elif not entry.name.startswith(TEMP_PREFIX):
			kept.append(entry)

	removed += _evict_over_limit(kept, max_size_mb, now)

	if removed:
		logger().info(f"Removed {removed} cached R2 copies")
	return removed


def _check_size():
	"""After a copy is added: one pass over the folder, and eviction only if it is over the limit."""
	try:
		from frappe_cloudflare_r2.storage import get_settings

		max_size_mb = cint(get_settings().cache_max_size_mb)
		if max_size_mb <= 0:
			return
		entries = [e for e in _entries() if not e.name.startswith(TEMP_PREFIX)]
		if sum(e.size for e in entries) <= max_size_mb * MB:
			return  # the usual case: under the limit, nothing sorted or removed
		removed = _evict_over_limit(entries, max_size_mb, time.time())
		if removed:
			logger().info(f"Removed {removed} cached R2 copies over the size limit")
	except Exception:
		# the copy is already in place; a failed size check must not fail the caller's read
		logger().exception("Cloudflare R2 cache size check failed")


def _evict_over_limit(entries, max_size_mb, now):
	"""Remove the least recently used copies until under the limit, sparing copies used in the last hour."""
	if max_size_mb <= 0:
		return 0
	limit = max_size_mb * MB
	total = sum(e.size for e in entries)
	if total <= limit:
		return 0
	removed = 0
	older = [e for e in entries if now - e.mtime >= GRACE_SECONDS]
	for entry in sorted(older, key=lambda e: e.mtime):  # least recently used first
		if total <= limit:
			break
		removed += _remove(entry.path)
		total -= entry.size
	if total > limit:
		logger().warning(
			f"Cloudflare R2 cache is over its size limit ({total // MB} MB of {max_size_mb} MB): "
			"the remaining copies were used in the last hour and are kept"
		)
	return removed


def clear():
	"""Remove every cached copy. Safe at any time: copies are downloaded again when needed."""
	removed = sum(_remove(e.path) for e in _entries())
	print(f"Removed {removed} cached R2 copies")
	return removed


class _Entry:
	__slots__ = ("mtime", "name", "path", "size")

	def __init__(self, dir_entry):
		stat = dir_entry.stat()
		self.name = dir_entry.name
		self.path = dir_entry.path
		self.size = stat.st_size
		self.mtime = stat.st_mtime


def _entries():
	path = _folder_path()
	if not os.path.isdir(path):
		return []
	with os.scandir(path) as it:
		return [_Entry(e) for e in it if e.is_file(follow_symlinks=False)]


def _remove(path):
	try:
		os.remove(path)
		return 1
	except OSError:
		return 0
