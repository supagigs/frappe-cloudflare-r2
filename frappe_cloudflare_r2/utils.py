import frappe


def has_app_permission():
	return "System Manager" in frappe.get_roles()
