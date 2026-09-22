# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Move a site from the "Frappe HR" name to "Hubble".

The app icon is named by its label, so the sync creates the renamed one and the old one is removed here, after
the module icons that hang under it have been moved to the new one. It is deleted directly, not with
`frappe.delete_doc`: deleting a standard record in developer mode also looks for folders on disk. A navbar logo
that still points at the old file is pointed at the new one.
"""

import frappe

OLD, NEW = "Frappe HR", "Hubble"
OLD_LOGO = "/assets/hrms/images/frappe-hr-logo.svg"
NEW_LOGO = "/assets/hrms/images/hubble-logo.svg"


def execute():
	if frappe.db.exists("Desktop Icon", NEW):
		frappe.db.set_value("Desktop Icon", {"parent_icon": OLD}, "parent_icon", NEW, update_modified=False)
	if frappe.db.exists("Desktop Icon", OLD):
		frappe.db.delete("Desktop Icon", {"name": OLD})

	if frappe.db.get_single_value("Navbar Settings", "app_logo") == OLD_LOGO:
		frappe.db.set_single_value("Navbar Settings", "app_logo", NEW_LOGO)
