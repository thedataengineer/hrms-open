// ADHD-058 (Leave Application "Balance at a Glance") and ADHD-059 (Expense Claim "Receipt Attached?").
// Both real client modules run in a vm against a small stand-in for the parts of Frappe/the form grid they
// touch, following the pattern of erpnext/tests/adhd_tickets_021_030_receiving.test.js: only what Frappe
// really exposes is provided (`layout.wrapper`, no `layout.$wrapper`; `frm.fields_dict.<f>.$wrapper`, not
// `.wrapper`), so a misused property fails a test instead of quietly doing nothing.
//
// `frappe.utils.debounce` is the REAL algorithm from
// frappe-bench/apps/frappe/frappe/public/js/frappe/utils/utils.js:960 (copied verbatim below, with real
// setTimeout/clearTimeout), not a passthrough stub — otherwise the "only one call when typing fast" guarantee
// would never actually be exercised.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const appRoot = path.resolve(__dirname, "..");
const jsRoot = path.join(appRoot, "public/js/adhd");
const read = (name) => fs.readFileSync(path.join(jsRoot, name), "utf8");
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// frappe.utils.escape_html, frappe/public/js/frappe/utils/utils.js (the map ERPNext's own tests use too)
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
	String(text == null ? "" : text).replace(/[&<>"'`=]/g, (c) => ESCAPES[c] || c);
const translate = (text, values = []) =>
	values.reduce((result, value, index) => result.replace(`{${index}}`, value), text);
const flt = (value) => Number.parseFloat(value) || 0;
const format_number = (value, _fmt, decimals) => Number(value).toFixed(decimals ?? 0);

// verbatim port of frappe.utils.debounce (frappe/public/js/frappe/utils/utils.js:960)
function debounce(func, wait_ms) {
	let timeout, context, args;
	const later = () => {
		timeout = null;
		func.apply(context, args);
	};
	const debounced = function () {
		context = this;
		args = arguments;
		clearTimeout(timeout);
		timeout = setTimeout(later, wait_ms);
	};
	debounced.cancel = () => {
		if (!timeout) return false;
		clearTimeout(timeout);
		timeout = null;
		return true;
	};
	return debounced;
}

function hasClass(html, selector) {
	if (selector.startsWith("#")) return new RegExp(`id="${selector.slice(1)}"`).test(html);
	const cls = selector.slice(1);
	return new RegExp(`class="(?:[^"]*\\s)?${cls}(?:\\s[^"]*)?"`).test(html);
}

// A minimal page: top-level panels (the leave card, the expense summary) plus, for the expense grid, one
// cell-list per child row. Both the leave card and the expense summary sit under `frm.layout.wrapper` in the
// real DOM, so `.find(selector).remove()` there must reach both, exactly like a real `.find()` would.
function makeDom() {
	const panels = [];
	const gridRows = {}; // fieldname -> rowname -> { cells: [] }

	const asHtml = (elOrHtml) => (typeof elOrHtml === "string" ? elOrHtml : elOrHtml.html);

	function fieldWrapper(fieldname) {
		return {
			length: 1,
			after(elOrHtml) {
				panels.push({ fieldname, html: asHtml(elOrHtml) });
			},
		};
	}

	function gridRowHandle(fieldname, rowname) {
		gridRows[fieldname] = gridRows[fieldname] || {};
		gridRows[fieldname][rowname] = gridRows[fieldname][rowname] || { cells: [] };
		const row = gridRows[fieldname][rowname];
		return {
			length: 1,
			append(elOrHtml) {
				row.cells.push(asHtml(elOrHtml));
			},
			find(selector) {
				return { length: row.cells.some((html) => hasClass(html, selector)) ? 1 : 0 };
			},
		};
	}

	function layoutWrapper() {
		return {
			find(selector) {
				return {
					remove() {
						for (let i = panels.length - 1; i >= 0; i--) {
							if (hasClass(panels[i].html, selector)) panels.splice(i, 1);
						}
						for (const fieldname of Object.keys(gridRows)) {
							for (const rowname of Object.keys(gridRows[fieldname])) {
								const row = gridRows[fieldname][rowname];
								row.cells = row.cells.filter((html) => !hasClass(html, selector));
							}
						}
					},
				};
			},
		};
	}

	return {
		$: (target) => (typeof target === "string" ? { html: target, length: 1 } : target),
		fieldWrapper,
		gridRowHandle,
		layoutWrapper,
		panels,
		gridRows,
		panelHtml: () => panels.map((p) => p.html).join("\n"),
		badgesOn: (fieldname, rowname) => (gridRows[fieldname]?.[rowname]?.cells || []).length,
		totalBadges: () =>
			Object.values(gridRows).reduce(
				(sum, rows) => sum + Object.values(rows).reduce((s, r) => s + r.cells.length, 0),
				0,
			),
	};
}

function handlerFor(registrations, doctype, event) {
	const found = registrations.find(
		(entry) => entry.doctype === doctype && entry.handlers[event],
	);
	return found && found.handlers[event];
}

function makeSandbox({ dom, adhdMode = true, metaByDoctype = {}, respond } = {}) {
	const window = { cur_frm: null };
	const registrations = [];
	const stateHandlers = [];
	const calls = [];
	const sandbox = {
		console,
		Promise,
		setTimeout,
		clearTimeout,
		window,
		$: dom.$,
		__: translate,
		flt,
		format_number,
		frappe: {
			boot: { adhd_mode: adhdMode },
			provide() {},
			call: async (options) => {
				calls.push(options);
				const message = respond ? respond(options) : null;
				if (message === "REJECT") throw new Error("simulated failure");
				return { message };
			},
			ui: { form: { on: (doctype, handlers) => registrations.push({ doctype, handlers }) } },
			utils: { escape_html: escapeHtml, debounce },
			get_meta: (doctype) => metaByDoctype[doctype] || null,
		},
		erpnext: {
			adhd: {
				onStateChange(fn) {
					stateHandlers.push(fn);
					fn(Boolean(sandbox.frappe.boot.adhd_mode));
				},
			},
		},
	};
	const setMode = (active) => {
		sandbox.frappe.boot.adhd_mode = active;
		stateHandlers.forEach((fn) => fn(active));
	};
	return { sandbox, registrations, calls, window, setMode };
}

function loadModule(name, sandbox) {
	const context = vm.createContext(sandbox);
	vm.runInContext(read(name), context, { filename: name });
	return context;
}

// =================================================================================================
// ADHD-058: Leave Application "Balance at a Glance"
// =================================================================================================

function makeLeaveFrm(dom, overrides = {}) {
	return {
		doctype: "Leave Application",
		docname: "LA-0001",
		doc: {
			name: "LA-0001",
			employee: "HR-EMP-00001",
			leave_type: "Casual Leave",
			from_date: "2026-10-05",
			to_date: "2026-10-07",
			half_day: 0,
			half_day_date: "",
			...overrides,
		},
		is_new: () => false,
		layout: { wrapper: dom.layoutWrapper() },
		fields_dict: { leave_type: { $wrapper: dom.fieldWrapper("leave_type") } },
	};
}

test("leave: no card while employee, leave_type or from_date is blank", async () => {
	const dom = makeDom();
	const { sandbox, registrations, calls } = makeSandbox({ dom });
	loadModule("adhd_leave_application.js", sandbox);
	const refresh = handlerFor(registrations, "Leave Application", "refresh");

	for (const blank of ["employee", "leave_type", "from_date"]) {
		const frm = makeLeaveFrm(dom, { [blank]: "" });
		refresh(frm);
		await wait(20);
		assert.equal(dom.panels.length, 0, `no card with ${blank} blank`);
	}
	assert.equal(calls.length, 0, "a blank required field must never reach the server");
});

test("leave: renders entitlement/used/remaining/days and colours green when balance is fine", async () => {
	const dom = makeDom();
	const { sandbox, registrations, calls } = makeSandbox({
		dom,
		respond: () => ({
			available: true,
			state: "ok",
			total_leaves: 12,
			used: 2,
			remaining: 10,
			days: 3,
			leave_type: "Casual Leave",
		}),
	});
	loadModule("adhd_leave_application.js", sandbox);
	const refresh = handlerFor(registrations, "Leave Application", "refresh");
	const frm = makeLeaveFrm(dom);

	refresh(frm);
	await wait(450); // debounce is 400ms
	assert.equal(calls.length, 1);
	assert.equal(calls[0].method, "hrms.hr.services.adhd_leave_balance.get_leave_glance");
	const html = dom.panelHtml();
	assert.match(html, /id="adhd-leave-balance"/);
	assert.match(html, /adhd-leave-green/);
	assert.match(html, /Entitlement.*12/s);
	assert.match(html, /Used.*2/s);
	assert.match(html, /Remaining.*10/s);
	assert.match(html, /This application.*3/s);
	assert.doesNotMatch(html, /Insufficient balance/);
});

test("leave: amber when the application would leave under 2 days, red with the warning when short", async () => {
	const dom = makeDom();
	let state = "low";
	const { sandbox, registrations } = makeSandbox({
		dom,
		respond: () => ({
			available: true,
			state,
			total_leaves: 10,
			used: 8,
			remaining: 2,
			days: state === "short" ? 5 : 1,
			leave_type: "Casual Leave",
		}),
	});
	loadModule("adhd_leave_application.js", sandbox);
	const refresh = handlerFor(registrations, "Leave Application", "refresh");

	refresh(makeLeaveFrm(dom));
	await wait(450);
	assert.match(dom.panelHtml(), /adhd-leave-amber/);

	state = "short";
	refresh(makeLeaveFrm(dom, { to_date: "2026-10-10" }));
	await wait(450);
	const html = dom.panelHtml();
	assert.match(html, /adhd-leave-red/);
	assert.match(html, /adhd-leave-warn">⚠ Insufficient balance/);
});

test("leave: negative-allowed leave type warns without blocking, never shown as a hard insufficiency", async () => {
	const dom = makeDom();
	const { sandbox, registrations } = makeSandbox({
		dom,
		respond: () => ({
			available: true,
			state: "negative_allowed",
			total_leaves: 5,
			used: 5,
			remaining: 0,
			days: 2,
			leave_type: "Unlimited Leave",
		}),
	});
	loadModule("adhd_leave_application.js", sandbox);
	const refresh = handlerFor(registrations, "Leave Application", "refresh");

	refresh(makeLeaveFrm(dom, { leave_type: "Unlimited Leave" }));
	await wait(450);
	const html = dom.panelHtml();
	assert.match(html, /adhd-leave-amber/);
	assert.match(html, /allows a negative balance/);
	assert.doesNotMatch(html, /Insufficient balance/);
});

test("leave: a submitted/cancelled application shows a read-only recorded state, no warning", async () => {
	const dom = makeDom();
	const { sandbox, registrations } = makeSandbox({
		dom,
		respond: () => ({
			available: true,
			state: "recorded",
			total_leaves: 12,
			used: 12,
			days: 3,
			counted: true,
			leave_type: "Casual Leave",
		}),
	});
	loadModule("adhd_leave_application.js", sandbox);
	const refresh = handlerFor(registrations, "Leave Application", "refresh");

	refresh(makeLeaveFrm(dom));
	await wait(450);
	const html = dom.panelHtml();
	assert.match(html, /adhd-leave-green/);
	assert.match(html, /Already counted/);
	assert.doesNotMatch(html, /Insufficient balance/);
});

test("leave: an employee the caller cannot read never gets numbers or a warning", async () => {
	const dom = makeDom();
	const { sandbox, registrations, calls } = makeSandbox({
		dom,
		respond: () => ({ available: false }),
	});
	loadModule("adhd_leave_application.js", sandbox);
	const refresh = handlerFor(registrations, "Leave Application", "refresh");

	refresh(makeLeaveFrm(dom));
	await wait(450);
	assert.equal(calls.length, 1);
	const html = dom.panelHtml();
	assert.match(html, /adhd-leave-amber/);
	assert.match(html, /not available/);
	assert.doesNotMatch(html, /\d/); // no invented numbers
});

test("leave: leave_type is escaped before it reaches the DOM", async () => {
	const dom = makeDom();
	const { sandbox, registrations } = makeSandbox({
		dom,
		respond: () => ({
			available: true,
			state: "ok",
			total_leaves: 1,
			used: 0,
			remaining: 1,
			days: 1,
		}),
	});
	loadModule("adhd_leave_application.js", sandbox);
	const refresh = handlerFor(registrations, "Leave Application", "refresh");

	refresh(makeLeaveFrm(dom, { leave_type: "<img src=x onerror=alert(1)>" }));
	await wait(450);
	const html = dom.panelHtml();
	assert.doesNotMatch(html, /<img/);
	assert.match(html, /&lt;img/);
});

test("leave: rapid field edits collapse into exactly one server call (debounced)", async () => {
	const dom = makeDom();
	const { sandbox, registrations, calls } = makeSandbox({
		dom,
		respond: () => ({
			available: true,
			state: "ok",
			total_leaves: 10,
			used: 0,
			remaining: 10,
			days: 1,
		}),
	});
	loadModule("adhd_leave_application.js", sandbox);
	const refresh = handlerFor(registrations, "Leave Application", "refresh");
	const leaveType = handlerFor(registrations, "Leave Application", "leave_type");
	const frm = makeLeaveFrm(dom);

	refresh(frm);
	for (const lt of ["Sick Leave", "Privilege Leave", "Casual Leave"]) {
		frm.doc.leave_type = lt;
		leaveType(frm);
	}
	await wait(450);
	assert.equal(calls.length, 1, "five triggers in quick succession must reach the server once");
});

test("leave: switching Focus Mode off cancels a pending fetch and clears the card", async () => {
	const dom = makeDom();
	const { sandbox, registrations, calls, window, setMode } = makeSandbox({
		dom,
		respond: () => ({
			available: true,
			state: "ok",
			total_leaves: 10,
			used: 0,
			remaining: 10,
			days: 1,
		}),
	});
	loadModule("adhd_leave_application.js", sandbox);
	const refresh = handlerFor(registrations, "Leave Application", "refresh");
	const frm = makeLeaveFrm(dom);
	window.cur_frm = frm;

	refresh(frm);
	setMode(false); // before the 400ms debounce fires
	await wait(450);
	assert.equal(calls.length, 0, "turning the mode off must cancel the scheduled call");
	assert.equal(dom.panels.length, 0);
});

// =================================================================================================
// ADHD-059: Expense Claim "Receipt Attached?"
// =================================================================================================

const EXPENSE_DETAIL_NO_ATTACH_FIELD = {
	fields: [
		{ fieldname: "expense_date", fieldtype: "Date" },
		{ fieldname: "expense_type", fieldtype: "Link" },
		{ fieldname: "description", fieldtype: "Text Editor" },
		{ fieldname: "amount", fieldtype: "Currency" },
		{ fieldname: "sanctioned_amount", fieldtype: "Currency" },
	],
};
const EXPENSE_DETAIL_WITH_ATTACH_FIELD = {
	fields: [
		...EXPENSE_DETAIL_NO_ATTACH_FIELD.fields,
		{ fieldname: "receipt", fieldtype: "Attach" },
	],
};

function makeExpenseFrm(dom, rows, { isNew = false, metaField = false } = {}) {
	const grid_rows_by_docname = {};
	rows.forEach((row) => {
		grid_rows_by_docname[row.name] = { row: dom.gridRowHandle("expenses", row.name) };
	});
	return {
		doctype: "Expense Claim",
		docname: isNew ? "new-expense-claim-1" : "HR-EXP-0001",
		doc: { name: isNew ? "new-expense-claim-1" : "HR-EXP-0001", expenses: rows },
		is_new: () => isNew,
		layout: { wrapper: dom.layoutWrapper() },
		fields_dict: {
			expenses: {
				df: { options: "Expense Claim Detail" },
				$wrapper: dom.fieldWrapper("expenses"),
				grid: { grid_rows_by_docname, wrapper: {} },
			},
		},
	};
}

test("expense: no badges, no summary, and no server call for an empty expenses table", async () => {
	const dom = makeDom();
	const { sandbox, registrations, calls } = makeSandbox({
		dom,
		metaByDoctype: { "Expense Claim Detail": EXPENSE_DETAIL_NO_ATTACH_FIELD },
	});
	loadModule("adhd_expense_claim.js", sandbox);
	const refresh = handlerFor(registrations, "Expense Claim", "refresh");

	refresh(makeExpenseFrm(dom, []));
	await wait(10);
	assert.equal(dom.panels.length, 0);
	assert.equal(calls.length, 0);
});

test("expense: with a real per-row Attach field, badges and the summary are exact and no File fetch happens", async () => {
	const dom = makeDom();
	const { sandbox, registrations, calls } = makeSandbox({
		dom,
		metaByDoctype: { "Expense Claim Detail": EXPENSE_DETAIL_WITH_ATTACH_FIELD },
	});
	loadModule("adhd_expense_claim.js", sandbox);
	const refresh = handlerFor(registrations, "Expense Claim", "refresh");

	const rows = [
		{ name: "row-1", receipt: "/files/a.png" },
		{ name: "row-2", receipt: "" },
		{ name: "row-3", receipt: "" },
	];
	refresh(makeExpenseFrm(dom, rows));
	await wait(10);
	assert.equal(calls.length, 0, "the real field is already on the row; no fetch is needed");
	assert.equal(dom.badgesOn("expenses", "row-1"), 0);
	assert.equal(dom.badgesOn("expenses", "row-2"), 1);
	assert.equal(dom.badgesOn("expenses", "row-3"), 1);
	const html = dom.panelHtml();
	assert.match(html, /1 of 3 expenses have a receipt attached/);
	assert.match(html, /adhd-receipt-amber/);
});

test("expense: field-mode, all rows covered is green with no badges", async () => {
	const dom = makeDom();
	const { sandbox, registrations } = makeSandbox({
		dom,
		metaByDoctype: { "Expense Claim Detail": EXPENSE_DETAIL_WITH_ATTACH_FIELD },
	});
	loadModule("adhd_expense_claim.js", sandbox);
	const refresh = handlerFor(registrations, "Expense Claim", "refresh");

	const rows = [
		{ name: "row-1", receipt: "/files/a.png" },
		{ name: "row-2", receipt: "/files/b.png" },
	];
	refresh(makeExpenseFrm(dom, rows));
	await wait(10);
	assert.equal(dom.totalBadges(), 0);
	assert.match(dom.panelHtml(), /adhd-receipt-green/);
	assert.match(dom.panelHtml(), /2 of 2 expenses have a receipt attached/);
});

test("expense: without a per-row field, zero attachments on the claim is the only case every row is badged", async () => {
	const dom = makeDom();
	const { sandbox, registrations, calls } = makeSandbox({
		dom,
		metaByDoctype: { "Expense Claim Detail": EXPENSE_DETAIL_NO_ATTACH_FIELD },
		respond: (options) => {
			assert.equal(options.method, "frappe.client.get_list");
			assert.equal(options.args.doctype, "File");
			assert.equal(options.args.filters.attached_to_doctype, "Expense Claim");
			return [];
		},
	});
	loadModule("adhd_expense_claim.js", sandbox);
	const refresh = handlerFor(registrations, "Expense Claim", "refresh");

	const rows = [{ name: "row-1" }, { name: "row-2" }, { name: "row-3" }];
	refresh(makeExpenseFrm(dom, rows));
	await wait(10);
	assert.equal(calls.length, 1);
	assert.equal(dom.badgesOn("expenses", "row-1"), 1);
	assert.equal(dom.badgesOn("expenses", "row-2"), 1);
	assert.equal(dom.badgesOn("expenses", "row-3"), 1);
	assert.match(dom.panelHtml(), /0 of 3 expenses have a receipt attached/);
	assert.match(dom.panelHtml(), /adhd-receipt-red/);
});

test("expense: without a per-row field, one or more attachments never badges any row as missing (unknowable)", async () => {
	const dom = makeDom();
	const { sandbox, registrations } = makeSandbox({
		dom,
		metaByDoctype: { "Expense Claim Detail": EXPENSE_DETAIL_NO_ATTACH_FIELD },
		respond: () => [{ name: "File-1" }],
	});
	loadModule("adhd_expense_claim.js", sandbox);
	const refresh = handlerFor(registrations, "Expense Claim", "refresh");

	const rows = [{ name: "row-1" }, { name: "row-2" }, { name: "row-3" }];
	refresh(makeExpenseFrm(dom, rows));
	await wait(10);
	assert.equal(
		dom.totalBadges(),
		0,
		"never claim a specific row is missing a receipt when it is not knowable",
	);
	const html = dom.panelHtml();
	assert.match(html, /1 attachment\(s\) on this claim for 3 expense\(s\)/);
	assert.match(html, /has one can&#39;t be shown/);
	assert.doesNotMatch(html, /\d of 3 expenses have a receipt/); // never a false per-row claim
});

test("expense: a failed File lookup never claims a receipt is missing", async () => {
	const dom = makeDom();
	const { sandbox, registrations } = makeSandbox({
		dom,
		metaByDoctype: { "Expense Claim Detail": EXPENSE_DETAIL_NO_ATTACH_FIELD },
		respond: () => "REJECT",
	});
	loadModule("adhd_expense_claim.js", sandbox);
	const refresh = handlerFor(registrations, "Expense Claim", "refresh");

	refresh(makeExpenseFrm(dom, [{ name: "row-1" }, { name: "row-2" }]));
	await wait(10);
	assert.equal(dom.totalBadges(), 0);
	assert.match(dom.panelHtml(), /not available/);
});

test("expense: an unsaved document never calls the server (no name to attach a File to)", async () => {
	const dom = makeDom();
	const { sandbox, registrations, calls } = makeSandbox({
		dom,
		metaByDoctype: { "Expense Claim Detail": EXPENSE_DETAIL_NO_ATTACH_FIELD },
	});
	loadModule("adhd_expense_claim.js", sandbox);
	const refresh = handlerFor(registrations, "Expense Claim", "refresh");

	refresh(makeExpenseFrm(dom, [{ name: "row-1" }], { isNew: true }));
	await wait(10);
	assert.equal(calls.length, 0);
	assert.equal(dom.badgesOn("expenses", "row-1"), 1); // certainly no receipt yet
});

test("expense: adding/removing a row re-renders from the cached count, without another File fetch", async () => {
	const dom = makeDom();
	const { sandbox, registrations, calls } = makeSandbox({
		dom,
		metaByDoctype: { "Expense Claim Detail": EXPENSE_DETAIL_NO_ATTACH_FIELD },
		respond: () => [{ name: "File-1" }],
	});
	loadModule("adhd_expense_claim.js", sandbox);
	const refresh = handlerFor(registrations, "Expense Claim", "refresh");
	const added = handlerFor(registrations, "Expense Claim Detail", "expenses_add");

	const frm = makeExpenseFrm(dom, [{ name: "row-1" }]);
	refresh(frm);
	await wait(10);
	assert.equal(calls.length, 1);

	frm.doc.expenses.push({ name: "row-2" });
	frm.fields_dict.expenses.grid.grid_rows_by_docname["row-2"] = {
		row: dom.gridRowHandle("expenses", "row-2"),
	};
	added(frm);
	await wait(10);
	assert.equal(calls.length, 1, "the parent document's own attachments did not change");
	assert.match(dom.panelHtml(), /1 attachment\(s\) on this claim for 2 expense\(s\)/);
});

test("expense: switching Focus Mode off clears every badge and the summary", async () => {
	const dom = makeDom();
	const { sandbox, registrations, window, setMode } = makeSandbox({
		dom,
		metaByDoctype: { "Expense Claim Detail": EXPENSE_DETAIL_NO_ATTACH_FIELD },
		respond: () => [],
	});
	loadModule("adhd_expense_claim.js", sandbox);
	const refresh = handlerFor(registrations, "Expense Claim", "refresh");
	const frm = makeExpenseFrm(dom, [{ name: "row-1" }, { name: "row-2" }]);
	window.cur_frm = frm;

	refresh(frm);
	await wait(10);
	assert.equal(dom.totalBadges(), 2);

	setMode(false);
	assert.equal(dom.totalBadges(), 0);
	assert.equal(dom.panels.length, 0);
});

test("expense: no badges/summary at all when Focus Mode is off from the start", async () => {
	const dom = makeDom();
	const { sandbox, registrations, calls } = makeSandbox({
		dom,
		adhdMode: false,
		metaByDoctype: { "Expense Claim Detail": EXPENSE_DETAIL_NO_ATTACH_FIELD },
	});
	loadModule("adhd_expense_claim.js", sandbox);
	const refresh = handlerFor(registrations, "Expense Claim", "refresh");

	refresh(makeExpenseFrm(dom, [{ name: "row-1" }]));
	await wait(10);
	assert.equal(dom.totalBadges(), 0);
	assert.equal(dom.panels.length, 0);
	assert.equal(calls.length, 0);
});

// =================================================================================================
// The "Leave Balance and Receipt Checks" switch in Focus Settings (registered by adhd_focus_registrations.js)
// =================================================================================================

// Focus Settings as adhd_settings.js exposes them: get() by key, and jQuery's document events when a switch
// flips. `known` says whether the switch was registered at all (an older RTB has no registerFeature).
function withSettings(sandbox, { on = true, known = true } = {}) {
	const values = { hr_form_aids: on };
	const listeners = [];
	sandbox.document = {};
	const dollar = sandbox.$;
	sandbox.$ = (target) =>
		target === sandbox.document
			? {
					on(events, handler) {
						events.split(/\s+/).forEach((event) => listeners.push({ event, handler }));
						return this;
					},
			  }
			: dollar(target);
	sandbox.erpnext.adhd.ADHD_FEATURES = known
		? [{ key: "hr_form_aids", label: "Leave Balance and Receipt Checks" }]
		: [];
	sandbox.erpnext.adhd.ADHDSettings = {
		get: (key) => Boolean(values[key]),
		set(key, value) {
			values[key] = Boolean(value);
			listeners
				.filter((item) => item.event === "adhd_setting_changed")
				.forEach((item) => item.handler({}, { key, value: Boolean(value) }));
		},
		reset() {
			values.hr_form_aids = true;
			listeners
				.filter((item) => item.event === "adhd_settings_reset")
				.forEach((item) => item.handler({}));
		},
	};
	return sandbox.erpnext.adhd.ADHDSettings;
}

const OK_BALANCE = () => ({
	available: true,
	state: "ok",
	total_leaves: 12,
	used: 2,
	remaining: 10,
	days: 3,
});

test("leave: the switch off in Focus Settings means no card and no server call, even with the mode on", async () => {
	const dom = makeDom();
	const { sandbox, registrations, calls } = makeSandbox({ dom, respond: OK_BALANCE });
	withSettings(sandbox, { on: false });
	loadModule("adhd_leave_application.js", sandbox);
	handlerFor(registrations, "Leave Application", "refresh")(makeLeaveFrm(dom));
	await wait(450);
	assert.equal(dom.panels.length, 0);
	assert.equal(calls.length, 0);
});

test("leave: flipping the switch while the form is open removes and restores the card at once", async () => {
	const dom = makeDom();
	const { sandbox, registrations, calls, window } = makeSandbox({ dom, respond: OK_BALANCE });
	const settings = withSettings(sandbox, { on: true });
	loadModule("adhd_leave_application.js", sandbox);
	const frm = makeLeaveFrm(dom);
	window.cur_frm = frm;
	handlerFor(registrations, "Leave Application", "refresh")(frm);
	await wait(450);
	assert.equal(dom.panels.length, 1);

	settings.set("hr_form_aids", false);
	await wait(450);
	assert.equal(dom.panels.length, 0, "off: the card goes");
	const before = calls.length;
	settings.set("pomodoro", false); // another switch changes nothing here
	await wait(450);
	assert.equal(calls.length, before);

	settings.set("hr_form_aids", true);
	await wait(450);
	assert.equal(dom.panels.length, 1, "on again: the card is back");
	settings.set("hr_form_aids", false);
	await wait(450);
	settings.reset(); // a reset turns every switch back to its default, and this one is on by default
	await wait(450);
	assert.equal(dom.panels.length, 1);
});

test("leave: an RTB whose settings do not know the switch keeps the card on", async () => {
	const dom = makeDom();
	const { sandbox, registrations } = makeSandbox({ dom, respond: OK_BALANCE });
	withSettings(sandbox, { on: false, known: false });
	loadModule("adhd_leave_application.js", sandbox);
	handlerFor(registrations, "Leave Application", "refresh")(makeLeaveFrm(dom));
	await wait(450);
	assert.equal(dom.panels.length, 1);
});

test("expense: the switch off means no summary, no badges and no File fetch; on again restores them", async () => {
	const dom = makeDom();
	const { sandbox, registrations, calls, window } = makeSandbox({
		dom,
		metaByDoctype: { "Expense Claim Detail": EXPENSE_DETAIL_NO_ATTACH_FIELD },
		respond: () => [],
	});
	const settings = withSettings(sandbox, { on: false });
	loadModule("adhd_expense_claim.js", sandbox);
	const frm = makeExpenseFrm(dom, [{ name: "row-1" }, { name: "row-2" }]);
	window.cur_frm = frm;
	handlerFor(registrations, "Expense Claim", "refresh")(frm);
	await wait(450);
	assert.equal(dom.panels.length, 0);
	assert.equal(dom.totalBadges(), 0);
	assert.equal(calls.length, 0);

	settings.set("hr_form_aids", true);
	await wait(450);
	assert.equal(dom.panels.length, 1, "on: the summary appears");
	assert.equal(calls.length, 1, "on: the attachments are fetched once");
	settings.set("hr_form_aids", false);
	await wait(450);
	assert.equal(dom.panels.length, 0);
	assert.equal(dom.totalBadges(), 0);
});
