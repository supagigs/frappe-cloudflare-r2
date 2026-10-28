frappe.listview_settings["R2 File Log"] = {
	get_indicator(doc) {
		const color = { Success: "green", Skipped: "gray", Failed: "red" }[doc.status] || "blue";
		return [__(doc.event), color, `event,=,${doc.event}`];
	},
};
