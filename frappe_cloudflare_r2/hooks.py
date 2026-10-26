app_name = "frappe_cloudflare_r2"
app_title = "Cloudflare R2"
app_publisher = "Supagigs"
app_description = "Store Frappe / ERPNext attachments in Cloudflare R2, with one-click migration from local disk and Amazon S3."
app_email = "support@supagigs.com"
app_license = "mit"
app_logo_url = "/assets/frappe_cloudflare_r2/images/logo.png"

add_to_apps_screen = [
	{
		"name": app_name,
		"logo": app_logo_url,
		"title": app_title,
		"route": "/app/cloudflare-r2-settings",
		"has_permission": "frappe_cloudflare_r2.utils.has_app_permission",
	}
]

doc_events = {
	"File": {
		"after_insert": "frappe_cloudflare_r2.controller.upload_to_r2",
		"on_trash": "frappe_cloudflare_r2.controller.delete_from_r2",
	}
}

# Read file bytes from R2 (email attachments, Data Import, ...).
# v16+: extend the core File class. v15 has no extension hook, so it needs a class override.
extend_doctype_class = {
	"File": ["frappe_cloudflare_r2.overrides.file.R2FileMixin"],
}

try:
	from frappe import __version__ as _frappe_version

	if int(_frappe_version.split(".")[0]) < 16:
		override_doctype_class = {"File": "frappe_cloudflare_r2.overrides.file.R2File"}
except (ImportError, ValueError):
	pass

# Old links written by the Frappe S3 Attachment app keep working after migration.
override_whitelisted_methods = {
	"frappe_s3_attachment.controller.generate_file": "frappe_cloudflare_r2.controller.legacy_s3_file",
}

scheduler_events = {
	"daily": ["frappe_cloudflare_r2.tasks.daily"],
}

# Other apps can customise object keys by declaring:
# r2_key_generator = "my_app.storage.make_key"
# def make_key(file_name, is_private, parent_doctype, parent_name) -> str | None
