// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

function payrollChecklistModeActive() {
	if (frappe.boot && Object.prototype.hasOwnProperty.call(frappe.boot, "adhd_mode")) {
		return Boolean(frappe.boot.adhd_mode);
	}
	return typeof erpnext !== "undefined" && Boolean(erpnext?.adhd?.isActive?.());
}

// Frappe calls on_page_load once, when the page is first created, and on_page_show right after it (and on
// every later visit). The mode is read fresh every time, never cached from load, so a toggle before the
// visit is always honoured; on_state_change below also reacts to a toggle during the visit itself.
frappe.pages["focus-payroll-checklist"].on_page_load = function (wrapper) {
	frappe.payroll_checklist_page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Payroll Cycle Checklist"),
		single_column: true,
	});

	if (typeof erpnext !== "undefined" && erpnext?.adhd?.onStateChange) {
		erpnext.adhd.onStateChange((active) => {
			if (!frappe.payroll_checklist_page.__adhd_payroll_rendered) return;
			if (!active) frappe.set_route("Workspaces");
		});
	}
};

frappe.pages["focus-payroll-checklist"].on_page_show = function () {
	if (!payrollChecklistModeActive()) {
		frappe.set_route("Workspaces");
		return;
	}
	if (!(typeof hrms !== "undefined" && hrms.adhd && hrms.adhd.initPayrollChecklist)) return;

	const page = frappe.payroll_checklist_page;
	if (!page) return;

	page.__adhd_payroll_rendered = true;
	hrms.adhd.initPayrollChecklist(page.body);
};
