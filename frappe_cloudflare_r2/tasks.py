# Copyright (c) 2026, Supagigs and contributors
# For license information, please see license.txt

from frappe_cloudflare_r2.migration import purge_r2_trash, purge_s3_sources


def daily():
	purge_r2_trash()
	purge_s3_sources()
