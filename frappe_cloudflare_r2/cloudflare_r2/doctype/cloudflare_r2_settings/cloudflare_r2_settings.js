// Copyright (c) 2026, Supagigs and contributors
// For license information, please see license.txt

frappe.ui.form.on("Cloudflare R2 Settings", {
	setup(frm) {
		frappe.realtime.on("r2_migration_progress", (data) => {
			frm.set_value("migration_status", data.text);
			frm.doc.__unsaved = 0;
			frm.refresh_header();
			if (data.total) {
				frappe.show_progress(
					__("Migrating files"),
					data.done,
					data.total,
					`${data.label} · ${data.done} / ${data.total}`,
					true
				);
			}
			if (["Completed"].includes(data.label) || data.label.startsWith("Stopped")) {
				frappe.hide_progress();
				frappe.show_alert({
					message: data.text.split("\n")[0],
					indicator: data.label === "Completed" ? "green" : "red",
				});
				frm.reload_doc();
			}
		});
	},

	refresh(frm) {
		frm.add_custom_button(__("Migration Log"), () => frappe.set_route("List", "R2 Migration Log"));
		if (frm.doc.__onload && frm.doc.__onload.s3_app_installed) {
			frm.dashboard.set_headline_alert(
				__("Frappe S3 Attachment is installed. Migrate its files to R2, then uninstall it."),
				"orange"
			);
		}
	},

	test_connection(frm) {
		save_then(frm, () =>
			frappe.call({
				method: "frappe_cloudflare_r2.controller.test_connection",
				freeze: true,
				freeze_message: __("Talking to Cloudflare R2…"),
				callback(r) {
					frm.reload_doc();
					frappe.msgprint({
						title: r.message.ok ? __("Connection OK") : __("Connection problem"),
						indicator: r.message.ok ? "green" : "red",
						message: frappe.utils.escape_html(r.message.status).replace(/\n/g, "<br>"),
					});
				},
			})
		);
	},

	migrate_local_files(frm) {
		start(frm, "local_to_r2", __("Upload all files stored on the server to R2 and delete the server copies?"));
	},

	migrate_from_s3(frm) {
		start(
			frm,
			"s3_to_r2",
			__("Copy all files uploaded by Frappe S3 Attachment to R2? S3 originals are kept for {0} days.", [
				frm.doc.s3_source_retention_days || __("unlimited"),
			])
		);
	},

	migrate_r2_to_local(frm) {
		start(frm, "r2_to_local", __("Download every R2 file back to the server disk?"));
	},

	load_from_s3_app(frm) {
		frappe.call({
			method: "frappe_cloudflare_r2.controller.load_s3_attachment_settings",
			args: { overwrite: 1 },
			callback() {
				frm.reload_doc();
				frappe.show_alert({ message: __("Loaded S3 settings"), indicator: "green" });
			},
		});
	},
});

function save_then(frm, fn) {
	if (frm.is_dirty()) {
		frm.save().then(fn);
	} else {
		fn();
	}
}

function start(frm, direction, question) {
	save_then(frm, () =>
		frappe.confirm(question, () =>
			frappe.call({
				method: "frappe_cloudflare_r2.migration.start_migration",
				args: { direction },
				callback(r) {
					frappe.show_alert({
						message: __("Migration queued: {0} files", [r.message.total]),
						indicator: "blue",
					});
					frm.reload_doc();
				},
			})
		)
	);
}
