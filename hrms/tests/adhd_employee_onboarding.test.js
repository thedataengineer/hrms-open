// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt
//
// Node tests for the Focus "Onboard New Employee" wizard client (ADHD-014).
//
// The wizard's step config, local-draft persistence, payload building and per-step field definitions are
// pure functions (no DOM), so they are loaded in a real `vm` context and executed directly -- this is the
// same style used by erpnext/tests/adhd_form_wizard.test.js and the wider erpnext/tests/adhd_tickets_*.test.js
// suite, whose stand-ins for jQuery/frappe this file's tiny localStorage-only stub mirrors.
//
// The rendering itself needs a real frappe.ui.FieldGroup / frappe.ui.Page (a browser), so instead of a large
// hand-rolled DOM, the rendering, escaping, atomicity-call-site and wiring guarantees are checked against the
// source text -- exactly how adhd_form_wizard.test.js checks the sibling guided-save wizard in the ERPNext repo.
// Field names below were checked against the real doctype JSON under
// ~/code/personal/frappe-bench/apps/{erpnext,hrms}: Employee (first_name, last_name, gender, date_of_birth,
// employment_type, department, designation, company, date_of_joining, employee_number, education child table),
// Employee Education (qualification, school_univ, year_of_passing, level), Address (address_type, address_line1,
// city, country, address_title), Salary Structure Assignment (salary_structure, from_date, base, income_tax_slab),
// Leave Policy Assignment (leave_policy, assignment_based_on, leave_period, effective_from/to).

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const SOURCE_PATH = path.resolve(
	__dirname,
	"../hr/page/employee_onboarding_wizard/employee_onboarding_wizard.js",
);
const SOURCE = fs.readFileSync(SOURCE_PATH, "utf8");

// frappe.utils.escape_html, exactly as Frappe's own utils.js defines it (see frappe/public/js/frappe/utils/utils.js).
const ESCAPES = {
	"&": "&amp;",
	"<": "&lt;",
	">": "&gt;",
	'"': "&quot;",
	"'": "&#39;",
	"`": "&#x60;",
	"=": "&#x3D;",
};
const escapeHtml = (text) =>
	text == null ? "" : String(text).replace(/[&<>"'`=]/g, (c) => ESCAPES[c] || c);

function makeLocalStorage({ throwing = false } = {}) {
	const store = new Map();
	return {
		getItem: (key) => {
			if (throwing) throw new Error("blocked");
			return store.has(key) ? store.get(key) : null;
		},
		setItem: (key, value) => {
			if (throwing) throw new Error("blocked");
			store.set(key, String(value));
		},
		removeItem: (key) => {
			if (throwing) throw new Error("blocked");
			store.delete(key);
		},
		_store: store,
	};
}

// Values built by code running inside the vm context come from that context's own realm (its own Array /
// Object constructors), so assert's *strict* deep-equal -- which also compares prototypes -- reports them as
// unequal to an outer-realm literal even when every field matches. Round-tripping through JSON gives back a
// plain outer-realm value for comparison; every value compared this way here is plain JSON-safe data.
function toPlain(value) {
	return JSON.parse(JSON.stringify(value));
}

function loadWizard({ localStorage } = {}) {
	const sandbox = {
		console,
		window: {},
		localStorage: localStorage || makeLocalStorage(),
		__: (text, values = []) =>
			values.reduce((out, value, i) => out.replace(`{${i}}`, value), text),
		frappe: {
			provide() {},
			pages: { "employee-onboarding-wizard": {} },
			session: { user: "hr.user@example.com" },
			boot: { adhd_mode: false },
			utils: { escape_html: escapeHtml },
			ui: {},
		},
		erpnext: { adhd: {} },
	};
	vm.createContext(sandbox);
	vm.runInContext(SOURCE, sandbox, { filename: SOURCE_PATH });
	return sandbox;
}

// ---- Step configuration -----------------------------------------------------------------------------------

test("seven steps, in order, one thing per screen", () => {
	const { erpnext } = loadWizard();
	const ids = erpnext.adhd.onboardingWizard.WIZARD_STEPS.map((s) => s.id);
	assert.deepEqual(toPlain(ids), [
		"who",
		"job",
		"education",
		"address",
		"salary",
		"leave",
		"review",
	]);
});

test("only Employee (who/job) and the review confirmation are required; the rest can be skipped", () => {
	const { erpnext } = loadWizard();
	const byId = Object.fromEntries(
		erpnext.adhd.onboardingWizard.WIZARD_STEPS.map((s) => [s.id, s]),
	);
	assert.equal(byId.who.skippable, false);
	assert.equal(byId.job.skippable, false);
	assert.equal(byId.review.skippable, false);
	for (const id of ["education", "address", "salary", "leave"]) {
		assert.equal(byId[id].skippable, true, `${id} should be skippable`);
	}
});

test("stepAt clamps out-of-range indexes instead of returning undefined", () => {
	const { erpnext } = loadWizard();
	const { stepAt, WIZARD_STEPS } = erpnext.adhd.onboardingWizard;
	assert.equal(stepAt(-5).id, "who");
	assert.equal(stepAt(999).id, WIZARD_STEPS[WIZARD_STEPS.length - 1].id);
	assert.equal(stepAt(2).id, "education");
});

test("stepIndex finds a step by id and reports -1 for an unknown one", () => {
	const { erpnext } = loadWizard();
	const { stepIndex } = erpnext.adhd.onboardingWizard;
	assert.equal(stepIndex("salary"), 4);
	assert.equal(stepIndex("not-a-step"), -1);
});

test("progressText reads 'Step N of TOTAL — Label', one-based", () => {
	const { erpnext } = loadWizard();
	const { progressText, WIZARD_STEPS } = erpnext.adhd.onboardingWizard;
	assert.equal(progressText(0), `Step 1 of ${WIZARD_STEPS.length} — Who's Joining`);
	assert.equal(progressText(3), `Step 4 of ${WIZARD_STEPS.length} — Address`);
});

// ---- Local draft persistence (progressive save, nothing lost) --------------------------------------------

test("draftKey is scoped per user, so two people never share a draft", () => {
	const { erpnext } = loadWizard();
	const { draftKey } = erpnext.adhd.onboardingWizard;
	assert.notEqual(draftKey("alice@example.com"), draftKey("bob@example.com"));
	assert.match(draftKey("alice@example.com"), /alice@example\.com$/);
});

test("emptyDraft starts at step 0, nothing skipped, education as a list", () => {
	const { erpnext } = loadWizard();
	const draft = toPlain(erpnext.adhd.onboardingWizard.emptyDraft());
	assert.equal(draft.index, 0);
	assert.deepEqual(draft.skipped, []);
	assert.equal(draft.submit_now, true);
	assert.ok(Array.isArray(draft.education));
	for (const key of ["who", "job", "address", "salary", "leave"]) {
		assert.deepEqual(draft[key], {});
	}
});

test("loadDraft returns an empty draft when nothing was saved yet", () => {
	const { erpnext } = loadWizard();
	const draft = erpnext.adhd.onboardingWizard.loadDraft("new.user@example.com");
	assert.deepEqual(toPlain(draft), toPlain(erpnext.adhd.onboardingWizard.emptyDraft()));
});

test("saveDraft then loadDraft round-trips, and backfills fields an older draft did not have", () => {
	const storage = makeLocalStorage();
	const { erpnext } = loadWizard({ localStorage: storage });
	const { saveDraft, loadDraft, draftKey } = erpnext.adhd.onboardingWizard;
	const user = "hr.user@example.com";
	// an older / partial draft missing e.g. "leave" and "submit_now"
	storage.setItem(draftKey(user), JSON.stringify({ index: 2, who: { first_name: "Jo" } }));
	const draft = loadDraft(user);
	assert.equal(draft.index, 2);
	assert.equal(draft.who.first_name, "Jo");
	assert.deepEqual(toPlain(draft.leave), {}); // backfilled, not left undefined
	assert.equal(draft.submit_now, true); // backfilled default

	saveDraft(user, { ...draft, index: 5 });
	assert.equal(loadDraft(user).index, 5);
});

test("loadDraft never throws on corrupted JSON; it starts fresh instead", () => {
	const storage = makeLocalStorage();
	const { erpnext } = loadWizard({ localStorage: storage });
	const { loadDraft, draftKey } = erpnext.adhd.onboardingWizard;
	storage.setItem(draftKey("x@example.com"), "{not json");
	assert.deepEqual(
		toPlain(loadDraft("x@example.com")),
		toPlain(erpnext.adhd.onboardingWizard.emptyDraft()),
	);
});

test("a localStorage that throws (private window, quota) never breaks load/save/clear", () => {
	const { erpnext } = loadWizard({ localStorage: makeLocalStorage({ throwing: true }) });
	const { loadDraft, saveDraft, clearDraft } = erpnext.adhd.onboardingWizard;
	assert.deepEqual(
		toPlain(loadDraft("x@example.com")),
		toPlain(erpnext.adhd.onboardingWizard.emptyDraft()),
	);
	assert.doesNotThrow(() => saveDraft("x@example.com", { index: 1 }));
	assert.doesNotThrow(() => clearDraft("x@example.com"));
});

// ---- Payload building: skipped steps are never sent, so the server can't act on stale answers ------------

test("buildPayload passes unskipped steps through untouched", () => {
	const { erpnext } = loadWizard();
	const draft = erpnext.adhd.onboardingWizard.emptyDraft();
	draft.who = { first_name: "Jo" };
	draft.job = { company: "Acme", date_of_joining: "2026-01-05" };
	draft.address = { city: "Austin" };
	const payload = toPlain(erpnext.adhd.onboardingWizard.buildPayload(draft));
	assert.deepEqual(payload.who, draft.who);
	assert.deepEqual(payload.job, draft.job);
	assert.deepEqual(payload.address, draft.address);
});

test("buildPayload blanks a step's data once it is skipped, even if the draft still holds old values", () => {
	const { erpnext } = loadWizard();
	const draft = erpnext.adhd.onboardingWizard.emptyDraft();
	draft.address = { city: "Austin" }; // filled in, then the person went back and skipped it
	draft.salary = { salary_structure: "SS-1" };
	draft.education = [{ qualification: "BSc" }];
	draft.skipped = ["address", "salary", "education"];
	const payload = toPlain(erpnext.adhd.onboardingWizard.buildPayload(draft));
	assert.deepEqual(payload.address, {});
	assert.deepEqual(payload.salary, {});
	assert.deepEqual(payload.education, []);
	assert.deepEqual(payload.skipped, ["address", "salary", "education"]);
});

test("buildPayload sends options.submit as a real boolean", () => {
	const { erpnext } = loadWizard();
	const draft = erpnext.adhd.onboardingWizard.emptyDraft();
	draft.submit_now = false;
	assert.equal(erpnext.adhd.onboardingWizard.buildPayload(draft).options.submit, false);
	draft.submit_now = true;
	assert.equal(erpnext.adhd.onboardingWizard.buildPayload(draft).options.submit, true);
});

// ---- Per-step fields: what a person is actually asked, and only what applies to this site -----------------

test("who: employee_number only appears when HR Settings names employees that way", () => {
	const { erpnext } = loadWizard();
	const { buildFieldsForStep } = erpnext.adhd.onboardingWizard;
	const withoutNumber = buildFieldsForStep("who", { naming: "Naming Series" }, {});
	assert.ok(!withoutNumber.some((f) => f.fieldname === "employee_number"));

	const withNumber = buildFieldsForStep("who", { naming: "Employee Number" }, {});
	const numberField = withNumber.find((f) => f.fieldname === "employee_number");
	assert.ok(numberField && numberField.reqd === 1);
});

test("who: first_name and gender are required, matching the real Employee doctype", () => {
	const { erpnext } = loadWizard();
	const fields = erpnext.adhd.onboardingWizard.buildFieldsForStep(
		"who",
		{ naming: "Naming Series" },
		{},
	);
	const byName = Object.fromEntries(fields.map((f) => [f.fieldname, f]));
	assert.equal(byName.first_name.reqd, 1);
	assert.equal(byName.gender.reqd, 1);
	assert.equal(byName.gender.options, "Gender");
	assert.equal(byName.date_of_birth.reqd, 1); // Employee.date_of_birth is reqd on the real doctype
	assert.ok(!byName.last_name.reqd); // Employee.last_name is not mandatory on the doctype
});

test("job: employment_type is offered only when the site's Employee doctype has that field", () => {
	const { erpnext } = loadWizard();
	const { buildFieldsForStep } = erpnext.adhd.onboardingWizard;
	const withType = buildFieldsForStep("job", { has_employment_type: true }, {});
	assert.ok(withType.some((f) => f.fieldname === "employment_type"));
	const withoutType = buildFieldsForStep("job", { has_employment_type: false }, {});
	assert.ok(!withoutType.some((f) => f.fieldname === "employment_type"));
});

test("job: company defaults from context, department is scoped to the chosen company", () => {
	const { erpnext } = loadWizard();
	const { buildFieldsForStep } = erpnext.adhd.onboardingWizard;
	const fields = buildFieldsForStep(
		"job",
		{ default_company: "Yadavilli Solutions" },
		{ job: { company: "Acme" } },
	);
	const byName = Object.fromEntries(fields.map((f) => [f.fieldname, f]));
	assert.equal(byName.company.default, "Yadavilli Solutions");
	assert.equal(byName.company.reqd, 1);
	assert.equal(byName.date_of_joining.reqd, 1);
	assert.deepEqual(toPlain(byName.department.get_query()), { filters: { company: "Acme" } });
});

test("address: address_type choices come from the doctype's own options, with a blank first choice", () => {
	const { erpnext } = loadWizard();
	const fields = erpnext.adhd.onboardingWizard.buildFieldsForStep(
		"address",
		{ address_types: ["Billing", "Current"] },
		{},
	);
	const type = fields.find((f) => f.fieldname === "address_type");
	assert.equal(type.options, "\nBilling\nCurrent");
	assert.equal(type.reqd, 1);
	assert.equal(fields.find((f) => f.fieldname === "city").reqd, 1);
	assert.equal(fields.find((f) => f.fieldname === "country").options, "Country");
});

test("salary: only submitted, active structures for this company are offered; start date defaults to the joining date", () => {
	const { erpnext } = loadWizard();
	const draft = { job: { company: "Acme", date_of_joining: "2026-02-01" } };
	const fields = erpnext.adhd.onboardingWizard.buildFieldsForStep("salary", null, draft);
	const byName = Object.fromEntries(fields.map((f) => [f.fieldname, f]));
	assert.deepEqual(toPlain(byName.salary_structure.get_query()), {
		filters: { company: "Acme", docstatus: 1, is_active: "Yes" },
	});
	assert.equal(byName.from_date.default, "2026-02-01");
	assert.equal(byName.salary_structure.reqd, 1);
});

test("leave: leave_period only applies (and is only required) when the period is period-based", () => {
	const { erpnext } = loadWizard();
	const draft = { job: { company: "Acme" } };
	const fields = erpnext.adhd.onboardingWizard.buildFieldsForStep("leave", null, draft);
	const byName = Object.fromEntries(fields.map((f) => [f.fieldname, f]));
	const expr = 'eval:doc.assignment_based_on=="Leave Period"';
	assert.equal(byName.leave_period.depends_on, expr);
	assert.equal(byName.leave_period.mandatory_depends_on, expr);
	assert.deepEqual(toPlain(byName.leave_period.get_query()), {
		filters: { company: "Acme", is_active: 1 },
	});
	assert.equal(byName.leave_policy.reqd, 1);
	assert.equal(byName.assignment_based_on.options, "\nLeave Period\nJoining Date");
});

test("education and review steps are not FieldGroup screens (education is hand-rolled rows; review is a summary)", () => {
	const { erpnext } = loadWizard();
	const { buildFieldsForStep } = erpnext.adhd.onboardingWizard;
	assert.equal(buildFieldsForStep("education", {}, {}), null);
	assert.equal(buildFieldsForStep("review", {}, {}), null);
});

test("the Page controller hook is wired for the route this wizard's Page JSON declares", () => {
	const { frappe } = loadWizard();
	assert.equal(typeof frappe.pages["employee-onboarding-wizard"].on_page_load, "function");
});

test("getState() is exposed on the wizard class for the ADHD-020 test harness", () => {
	assert.match(SOURCE, /getState\(\)\s*\{/);
	assert.match(SOURCE, /step:\s*stepAt\(this\.draft\.index\)\.id/);
});

// ---- Source-level checks for the DOM/rendering half (needs a real frappe.ui.FieldGroup + browser) ---------
// Mirrors erpnext/tests/adhd_form_wizard.test.js's approach for the sibling guided-save wizard.

test("class names never collide with the ERPNext guided-save wizard's adhd-wizard-* classes", () => {
	assert.ok(
		!/["'\s]adhd-wizard-/.test(SOURCE),
		"found a bare adhd-wizard- class; use adhd-onb- instead",
	);
	assert.match(SOURCE, /adhd-onboarding-wizard/);
	assert.match(SOURCE, /adhd-onb-progress/);
});

test("nothing is written to the server until Confirm: the Confirm button starts disabled", () => {
	assert.match(SOURCE, /adhd-onb-confirm"\s+disabled>/);
});

test("the atomic create call has exactly one call site, inside confirm()", () => {
	const occurrences = SOURCE.split("METHOD_CREATE").length - 1;
	assert.equal(occurrences, 2, "expected one const declaration + one use of METHOD_CREATE");
	const confirmBody = SOURCE.slice(
		SOURCE.indexOf("async confirm()"),
		SOURCE.indexOf("_renderCreateFailure("),
	);
	assert.match(confirmBody, /METHOD_CREATE/);
});

test("Skip never calls the server: it only marks the step and schedules a reminder locally", () => {
	const skipBody = SOURCE.slice(
		SOURCE.indexOf("skipStep() {"),
		SOURCE.indexOf("async confirm()"),
	);
	assert.ok(
		!/METHOD_CHECK|METHOD_CREATE|xcall/.test(skipBody),
		"skipStep should not talk to the server",
	);
	assert.match(skipBody, /this\.draft\.skipped\.push\(step\.id\)/);
	assert.match(skipBody, /if \(!step\.skippable\) return;/);
});

test("Next validates the current step against the server before advancing, and shows the field-named errors on failure", () => {
	const nextBody = SOURCE.slice(
		SOURCE.indexOf("\n\t\tasync goNext()"),
		SOURCE.indexOf("\n\t\tgoBack()"),
	);
	assert.match(nextBody, /METHOD_CHECK/);
	assert.match(nextBody, /step:\s*step\.id/); // only this step's errors are asked for
	assert.match(nextBody, /if \(!result \|\| !result\.ok\)/);
	assert.match(nextBody, /this\._showErrors/);

	// Ordering, not just presence: the server call, then the ok-check-and-early-return, then (only after
	// that) the step index advances. Checked by position rather than a regex gap, since a regex looking for
	// "index changes shortly before the check" cannot describe "index changes before the check" in general.
	const checkCallAt = nextBody.indexOf("frappe.xcall(METHOD_CHECK");
	const earlyReturnAt = nextBody.indexOf("if (!result || !result.ok)");
	const indexAdvanceAt = nextBody.indexOf("this.draft.index = Math.min(");
	assert.ok(
		checkCallAt >= 0 && earlyReturnAt >= 0 && indexAdvanceAt >= 0,
		"expected all three to be present",
	);
	assert.ok(
		checkCallAt < earlyReturnAt && earlyReturnAt < indexAdvanceAt,
		"the step index must not advance before the server check comes back ok",
	);
});

test("field-level errors are attached to the named field's own wrapper, not just a generic banner", () => {
	assert.match(SOURCE, /error\.field && this\.form && this\.form\.fields_dict\[error\.field\]/);
	assert.match(SOURCE, /\$wrapper\.addClass\("has-error"\)/);
	assert.match(SOURCE, /adhd-field-error/);
});

test("every server- or person-supplied string interpolated into markup is escaped", () => {
	const risky = [
		/\$\{escapeHtml\(progressText\(index\)\)\}/,
		/\$\{escapeHtml\(doc\.label\)\}/,
		/\$\{escapeHtml\(doc\.status\)\}/,
		/\$\{escapeHtml\(error\.message\)\}/,
		/\$\{escapeHtml\(line\.text\)\}/,
		/escapeHtml\(\s*__\("\{0\} onboarded successfully", \[result\.employee_name\]\),?\s*\)/,
		/\$\{escapeHtml\(values\.qualification/,
		/\$\{escapeHtml\(values\.school_univ/,
		/\$\{escapeHtml\(reminder\.label\)\}/,
	];
	for (const pattern of risky) {
		assert.match(SOURCE, pattern, `expected escaped interpolation matching ${pattern}`);
	}
	// get_form_link (not escapeHtml) is the right call for a document link -- it escapes the name itself
	// (frappe.utils.get_form_link -> frappe.utils.escape_html internally) and returns an <a> tag.
	assert.match(SOURCE, /frappe\.utils\.get_form_link\(doc\.doctype, doc\.name, true\)/);
});

test("Focus mode only changes decorative styling; it never gates whether a step can be completed", () => {
	assert.match(SOURCE, /toggleClass\("adhd-mode-on"/);
	// the navigation methods must not early-return based on adhd_mode. Anchored on "\n\t\t<method>" (the
	// method's own definition, at its class-body indentation) rather than a bare substring match, because
	// "goNext()" etc. also appear earlier as call sites (e.g. `.on("click", () => this.goNext())`), and a
	// plain indexOf would find those instead of the method body this test means to inspect.
	for (const method of ["async goNext()", "goBack()", "skipStep()", "async confirm()"]) {
		const start = SOURCE.indexOf(`\n\t\t${method}`);
		assert.ok(start >= 0, `method ${method} not found`);
		const body = SOURCE.slice(start, start + 400);
		assert.ok(
			!/adhd_mode/.test(body),
			`${method} must not read adhd_mode to decide whether to act`,
		);
	}
});

test("the success screen offers to open the Employee record and to start again, and only offers the classic Employee Onboarding checklist as a link the person chooses (never auto-created)", () => {
	assert.match(SOURCE, /adhd-onb-restart[\s\S]{0,120}\.on\("click", \(\) => this\.reset\(\)\)/);
	assert.match(
		SOURCE,
		/adhd-onb-goto-employee[\s\S]{0,150}frappe\.set_route\("Form", "Employee", result\.employee\)/,
	);
	assert.match(SOURCE, /can_create\.onboarding\)\s*\{/);
	assert.match(SOURCE, /frappe\.new_doc\("Employee Onboarding"/);
	// starting the checklist must never itself go through the atomic-create endpoint
	const onboardingBlock = SOURCE.slice(
		SOURCE.indexOf("can_create.onboarding"),
		SOURCE.indexOf("reset() {"),
	);
	assert.ok(!/METHOD_CREATE/.test(onboardingBlock));
});

test("a create failure names the step and offers to go back to it without losing the draft", () => {
	assert.match(SOURCE, /_renderCreateFailure/);
	assert.match(SOURCE, /this\.draft\.index = targetIndex;/);
	assert.match(SOURCE, /this\._persist\(\);\s*\n\s*this\.render\(\);/);
});
