// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// Hubble's forms and lists, plugged into RTB's Focus aids through the registration functions on `erpnext.adhd`
// (adhd_deadline_banner.js, form_focus.js, adhd_list_view.js, adhd_smart_inbox.js, adhd_draft_recovery.js and
// adhd_settings.js in the RTB app). Every field named here was checked against the doctype JSON, and the Node
// test reads those files again so a renamed field fails a test. Everything registered is display only and acts
// only while Focus Mode is on; an older RTB without one of the functions is simply left alone.

frappe.provide("erpnext.adhd");
frappe.provide("hrms.focus");

(() => {
	const plural = (count) => (Math.abs(count) === 1 ? "" : "s");
	const isDraft = (doc) => cint(doc.docstatus) === 0;

	// The banner at the top of a form: what its date means today, in plain words. `days` is the number of
	// days from today to the date (negative when it has passed).
	const DEADLINES = {
		"Leave Application": {
			dateField: "from_date",
			message: (days) =>
				days < 0
					? `⚠️ ${__("Leave started {0} day{1} ago and is still waiting for approval", [
							Math.abs(days),
							plural(days),
					  ])}`
					: days === 0
					  ? __("⚠️ Leave starts today and is still waiting for approval")
					  : `🗓️ ${__("Leave starts in {0} day{1}", [days, plural(days)])}`,
			showWhen: (doc) => isDraft(doc) && doc.status === "Open",
		},
		Interview: {
			dateField: "scheduled_on",
			message: (days) =>
				days < 0
					? `⚠️ ${__("Interview was {0} day{1} ago and has no result yet", [
							Math.abs(days),
							plural(days),
					  ])}`
					: days === 0
					  ? __("🎤 Interview today")
					  : `🎤 ${__("Interview in {0} day{1}", [days, plural(days)])}`,
			showWhen: (doc) => doc.status === "Pending",
		},
		Appraisal: {
			dateField: "end_date",
			message: (days) =>
				days < 0
					? `⚠️ ${__("Appraisal period ended {0} day{1} ago and this is not submitted", [
							Math.abs(days),
							plural(days),
					  ])}`
					: days === 0
					  ? __("⚠️ Appraisal period ends today")
					  : `📝 ${__("Appraisal period ends in {0} day{1}", [days, plural(days)])}`,
			showWhen: isDraft,
		},
		"Payroll Entry": {
			dateField: "end_date",
			message: (days) =>
				days < 0
					? `⚠️ ${__("Payroll period ended {0} day{1} ago and this is not submitted", [
							Math.abs(days),
							plural(days),
					  ])}`
					: days === 0
					  ? __("⚠️ Payroll period ends today")
					  : `💸 ${__("Payroll period ends in {0} day{1}", [days, plural(days)])}`,
			showWhen: isDraft,
		},
		"Employee Onboarding": {
			dateField: "date_of_joining",
			message: (days) =>
				days < 0
					? `⚠️ ${__("Joined {0} day{1} ago and onboarding is not complete", [
							Math.abs(days),
							plural(days),
					  ])}`
					: days === 0
					  ? __("👋 Joins today")
					  : `👋 ${__("Joins in {0} day{1}", [days, plural(days)])}`,
			showWhen: (doc) => doc.boarding_status !== "Completed" && cint(doc.docstatus) !== 2,
		},
		"Training Event": {
			dateField: "start_time",
			message: (days) =>
				days < 0
					? `⚠️ ${__("Training was {0} day{1} ago and is not marked completed", [
							Math.abs(days),
							plural(days),
					  ])}`
					: days === 0
					  ? __("🎓 Training today")
					  : `🎓 ${__("Training in {0} day{1}", [days, plural(days)])}`,
			showWhen: (doc) => doc.event_status === "Scheduled",
		},
	};

	// Forms that are easy to lose an hour in: the time-box banner offers a timer.
	const TIMEBOXED = [
		"Salary Structure",
		"Payroll Entry",
		"Shift Type",
		"Appraisal Cycle",
		"Leave Policy",
	];

	// Lists: the date that makes a row urgent, and the statuses that mean the date no longer matters.
	const LIST_DATES = {
		"Leave Application": ["to_date", ["Approved", "Rejected"]],
		Interview: ["scheduled_on", ["Cleared", "Rejected"]],
		"Job Opening": ["closes_on", ["Closed"]],
		"Shift Request": ["from_date", ["Approved", "Rejected"]],
		"Payroll Entry": ["end_date", ["Submitted"]],
	};

	// The Smart Inbox ranks the workspaces a person visits most; these are Hubble's (their names, as in
	// hrms/*/workspace/*/*.json).
	const MODULES = [
		"Leaves",
		"Expenses",
		"Payroll",
		"Recruitment",
		"Performance",
		"Tenure",
		"Shift & Attendance",
	];

	// Unsaved drafts worth keeping in this browser. The employee is filled in on its own when the person opens
	// the form, so it does not show the form is being worked on: the expense rows do, or the chosen leave type.
	const DRAFTS = {
		"Expense Claim": {
			party: [],
			scalars: [
				"company",
				"employee",
				"posting_date",
				"expense_approver",
				"cost_center",
				"project",
				"task",
				"remark",
			],
			tables: {
				expenses: {
					doctype: "Expense Claim Detail",
					identity: ["expense_type"],
					fields: [
						"expense_type",
						"expense_date",
						"description",
						"amount",
						"cost_center",
						"project",
					],
				},
			},
			contentTables: ["expenses"],
		},
		"Leave Application": {
			party: ["leave_type"],
			scalars: [
				"company",
				"employee",
				"leave_type",
				"from_date",
				"to_date",
				"half_day",
				"half_day_date",
				"description",
				"leave_approver",
			],
			tables: {},
			contentTables: [],
		},
	};

	// The switch in Focus Settings that the leave balance card and the receipt check read.
	const FEATURE = {
		key: "hr_form_aids",
		label: __("Leave Balance and Receipt Checks"),
		defaultOn: true,
	};

	function register() {
		const adhd = erpnext.adhd;
		const can = (name) => typeof adhd[name] === "function";
		if (can("registerDeadline")) {
			Object.entries(DEADLINES).forEach(([doctype, config]) =>
				adhd.registerDeadline(doctype, config),
			);
		}
		if (can("registerTimebox")) TIMEBOXED.forEach((doctype) => adhd.registerTimebox(doctype));
		if (can("registerListDate")) {
			Object.entries(LIST_DATES).forEach(([doctype, [dateField, closed]]) =>
				adhd.registerListDate(doctype, dateField, closed),
			);
		}
		if (can("registerInboxModule")) MODULES.forEach((name) => adhd.registerInboxModule(name));
		if (can("registerDraftRecovery")) {
			Object.entries(DRAFTS).forEach(([doctype, config]) =>
				adhd.registerDraftRecovery(doctype, config),
			);
		}
		if (can("registerFeature")) adhd.registerFeature(FEATURE);
	}

	register();

	hrms.focus.registrations = {
		DEADLINES,
		TIMEBOXED,
		LIST_DATES,
		MODULES,
		DRAFTS,
		FEATURE,
		register,
	};
})();
