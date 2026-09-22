// Hubble's registrations with RTB's Focus aids (hrms/public/js/adhd/adhd_focus_registrations.js). The module
// runs in a vm against a recording stand-in for `erpnext.adhd`; the field names it registers are checked
// against the real doctype JSON files in this repository, so a renamed field fails here before it fails on a
// form.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const appRoot = path.resolve(__dirname, "..");
const source = fs.readFileSync(
	path.join(appRoot, "public/js/adhd/adhd_focus_registrations.js"),
	"utf8",
);
const translate = (text, values = []) =>
	values.reduce((result, value, index) => result.replace(`{${index}}`, value), text);
const plain = (value) => JSON.parse(JSON.stringify(value));

// The doctype JSON, found under any module folder (`hrms/<module>/doctype/<slug>/<slug>.json`).
function doctypeJson(doctype) {
	const slug = doctype.toLowerCase().replace(/ /g, "_");
	const modules = fs
		.readdirSync(appRoot, { withFileTypes: true })
		.filter((entry) => entry.isDirectory());
	for (const module of modules) {
		const file = path.join(appRoot, module.name, "doctype", slug, `${slug}.json`);
		if (fs.existsSync(file)) return JSON.parse(fs.readFileSync(file, "utf8"));
	}
	throw new Error(`no doctype JSON for ${doctype}`);
}
const fieldsOf = (doctype) =>
	new Map(doctypeJson(doctype).fields.map((field) => [field.fieldname, field]));
const workspaceNames = () => {
	const names = [];
	for (const module of fs.readdirSync(appRoot, { withFileTypes: true })) {
		const dir = path.join(appRoot, module.name, "workspace");
		if (!module.isDirectory() || !fs.existsSync(dir)) continue;
		for (const workspace of fs.readdirSync(dir)) {
			const file = path.join(dir, workspace, `${workspace}.json`);
			if (fs.existsSync(file)) names.push(JSON.parse(fs.readFileSync(file, "utf8")).name);
		}
	}
	return names;
};

function load({ withFunctions = true } = {}) {
	const calls = {
		deadline: [],
		timebox: [],
		listDate: [],
		inboxModule: [],
		draft: [],
		feature: [],
	};
	const adhd = withFunctions
		? {
				registerDeadline: (doctype, config) => calls.deadline.push([doctype, config]),
				registerTimebox: (doctype) => calls.timebox.push(doctype),
				registerListDate: (...args) => calls.listDate.push(args),
				registerInboxModule: (name) => calls.inboxModule.push(name),
				registerDraftRecovery: (doctype, config) => calls.draft.push([doctype, config]),
				registerFeature: (feature) => calls.feature.push(feature),
		  }
		: {};
	const sandbox = {
		__: translate,
		cint: (value) => parseInt(value, 10) || 0,
		frappe: {},
		erpnext: { adhd },
	};
	// frappe.provide as the desk has it: the namespace is created on the global object if it is missing
	sandbox.frappe.provide = (namespace) => {
		let root = sandbox;
		for (const part of namespace.split(".")) root = root[part] ||= {};
	};
	vm.runInNewContext(source, sandbox, { filename: "adhd_focus_registrations.js" });
	return { calls, sandbox, registrations: sandbox.hrms.focus.registrations };
}

test("every registration reaches RTB, and an RTB without the functions is left alone", () => {
	const { calls, registrations } = load();
	assert.equal(calls.deadline.length, Object.keys(registrations.DEADLINES).length);
	assert.deepEqual(plain(calls.timebox), plain(registrations.TIMEBOXED));
	assert.equal(calls.listDate.length, Object.keys(registrations.LIST_DATES).length);
	assert.deepEqual(plain(calls.inboxModule), plain(registrations.MODULES));
	assert.equal(calls.draft.length, Object.keys(registrations.DRAFTS).length);
	assert.deepEqual(plain(calls.feature), [
		{ key: "hr_form_aids", label: "Leave Balance and Receipt Checks", defaultOn: true },
	]);

	const older = load({ withFunctions: false });
	assert.deepEqual(
		plain(older.calls),
		plain({
			deadline: [],
			timebox: [],
			listDate: [],
			inboxModule: [],
			draft: [],
			feature: [],
		}),
	);
});

test("every deadline names a real date field of its doctype and speaks in plain words", () => {
	const { calls } = load();
	for (const [doctype, config] of calls.deadline) {
		const field = fieldsOf(doctype).get(config.dateField);
		assert.ok(field, `${doctype}.${config.dateField}`);
		assert.ok(
			["Date", "Datetime"].includes(field.fieldtype),
			`${doctype}.${config.dateField} is ${field.fieldtype}`,
		);
		for (const days of [-3, -1, 0, 1, 5]) {
			const text = config.message(days);
			assert.ok(text.length > 8, `${doctype} ${days}`);
			assert.doesNotMatch(text, /ADHD|ERPNext|HRMS|Frappe HR/, `${doctype} ${days}`);
			if (days !== 0)
				assert.ok(text.includes(String(Math.abs(days))), `${doctype} ${days}: ${text}`);
		}
		assert.equal(config.message(-1).includes("days"), false, `${doctype}: one day, no plural`);
	}
});

test("a banner shows only while the document still needs the person", () => {
	const { registrations } = load();
	const { DEADLINES } = registrations;
	assert.equal(DEADLINES["Leave Application"].showWhen({ docstatus: 0, status: "Open" }), true);
	assert.equal(
		DEADLINES["Leave Application"].showWhen({ docstatus: 1, status: "Approved" }),
		false,
	);
	assert.equal(
		DEADLINES["Leave Application"].showWhen({ docstatus: 0, status: "Rejected" }),
		false,
	);
	assert.equal(DEADLINES.Interview.showWhen({ docstatus: 1, status: "Pending" }), true);
	assert.equal(DEADLINES.Interview.showWhen({ docstatus: 1, status: "Cleared" }), false);
	assert.equal(DEADLINES.Appraisal.showWhen({ docstatus: 0 }), true);
	assert.equal(DEADLINES.Appraisal.showWhen({ docstatus: 1 }), false);
	assert.equal(DEADLINES["Payroll Entry"].showWhen({ docstatus: "0" }), true);
	assert.equal(DEADLINES["Payroll Entry"].showWhen({ docstatus: 1 }), false);
	assert.equal(
		DEADLINES["Employee Onboarding"].showWhen({ docstatus: 1, boarding_status: "In Process" }),
		true,
	);
	assert.equal(
		DEADLINES["Employee Onboarding"].showWhen({ docstatus: 1, boarding_status: "Completed" }),
		false,
	);
	assert.equal(
		DEADLINES["Employee Onboarding"].showWhen({ docstatus: 2, boarding_status: "Pending" }),
		false,
	);
	assert.equal(DEADLINES["Training Event"].showWhen({ event_status: "Scheduled" }), true);
	assert.equal(DEADLINES["Training Event"].showWhen({ event_status: "Completed" }), false);
});

test("the statuses that close a deadline, and the list date fields, exist on their doctypes", () => {
	const { calls } = load();
	for (const [doctype, dateField, closed] of calls.listDate) {
		const fields = fieldsOf(doctype);
		assert.ok(fields.get(dateField), `${doctype}.${dateField}`);
		assert.equal(fields.get(dateField).fieldtype, "Date", `${doctype}.${dateField}`);
		const options = (fields.get("status")?.options || "").split("\n");
		for (const status of closed)
			assert.ok(options.includes(status), `${doctype} status ${status}`);
	}
	for (const [doctype, config] of calls.deadline) {
		if (doctype === "Leave Application") assert.equal(config.dateField, "from_date");
	}
});

test("the time-boxed forms and the inbox modules are real doctypes and workspaces", () => {
	const { calls } = load();
	for (const doctype of calls.timebox) assert.ok(doctypeJson(doctype), doctype);
	const names = workspaceNames();
	for (const name of calls.inboxModule) assert.ok(names.includes(name), `workspace ${name}`);
});

test("every draft field and table is a real field, and the tables name their real child doctype", () => {
	const { calls } = load();
	for (const [doctype, config] of calls.draft) {
		const fields = fieldsOf(doctype);
		for (const name of [...config.party, ...config.scalars, ...(config.lateScalars || [])]) {
			assert.ok(fields.get(name), `${doctype}.${name}`);
			assert.notEqual(fields.get(name).fieldtype, "Table", `${doctype}.${name} is a table`);
		}
		for (const [table, spec] of Object.entries(config.tables)) {
			const field = fields.get(table);
			assert.ok(field && field.fieldtype === "Table", `${doctype}.${table}`);
			assert.equal(field.options, spec.doctype, `${doctype}.${table} child doctype`);
			const childFields = fieldsOf(spec.doctype);
			for (const name of [...spec.identity, ...spec.fields])
				assert.ok(childFields.get(name), `${spec.doctype}.${name}`);
		}
		for (const table of config.contentTables)
			assert.ok(config.tables[table], `${doctype} content table ${table}`);
	}
	// the employee is filled in on its own, so it must not be what marks a form as work in progress
	const [, claim] = calls.draft.find(([doctype]) => doctype === "Expense Claim");
	assert.deepEqual(plain(claim.party), []);
	assert.deepEqual(plain(claim.contentTables), ["expenses"]);
	const [, leave] = calls.draft.find(([doctype]) => doctype === "Leave Application");
	assert.deepEqual(plain(leave.party), ["leave_type"]);
});
