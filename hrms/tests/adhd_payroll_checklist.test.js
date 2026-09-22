// Node tests for the Payroll Cycle Checklist client (ADHD-057).
//
// Pattern followed from erpnext/tests/adhd_fixes_accounts_and_time.test.js's loadMonthEnd() harness (ADHD-012):
// a vm sandbox with a small stand-in for jQuery/frappe that RECORDS what the script does (the last html a
// selector was given, the last handler registered for a selector+event) rather than a real DOM, plus a
// FakeStorage matching browser Storage semantics. These are structural/behavioural tests of the client only;
// erpnext/tests/test_adhd_server.py's sibling (hrms/tests/test_adhd_payroll_cycle.py) exercises the real
// Python detection logic — the Node suite cannot see whether a query is right, only whether the client wires
// up what the server sends it correctly and never lies about a "done" step.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const jsRoot = path.resolve(__dirname, "../public/js/adhd");
const readAdhd = (name) => fs.readFileSync(path.join(jsRoot, name), "utf8");

const ESCAPES = {
	"&": "&amp;",
	"<": "&lt;",
	">": "&gt;",
	'"': "&quot;",
	"'": "&#39;",
	"`": "&#x60;",
	"=": "&#x3D;",
};
const escapeHtml = (value) => String(value).replace(/[&<>"'`=]/g, (char) => ESCAPES[char] || char);

const STEP_IDS = [
	"attendance",
	"leave",
	"additions",
	"payroll_entry",
	"salary_slips",
	"bank_entry",
];

// Own enumerable properties are the stored keys, like a browser Storage (matches adhd_month_end's tests).
class FakeStorage {
	getItem(key) {
		return Object.hasOwn(this, key) ? this[key] : null;
	}
	setItem(key, value) {
		this[key] = String(value);
	}
	removeItem(key) {
		delete this[key];
	}
}

function doneStep(overrides = {}) {
	return { state: "done", reason: null, counts: { submitted: 3 }, ...overrides };
}
function pendingStep(reason, counts = {}, overrides = {}) {
	return { state: "pending", reason, counts, ...overrides };
}
function unknownStep(reason, counts = {}, overrides = {}) {
	return { state: "unknown", reason, counts, ...overrides };
}
function noAccessStep(overrides = {}) {
	return { state: "no_access", reason: "no_access", counts: {}, ...overrides };
}

function allStepsStatus(steps) {
	return {
		company: "Test Co",
		month: 9,
		year: 2026,
		period_start: "2026-09-01",
		period_end: "2026-09-30",
		steps,
		entries: [],
	};
}

// A node records the last html it was given and the last handler registered per event, keyed by a dotted
// selector path — enough to drive and inspect this script without a real DOM (see the file banner).
function makeSandbox({
	adhdMode = true,
	bootHasFlag = true,
	includeErpnext = true,
	companiesResponse = { companies: ["Test Co", "Other Co"], default: "Test Co" },
	statusResponse = () =>
		allStepsStatus({
			attendance: doneStep(),
			leave: doneStep(),
			additions: unknownStep("nothing_waiting"),
			payroll_entry: doneStep(),
			salary_slips: doneStep(),
			bank_entry: doneStep(),
		}),
	callError = null,
} = {}) {
	const state = { html: {}, handlers: {}, calls: [], storage: new FakeStorage() };

	function node(selectorPath) {
		return {
			html(value) {
				if (value === undefined) return state.html[selectorPath] || "";
				state.html[selectorPath] = value;
				return this;
			},
			find(selector) {
				return node(`${selectorPath} ${selector}`);
			},
			on(event, handler) {
				state.handlers[`${selectorPath}:${event}`] = handler;
				return this;
			},
		};
	}
	const $ = (input) => (typeof input === "string" ? node(input) : node("container"));

	const frappeBoot = bootHasFlag ? { adhd_mode: adhdMode } : {};
	const sandbox = {
		console,
		Promise,
		hrms: { adhd: {} },
		localStorage: state.storage,
		$,
		__: (text, values = []) =>
			values.reduce((result, value, index) => result.replace(`{${index}}`, value), text),
		frappe: {
			provide() {},
			boot: frappeBoot,
			session: { user: "user@example.com" },
			utils: { escape_html: escapeHtml },
			call: async (request) => {
				state.calls.push(request);
				if (callError && request.method.endsWith(callError.method))
					throw new Error("network error");
				if (request.method.endsWith("get_payroll_checklist_companies")) {
					return { message: companiesResponse };
				}
				if (request.method.endsWith("get_payroll_checklist_status")) {
					return { message: statusResponse(request.args) };
				}
				return { message: null };
			},
		},
	};
	if (includeErpnext) sandbox.erpnext = { adhd: { isActive: () => adhdMode } };
	return { sandbox, state };
}

function load(overrides) {
	const { sandbox, state } = makeSandbox(overrides);
	vm.runInNewContext(readAdhd("adhd_payroll_checklist.js"), sandbox, {
		filename: "adhd_payroll_checklist.js",
	});
	return { sandbox, state };
}

// ---- step catalogue ---------------------------------------------------------------------------------------

test("all six steps match the server's STEP_IDS, in the ticket's order", () => {
	const { sandbox } = load();
	const ids = sandbox.hrms.adhd.PAYROLL_STEPS.map((step) => step.id);
	assert.deepEqual(JSON.parse(JSON.stringify(ids)), STEP_IDS);
	// every step needs a real place to send the person, and no step describes itself in ADHD-speak
	for (const step of sandbox.hrms.adhd.PAYROLL_STEPS) {
		assert.match(step.link, /^\/app\//);
		assert.doesNotMatch(step.label, /adhd/i);
		assert.doesNotMatch(step.description, /adhd/i);
	}
});

// ---- mode gate ----------------------------------------------------------------------------------------------

test("mode off (boot flag): shows the off message and calls the server for nothing", async () => {
	const { sandbox, state } = load({ adhdMode: false, bootHasFlag: true });
	await sandbox.hrms.adhd.initPayrollChecklist({});
	assert.match(state.html.container, /Focus mode is off/);
	assert.equal(state.calls.length, 0);
});

test("mode on via the erpnext.adhd fallback when boot carries no flag at all", async () => {
	const { sandbox, state } = load({ bootHasFlag: false, includeErpnext: true, adhdMode: true });
	await sandbox.hrms.adhd.initPayrollChecklist({});
	assert.ok(state.calls.length > 0, "should have fetched companies and status");
});

test("no boot flag and no erpnext global: treated as off, and never throws", async () => {
	const { sandbox, state } = load({ bootHasFlag: false, includeErpnext: false });
	await assert.doesNotReject(sandbox.hrms.adhd.initPayrollChecklist({}));
	assert.match(state.html.container, /Focus mode is off/);
	assert.equal(state.calls.length, 0);
});

// ---- initial load ---------------------------------------------------------------------------------------

test("fetches companies once, then the status for the server's default company and the current period", async () => {
	const { sandbox, state } = load();
	await sandbox.hrms.adhd.initPayrollChecklist({});

	const companyCalls = state.calls.filter((call) =>
		call.method.endsWith("get_payroll_checklist_companies"),
	);
	const statusCalls = state.calls.filter((call) =>
		call.method.endsWith("get_payroll_checklist_status"),
	);
	assert.equal(companyCalls.length, 1);
	assert.equal(statusCalls.length, 1);
	assert.equal(statusCalls[0].args.company, "Test Co");
	assert.equal(statusCalls[0].args.year, new Date().getFullYear());
	assert.ok(statusCalls[0].args.month >= 1 && statusCalls[0].args.month <= 12);
});

test("no company available: says so and never calls for a status", async () => {
	const { sandbox, state } = load({ companiesResponse: { companies: [], default: null } });
	await sandbox.hrms.adhd.initPayrollChecklist({});
	assert.match(state.html.container, /No company is available/);
	assert.equal(
		state.calls.filter((call) => call.method.endsWith("get_payroll_checklist_status")).length,
		0,
	);
});

test("a company name is escaped before it reaches the page", async () => {
	const dangerous = 'Acme <script>alert(1)</script> & "Co"';
	const { sandbox, state } = load({
		companiesResponse: { companies: [dangerous], default: dangerous },
	});
	await sandbox.hrms.adhd.initPayrollChecklist({});
	assert.ok(!state.html.container.includes("<script>"));
	assert.match(state.html.container, /&lt;script&gt;/);
});

test("a failed status call shows an error and does not throw", async () => {
	const { sandbox, state } = load({ callError: { method: "get_payroll_checklist_status" } });
	await assert.doesNotReject(sandbox.hrms.adhd.initPayrollChecklist({}));
	assert.match(state.html["container .adhd-payroll-body"], /Could not load/);
});

// Isolates one step's whole row (a single, non-nested <div>) so a match can be scoped to it, not the page.
function rowFor(body, labelText) {
	const labelIndex = body.indexOf(`>${labelText}</label>`);
	assert.ok(labelIndex >= 0, `row for "${labelText}" not found`);
	const start = body.lastIndexOf('<div class="adhd-payroll-step', labelIndex);
	const end = body.indexOf("</div>", labelIndex) + "</div>".length;
	return body.slice(start, end);
}

// ---- rendering: done / pending / unknown / no_access are never confused -------------------------------------

test("a done step shows no checkbox and is marked complete", async () => {
	const { sandbox, state } = load({
		statusResponse: () =>
			allStepsStatus({
				attendance: doneStep({ counts: { employees_unmarked: 0 } }),
				leave: doneStep(),
				additions: unknownStep("nothing_waiting"),
				payroll_entry: doneStep(),
				salary_slips: doneStep(),
				bank_entry: doneStep(),
			}),
	});
	await sandbox.hrms.adhd.initPayrollChecklist({});
	const row = rowFor(state.html["container .adhd-payroll-body"], "Mark Attendance");
	assert.match(row, /class="adhd-payroll-step is-complete"/);
	// a done step is never given a checkbox to tick — there is nothing to override
	assert.doesNotMatch(row, /type="checkbox"/);
	assert.match(row, /Done/);
});

test("a pending step shows counts, not a checkbox, and is not marked complete", async () => {
	const { sandbox, state } = load({
		statusResponse: () =>
			allStepsStatus({
				attendance: pendingStep("unmarked", { employees_unmarked: 2, days_unmarked: 5 }),
				leave: doneStep(),
				additions: unknownStep("nothing_waiting"),
				payroll_entry: doneStep(),
				salary_slips: doneStep(),
				bank_entry: doneStep(),
			}),
	});
	await sandbox.hrms.adhd.initPayrollChecklist({});
	const row = rowFor(state.html["container .adhd-payroll-body"], "Mark Attendance");
	assert.match(row, /2 employee\(s\), 5 unmarked day\(s\)/);
	assert.doesNotMatch(row, /type="checkbox"/);
	assert.match(row, /class="adhd-payroll-step "/);
});

test("an unknown or no_access step gets a checkbox — the person's own judgement, not a false done", async () => {
	const { sandbox, state } = load({
		statusResponse: () =>
			allStepsStatus({
				attendance: doneStep(),
				leave: doneStep(),
				additions: unknownStep("nothing_waiting"),
				payroll_entry: noAccessStep(),
				salary_slips: doneStep(),
				bank_entry: doneStep(),
			}),
	});
	await sandbox.hrms.adhd.initPayrollChecklist({});
	const body = state.html["container .adhd-payroll-body"];
	assert.match(body, /id="adhd-payroll-additions"/);
	assert.match(body, /id="adhd-payroll-payroll_entry"/);
	assert.match(body, /No access to check this/);
	assert.match(body, /Can't auto-detect/);
	// neither is ticked yet (no stored manual override); the other four steps are done
	assert.match(body, /4 of 6 steps complete/);
});

test("no payroll figure ever appears in the rendered checklist", async () => {
	const { sandbox, state } = load({
		statusResponse: () =>
			allStepsStatus({
				attendance: doneStep(),
				leave: doneStep(),
				additions: pendingStep("waiting", { additional_salaries: 1 }),
				payroll_entry: doneStep(),
				salary_slips: doneStep(),
				bank_entry: doneStep(),
			}),
	});
	await sandbox.hrms.adhd.initPayrollChecklist({});
	const body = state.html["container .adhd-payroll-body"];
	for (const word of ["amount", "net_pay", "gross_pay", "$", "₹"]) {
		assert.ok(!body.toLowerCase().includes(word.toLowerCase()), `must not contain "${word}"`);
	}
});

// ---- selectors reload the status, without a second companies fetch ----------------------------------------

test("changing company, month or year refetches the status only, for the new selection", async () => {
	const { sandbox, state } = load();
	await sandbox.hrms.adhd.initPayrollChecklist({});

	state.handlers["container .adhd-payroll-company:change"]({
		currentTarget: { value: "Other Co" },
	});
	await new Promise((resolve) => setImmediate(resolve));
	state.handlers["container .adhd-payroll-month:change"]({ currentTarget: { value: "3" } });
	await new Promise((resolve) => setImmediate(resolve));
	state.handlers["container .adhd-payroll-year:change"]({ currentTarget: { value: "2020" } });
	await new Promise((resolve) => setImmediate(resolve));

	const companyCalls = state.calls.filter((call) =>
		call.method.endsWith("get_payroll_checklist_companies"),
	);
	const statusCalls = state.calls.filter((call) =>
		call.method.endsWith("get_payroll_checklist_status"),
	);
	assert.equal(companyCalls.length, 1, "companies must be fetched only once and reused");
	assert.equal(statusCalls.length, 4); // initial + company + month + year
	assert.equal(statusCalls.at(-1).args.company, "Other Co");
	assert.equal(statusCalls.at(-1).args.month, 3);
	assert.equal(statusCalls.at(-1).args.year, 2020);
});

test("the refresh button re-fetches the same selection", async () => {
	const { sandbox, state } = load();
	await sandbox.hrms.adhd.initPayrollChecklist({});
	state.handlers["container .adhd-payroll-refresh:click"]();
	await new Promise((resolve) => setImmediate(resolve));
	const statusCalls = state.calls.filter((call) =>
		call.method.endsWith("get_payroll_checklist_status"),
	);
	assert.equal(statusCalls.length, 2);
	assert.equal(statusCalls[0].args.company, statusCalls[1].args.company);
});

// ---- manual ticks persist per user, company and period ------------------------------------------------------

test("ticking a manual step saves it under a key scoped to user, company, year and month", async () => {
	const { sandbox, state } = load({
		statusResponse: () =>
			allStepsStatus({
				attendance: doneStep(),
				leave: doneStep(),
				additions: unknownStep("nothing_waiting"),
				payroll_entry: doneStep(),
				salary_slips: doneStep(),
				bank_entry: doneStep(),
			}),
	});
	await sandbox.hrms.adhd.initPayrollChecklist({});
	const call = state.calls.find((c) => c.method.endsWith("get_payroll_checklist_status"));
	const { company, month, year } = call.args;

	state.handlers["container input[data-step]:change"]({
		currentTarget: { dataset: { step: "additions" }, checked: true },
	});
	await new Promise((resolve) => setImmediate(resolve));

	const expectedKey = `adhd_payroll_user@example.com_${company}_${year}_${String(month).padStart(
		2,
		"0",
	)}`;
	assert.equal(JSON.parse(state.storage.getItem(expectedKey)).additions, true);

	const body = state.html["container .adhd-payroll-body"];
	assert.match(body, /6 of 6 steps complete/);
});

test("a manual tick is read back on the next render and reflected in the progress count", async () => {
	const { sandbox, state } = load({
		statusResponse: () =>
			allStepsStatus({
				attendance: doneStep(),
				leave: doneStep(),
				additions: unknownStep("nothing_waiting"),
				payroll_entry: doneStep(),
				salary_slips: doneStep(),
				bank_entry: doneStep(),
			}),
	});
	const now = new Date();
	const key = `adhd_payroll_user@example.com_Test Co_${now.getFullYear()}_${String(
		now.getMonth() + 1,
	).padStart(2, "0")}`;
	state.storage.setItem(key, JSON.stringify({ additions: true }));

	await sandbox.hrms.adhd.initPayrollChecklist({});
	const body = state.html["container .adhd-payroll-body"];
	assert.match(body, /6 of 6 steps complete/);
	assert.match(body, /id="adhd-payroll-additions"[^>]*checked/);
});

test("corrupted localStorage for the period is ignored, not thrown", async () => {
	const { sandbox, state } = load({
		statusResponse: () =>
			allStepsStatus({
				attendance: doneStep(),
				leave: doneStep(),
				additions: unknownStep("nothing_waiting"),
				payroll_entry: doneStep(),
				salary_slips: doneStep(),
				bank_entry: doneStep(),
			}),
	});
	const now = new Date();
	const key = `adhd_payroll_user@example.com_Test Co_${now.getFullYear()}_${String(
		now.getMonth() + 1,
	).padStart(2, "0")}`;
	state.storage.setItem(key, "{not json");

	await assert.doesNotReject(sandbox.hrms.adhd.initPayrollChecklist({}));
	assert.match(state.html["container .adhd-payroll-body"], /5 of 6 steps complete/);
});

// ---- summarize(): a few representative reasons, directly ------------------------------------------------------

test("summarizePayrollStep never mentions an amount and covers the reasons the server sends", () => {
	const { sandbox } = load();
	const summarize = sandbox.hrms.adhd.summarizePayrollStep;
	assert.match(
		summarize(pendingStep("unmarked", { employees_unmarked: 3, days_unmarked: 9 })),
		/3 employee\(s\), 9 unmarked day\(s\)/,
	);
	assert.match(
		summarize(pendingStep("not_in_entry", { not_in_entry: 4 })),
		/4 employee\(s\) not in any Payroll Entry/,
	);
	assert.equal(summarize(noAccessStep()), "");
	assert.equal(
		summarize(unknownStep("no_employees")),
		"No employees are due pay under this company yet",
	);
});
