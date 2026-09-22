// Payroll Cycle Checklist (ADHD-057): a stateful, auto-verifying guide through one payroll period.
// Pattern followed from erpnext/public/js/adhd/adhd_month_end.js (ADHD-012): server auto-detection where
// it is reliable, a manual checkbox persisted per user/period everywhere it is not, a progress bar, and a
// mode gate read at render time (never cached at load).

frappe.provide("hrms.adhd");

// The six real steps of a payroll cycle (see hrms/payroll/services/adhd_payroll_cycle.py for what "done"
// means for each). `link` is a plain desk URL: this checklist never pre-fills a filter that could go stale.
hrms.adhd.PAYROLL_STEPS = [
	{
		id: "attendance",
		label: __("Mark Attendance"),
		description: __(
			"Every employee due pay has the period's working days accounted for; nothing is left unmarked or in draft.",
		),
		link: "/app/attendance",
	},
	{
		id: "leave",
		label: __("Process Leave Applications"),
		description: __("Approve or reject every leave request that overlaps the period."),
		link: "/app/leave-application?status=Open",
	},
	{
		id: "additions",
		label: __("Settle Additional Pay & Expense Claims"),
		description: __(
			"Enter and approve Additional Salary, Employee Incentive, Arrear and Expense Claim rows for the period. Focus mode cannot tell whether everything was entered, only whether anything is still waiting — check this one off yourself.",
		),
		link: "/app/additional-salary?docstatus=0",
	},
	{
		id: "payroll_entry",
		label: __("Create Payroll Entry"),
		description: __(
			"Run a Payroll Entry for the period and submit it; submitting creates the salary slips.",
		),
		link: "/app/payroll-entry/new",
	},
	{
		id: "salary_slips",
		label: __("Generate & Submit Salary Slips"),
		description: __(
			"Review the slips the Payroll Entry created, then submit every one of them.",
		),
		link: "/app/salary-slip",
	},
	{
		id: "bank_entry",
		label: __("Record Bank Payment"),
		description: __(
			"Open the submitted Payroll Entry and use Make Bank Entry to book the payment against the bank account.",
		),
		link: "/app/payroll-entry?docstatus=1",
	},
];

const MONTH_NAMES = [
	__("January"),
	__("February"),
	__("March"),
	__("April"),
	__("May"),
	__("June"),
	__("July"),
	__("August"),
	__("September"),
	__("October"),
	__("November"),
	__("December"),
];

// Only these two states leave room for a person's own judgement; "done" and "pending" are backed by a
// real query and are never second-guessed by a stale manual tick.
const MANUAL_STATES = ["unknown", "no_access"];

function isFocusModeActive() {
	if (frappe.boot && Object.prototype.hasOwnProperty.call(frappe.boot, "adhd_mode")) {
		return Boolean(frappe.boot.adhd_mode);
	}
	// erpnext.adhd owns the mode toggle; HRMS only reads it, and only once the erpnext app has defined it
	// (required_apps guarantees erpnext loads before hrms, but a test sandbox may not provide it at all).
	return typeof erpnext !== "undefined" && Boolean(erpnext?.adhd?.isActive?.());
}

function storageKey(company, year, month) {
	return `adhd_payroll_${frappe.session.user}_${company}_${year}_${String(month).padStart(
		2,
		"0",
	)}`;
}

function loadManualState(key) {
	try {
		return JSON.parse(localStorage.getItem(key)) || {};
	} catch {
		return {};
	}
}

// Companies the caller may read, fetched once per page load and cached on the namespace (never on window,
// so a second page instance in the same session shares nothing stale by accident).
async function fetchCompanies() {
	if (hrms.adhd._payrollCompanies) return hrms.adhd._payrollCompanies;
	const response = await frappe.call({
		method: "hrms.payroll.services.adhd_payroll_cycle.get_payroll_checklist_companies",
	});
	hrms.adhd._payrollCompanies = response.message || { companies: [], default: null };
	return hrms.adhd._payrollCompanies;
}

function selectorsHtml(companies, selected) {
	const companyOptions = companies.companies
		.map(
			(name) =>
				`<option value="${frappe.utils.escape_html(name)}" ${
					name === selected.company ? "selected" : ""
				}>${frappe.utils.escape_html(name)}</option>`,
		)
		.join("");
	const monthOptions = MONTH_NAMES.map(
		(name, index) =>
			`<option value="${index + 1}" ${
				index + 1 === selected.month ? "selected" : ""
			}>${name}</option>`,
	).join("");
	const currentYear = new Date().getFullYear();
	const years = [];
	for (let year = currentYear - 4; year <= currentYear + 1; year++) years.push(year);
	const yearOptions = years
		.map(
			(year) =>
				`<option value="${year}" ${
					year === selected.year ? "selected" : ""
				}>${year}</option>`,
		)
		.join("");

	return `
		<div class="adhd-payroll-selectors">
			<label>${__("Company")}
				<select class="adhd-payroll-company">${companyOptions}</select>
			</label>
			<label>${__("Month")}
				<select class="adhd-payroll-month">${monthOptions}</select>
			</label>
			<label>${__("Year")}
				<select class="adhd-payroll-year">${yearOptions}</select>
			</label>
			<button type="button" class="btn btn-xs btn-default adhd-payroll-refresh">${__("Refresh")}</button>
		</div>
	`;
}

const STATE_BADGE = {
	done: { icon: "✓", text: __("Done"), css: "is-done" },
	pending: { icon: "○", text: __("Not yet"), css: "is-pending" },
	unknown: { icon: "?", text: __("Can't auto-detect"), css: "is-unknown" },
	no_access: { icon: "🔒", text: __("No access to check this"), css: "is-no-access" },
};

// A short, plain-English summary of a step's counts. Only counts and statuses are ever shown here — never
// an amount (payroll figures are sensitive; the server itself only returns counts).
function summarize(step) {
	const c = step.counts || {};
	switch (step.reason) {
		case "unmarked":
			return __("{0} employee(s), {1} unmarked day(s)", [
				c.employees_unmarked,
				c.days_unmarked,
			]);
		case "drafts":
			return c.draft_attendance
				? __("{0} draft Attendance record(s)", [c.draft_attendance])
				: __("{0} draft Salary Slip(s)", [c.draft]);
		case "waiting":
			if ("open" in c)
				return __("{0} open, {1} approved but not submitted", [
					c.open,
					c.approved_not_submitted,
				]);
			return __("{0} row(s) waiting", [
				(c.additional_salaries || 0) +
					(c.incentives || 0) +
					(c.arrears || 0) +
					(c.expense_claims || 0),
			]);
		case "no_payroll_entry":
			return __("No Payroll Entry found for this period");
		case "failed":
			return __("{0} Payroll Entry failed", [c.failed]);
		case "queued":
			return __("Salary Slip creation is queued");
		case "draft":
			return __("{0} Payroll Entry in draft", [c.drafts]);
		case "not_in_entry":
			return __("{0} employee(s) not in any Payroll Entry", [c.not_in_entry]);
		case "not_generated":
			return __("No Salary Slips generated yet");
		case "missing":
			return __("{0} employee(s) missing a Salary Slip", [c.missing]);
		case "no_bank_entry":
			return __("No Bank Entry recorded yet");
		case "draft_bank_entry":
			return __("Bank Entry is still a draft");
		case "no_employees":
			return __("No employees are due pay under this company yet");
		case "nothing_waiting":
			return __("Nothing found waiting — confirm everything was entered");
		case "partial_access":
			return __("Some of these rows are outside what you can see");
		case "too_many":
		case "limited_view":
			return __("Too many records to check reliably — verify by hand");
		default:
			if (step.state === "done" && "submitted" in c)
				return __("{0} submitted", [c.submitted]);
			return "";
	}
}

function stepRow(step, serverStep, manualState) {
	const badge = STATE_BADGE[serverStep.state] || STATE_BADGE.unknown;
	const manual = MANUAL_STATES.includes(serverStep.state);
	const checked = manual && Boolean(manualState[step.id]);
	const complete = serverStep.state === "done" || checked;
	const summary = frappe.utils.escape_html(summarize(serverStep));
	const inputId = `adhd-payroll-${step.id}`;

	// A manual step (unknown / no_access) gets a checkbox the person ticks themselves; every step, manual
	// or auto-detected, shows the badge that says what the server found (or why it could not tell).
	const control = manual
		? `<input type="checkbox" id="${inputId}" data-step="${step.id}" ${
				checked ? "checked" : ""
		  }>`
		: `<span class="adhd-payroll-tick ${badge.css}">${complete ? "✓" : "○"}</span>`;
	const labelFor = manual ? ` for="${inputId}"` : "";

	return `
		<div class="adhd-payroll-step ${complete ? "is-complete" : ""}">
			${control}
			<label${labelFor}>${step.label}</label>
			<small>${step.description}</small>
			<span class="adhd-payroll-badge ${badge.css}">${badge.icon} ${badge.text}</span>
			${summary ? `<span class="adhd-payroll-summary">${summary}</span>` : ""}
			<a class="btn btn-xs btn-default" href="${step.link}">${__("Open →")}</a>
		</div>`;
}

async function initPayrollChecklist(container) {
	const $container = $(container);
	if (!isFocusModeActive()) {
		$container.html(`<div class="text-muted">${__("Focus mode is off.")}</div>`);
		return;
	}

	let companies;
	try {
		companies = await fetchCompanies();
	} catch {
		$container.html(`<div class="text-danger">${__("Could not load companies.")}</div>`);
		return;
	}
	if (!companies.default) {
		$container.html(
			`<div class="text-muted">${__("No company is available to check payroll for.")}</div>`,
		);
		return;
	}

	const now = new Date();
	const selected = {
		company: companies.default,
		month: now.getMonth() + 1,
		year: now.getFullYear(),
	};

	const load = async () => {
		if (!isFocusModeActive()) {
			$container.html(`<div class="text-muted">${__("Focus mode is off.")}</div>`);
			return;
		}

		const key = storageKey(selected.company, selected.year, selected.month);
		const manualState = loadManualState(key);

		$container.html(`
			<div class="adhd-payroll-checklist">
				<h4>${__("Payroll Cycle Checklist")}</h4>
				${selectorsHtml(companies, selected)}
				<div class="adhd-payroll-body">
					<span class="text-muted">${__("Checking…")}</span>
				</div>
			</div>
		`);
		bindSelectors($container, selected, load);

		let response;
		try {
			response = await frappe.call({
				method: "hrms.payroll.services.adhd_payroll_cycle.get_payroll_checklist_status",
				args: { company: selected.company, month: selected.month, year: selected.year },
			});
		} catch {
			$container
				.find(".adhd-payroll-body")
				.html(
					`<span class="text-danger">${__(
						"Could not load the checklist for this period.",
					)}</span>`,
				);
			return;
		}

		const data = response.message;
		if (!data) return;

		const complete = hrms.adhd.PAYROLL_STEPS.filter((step) => {
			const serverStep = data.steps[step.id];
			return (
				serverStep.state === "done" ||
				(MANUAL_STATES.includes(serverStep.state) && manualState[step.id])
			);
		}).length;
		const total = hrms.adhd.PAYROLL_STEPS.length;

		const rows = hrms.adhd.PAYROLL_STEPS.map((step) =>
			stepRow(step, data.steps[step.id], manualState),
		).join("");
		$container.find(".adhd-payroll-body").html(`
			<label>${__("{0} of {1} steps complete", [complete, total])}</label>
			<progress max="${total}" value="${complete}"></progress>
			${rows}
		`);

		$container.find("input[data-step]").on("change", (event) => {
			manualState[event.currentTarget.dataset.step] = event.currentTarget.checked;
			try {
				localStorage.setItem(key, JSON.stringify(manualState));
			} catch {
				// storage unavailable: the tick still reflects for this render, just won't survive a reload
			}
			load();
		});
	};

	await load();
}

function bindSelectors($container, selected, reload) {
	$container.find(".adhd-payroll-company").on("change", (event) => {
		selected.company = event.currentTarget.value;
		reload();
	});
	$container.find(".adhd-payroll-month").on("change", (event) => {
		selected.month = Number(event.currentTarget.value);
		reload();
	});
	$container.find(".adhd-payroll-year").on("change", (event) => {
		selected.year = Number(event.currentTarget.value);
		reload();
	});
	$container.find(".adhd-payroll-refresh").on("click", () => reload());
}

hrms.adhd.initPayrollChecklist = initPayrollChecklist;
hrms.adhd.isFocusModeActive = isFocusModeActive;
hrms.adhd.summarizePayrollStep = summarize;
