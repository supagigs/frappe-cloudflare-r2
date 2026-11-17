# Copyright (c) 2026, Supagigs and contributors
# For license information, please see license.txt
"""
R2 files through frappe.utils.file_manager.get_file_path() (and so get_file()).

get_file_path() never calls File.get_full_path(): it looks the File up and builds a path under
sites/<site>/(private/)files from its URL, which fails for an R2 URL. This wraps it once, when the
app package is imported (frappe_cloudflare_r2/__init__.py), so it is in place in every process
that loads the app (web, worker, scheduler, bench console/execute/run-tests). For an R2 file the
wrapper returns the private cached copy from cache.py, the path File.get_full_path() returns; for
anything else it calls the original unchanged. The original stays reachable as
`_orig_get_file_path`.

Covered through it:
* get_file() calls get_file_path() through the module global. It returns
  [last part of the path, content], so for an R2 file the first item is the cached copy's name
  (a hash plus the extension), not the File's file_name.
* frappe.utils.data.pdf_to_base64 imports get_file_path inside the function.
* erpnext bank_statement_import.py binds get_file by name at module level; get_file still looks
  get_file_path up in file_manager, so it is covered too.

Guard (fails open): wrapped only on Frappe major version 16 and only if the sha256 of the
original's source is SOURCE_SHA256. Otherwise nothing is wrapped and a warning is logged.
test_compat fails when the source changes, so a Frappe upgrade shows up in CI: check the new
source, then update SOURCE_SHA256.

Not covered: code that builds the path itself with frappe.utils.get_files_path(file_name, ...):
package_import.py:56 and erpnext/regional/italy/utils.py:134. get_files_path is deliberately not
wrapped:
* it takes path parts, not a File, and a file name is not unique (an R2 and a local File can
  share one), so it can't be mapped safely to an R2 object;
* the same call writes new uploads (File.save_file_on_filesystem) and checks that a name is free
  (core/doctype/file/utils.py); sending those to a read-only cached copy would break uploads;
* core imports it by name (file.py, file/utils.py, file_manager.py, package_import.py), so a
  wrapper on frappe.utils would reach the Italy zip and almost nothing else.
Those callers need File.get_full_path() (or local_copy()) upstream.
"""

import functools
import hashlib
import inspect
import logging

import frappe

FRAPPE_MAJOR = 16
# sha256 of inspect.getsource(frappe.utils.file_manager.get_file_path), Frappe 16.53.0
SOURCE_SHA256 = "22ead451900c947930af9030ef3fc1e768dfa643bb2445ba934605b6d6869e99"
WRAPPED = "_frappe_cloudflare_r2_compat"

_orig_get_file_path = None


def apply(module=None, frappe_version=None):
	"""Wrap get_file_path() in `module` (default frappe.utils.file_manager), once.

	Returns True if it is wrapped. Never raises: a failure here must not stop the app loading.
	"""
	try:
		return _apply(module, frappe_version or frappe.__version__)
	except Exception:
		logging.getLogger(__name__).exception("frappe_cloudflare_r2: could not wrap get_file_path")
		return False


def _apply(module, frappe_version):
	global _orig_get_file_path
	real = module is None
	if real:
		from frappe.utils import file_manager as module

	original = module.get_file_path
	if getattr(original, WRAPPED, False):
		return True
	if not guard_passes(original, frappe_version):
		_logger().warning(
			f"frappe_cloudflare_r2: {module.__name__}.get_file_path is not the expected Frappe "
			f"{FRAPPE_MAJOR} version (Frappe {frappe_version}); not wrapped, so get_file() and "
			"get_file_path() can't read R2 files"
		)
		return False
	if real:
		_orig_get_file_path = original
	module.get_file_path = _make_get_file_path(original)
	return True


def guard_passes(func, frappe_version):
	try:
		major = int(str(frappe_version).split(".")[0])
	except ValueError:
		return False
	return major == FRAPPE_MAJOR and source_sha256(func) == SOURCE_SHA256


def source_sha256(func):
	try:
		return hashlib.sha256(inspect.getsource(func).encode()).hexdigest()
	except (OSError, TypeError):
		return None


def _make_get_file_path(original):
	@functools.wraps(original)
	def get_file_path(file_name):
		r2 = r2_file(file_name)
		if not r2:
			return original(file_name)
		from frappe_cloudflare_r2.cache import cached_copy

		return cached_copy(*r2)

	setattr(get_file_path, WRAPPED, True)
	return get_file_path


def r2_file(file_name):
	"""(key, is_private, file_name) if `file_name` resolves to a File stored in R2, else None.

	One query, matching the File as core get_file_path() does (name or file_name, first row).
	"""
	if not isinstance(file_name, str) or not file_name or "../" in file_name:
		return None

	from frappe_cloudflare_r2.storage import is_local_url, key_from_url

	File = frappe.qb.DocType("File")
	rows = (
		frappe.qb.from_(File)
		.where((File.name == file_name) | (File.file_name == file_name))
		.select(File.file_url, File.is_private, File.file_name)
		.run()
	)
	if not rows:
		return None
	file_url, is_private, name = rows[0]
	if not file_url or is_local_url(file_url):
		return None
	# the wrap is process-wide and a bench can serve sites without this app
	if "frappe_cloudflare_r2" not in frappe.get_installed_apps():
		return None
	key = key_from_url(file_url)
	return (key, is_private, name) if key else None


def _logger():
	return frappe.logger("frappe_cloudflare_r2")


apply()
