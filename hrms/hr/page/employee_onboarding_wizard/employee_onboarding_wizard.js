// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt
//
// Focus "Onboard New Employee" wizard (ADHD-014). One thing per screen, a visible "step X of Y", answers kept
// locally until the person confirms the review step, and one atomic server call that creates every document
// or none of them (see hrms.hr.services.adhd_employee_onboarding on the server).
//
// erpnext.adhd is the shared namespace the ERPNext app's Focus-mode layer already publishes on every desk
// page (see erpnext/public/js/adhd/adhd_mode.js); this file only adds to it, it never assumes ERPNext-only
// pages exist. Reading frappe.boot.adhd_mode only changes decorative styling here -- the wizard works the
// same whether Focus mode is on or off (ADHD-014 acceptance test 12), so nothing about it is read once at
// load and cached.

frappe.provide("erpnext.adhd");

erpnext.adhd.onboardingWizard = erpnext.adhd.onboardingWizard || {};

(() => {
	const PAGE_ROUTE = "employee-onboarding-wizard";
	const METHOD_PREFIX = "hrms.hr.page.employee_onboarding_wizard.employee_onboarding_wizard";
	const METHOD_CONTEXT = `${METHOD_PREFIX}.get_context`;
	const METHOD_CHECK = `${METHOD_PREFIX}.check_step`;
	const METHOD_CREATE = `${METHOD_PREFIX}.create_employee_onboarding`;
	const DRAFT_STORAGE_KEY = "hrms_adhd_onboarding_wizard_draft";
	const MAX_EDUCATION_ROWS_FALLBACK = 10;

	// One thing per screen. "review" only reads the other steps' answers back; it creates nothing by itself.
	const WIZARD_STEPS = [
		{ id: "who", label: __("Who's Joining"), doctype: "Employee", skippable: false },
		{ id: "job", label: __("Job Details"), doctype: "Employee", skippable: false },
		{
			id: "education",
			label: __("Education"),
			doctype: "Employee Education",
			skippable: true,
		},
		{ id: "address", label: __("Address"), doctype: "Address", skippable: true },
		{
			id: "salary",
			label: __("Salary"),
			doctype: "Salary Structure Assignment",
			skippable: true,
		},
		{ id: "leave", label: __("Leave"), doctype: "Leave Policy Assignment", skippable: true },
		{ id: "review", label: __("Review & Confirm"), doctype: null, skippable: false },
	];

	const escapeHtml = (text) => frappe.utils.escape_html(text == null ? "" : text);

	function stepIndex(id) {
		return WIZARD_STEPS.findIndex((step) => step.id === id);
	}

	function stepAt(index) {
		const clamped = Math.max(0, Math.min(WIZARD_STEPS.length - 1, index || 0));
		return WIZARD_STEPS[clamped];
	}

	function progressText(index) {
		const step = stepAt(index);
		return __("Step {0} of {1} — {2}", [index + 1, WIZARD_STEPS.length, step.label]);
	}

	function currentUser() {
		if (erpnext.adhd.currentUser) return erpnext.adhd.currentUser();
		return (frappe.session && frappe.session.user) || "Guest";
	}

	function draftKey(user) {
		return `${DRAFT_STORAGE_KEY}:${user}`;
	}

	function emptyDraft() {
		return {
			index: 0,
			skipped: [],
			submit_now: true,
			who: {},
			job: {},
			education: [],
			address: {},
			salary: {},
			leave: {},
		};
	}

	// localStorage can throw (private windows, quota, disabled site data): a draft that can't be read or
	// written just means the wizard starts fresh, never a broken page.
	function loadDraft(user) {
		try {
			const raw = localStorage.getItem(draftKey(user));
			if (!raw) return emptyDraft();
			const parsed = JSON.parse(raw);
			return Object.assign(emptyDraft(), parsed && typeof parsed === "object" ? parsed : {});
		} catch (e) {
			return emptyDraft();
		}
	}

	function saveDraft(user, draft) {
		try {
			localStorage.setItem(draftKey(user), JSON.stringify(draft));
		} catch (e) {
			/* not remembered across a reload, but the current page keeps working */
		}
	}

	function clearDraft(user) {
		try {
			localStorage.removeItem(draftKey(user));
		} catch (e) {
			/* nothing to do: the key may simply not be readable here */
		}
	}

	// What the server is asked to check or create: the skipped steps are sent empty so a value left in a
	// field the person chose to skip is never used.
	function buildPayload(draft) {
		const skipped = draft.skipped || [];
		const pick = (id, fallback) => (skipped.includes(id) ? fallback : draft[id]);
		return {
			who: draft.who || {},
			job: draft.job || {},
			education: pick("education", []),
			address: pick("address", {}),
			salary: pick("salary", {}),
			leave: pick("leave", {}),
			skipped,
			options: { submit: Boolean(draft.submit_now) },
		};
	}

	function fieldOptions(list) {
		return ["", ...(list || [])].join("\n");
	}

	// ---- Per-step field definitions (frappe.ui.FieldGroup docfield-like objects) --------------------------

	function buildWhoFields(context) {
		const fields = [
			{ fieldname: "first_name", fieldtype: "Data", label: __("First Name"), reqd: 1 },
			{ fieldname: "middle_name", fieldtype: "Data", label: __("Middle Name") },
			{ fieldname: "last_name", fieldtype: "Data", label: __("Last Name") },
			{
				fieldname: "gender",
				fieldtype: "Link",
				options: "Gender",
				label: __("Gender"),
				reqd: 1,
			},
			{
				fieldname: "date_of_birth",
				fieldtype: "Date",
				label: __("Date of Birth"),
				reqd: 1,
			},
		];
		if (context && context.naming === "Employee Number") {
			fields.push({
				fieldname: "employee_number",
				fieldtype: "Data",
				label: __("Employee Number"),
				reqd: 1,
				description: __("This becomes the employee's ID, so it must be unique."),
			});
		}
		return fields;
	}

	function buildJobFields(context, draft) {
		const fields = [
			{
				fieldname: "company",
				fieldtype: "Link",
				options: "Company",
				label: __("Company"),
				reqd: 1,
				default: (context && context.default_company) || undefined,
			},
			{
				fieldname: "date_of_joining",
				fieldtype: "Date",
				label: __("Date of Joining"),
				reqd: 1,
				default: (context && context.today) || undefined,
			},
			{
				fieldname: "department",
				fieldtype: "Link",
				options: "Department",
				label: __("Department"),
				get_query: () => ({ filters: { company: draft.job && draft.job.company } }),
			},
			{
				fieldname: "designation",
				fieldtype: "Link",
				options: "Designation",
				label: __("Designation"),
			},
		];
		if (context && context.has_employment_type) {
			fields.push({
				fieldname: "employment_type",
				fieldtype: "Link",
				options: "Employment Type",
				label: __("Employment Type"),
			});
		}
		return fields;
	}

	function buildAddressFields(context) {
		return [
			{
				fieldname: "address_type",
				fieldtype: "Select",
				options: fieldOptions(context && context.address_types),
				label: __("Address Type"),
				reqd: 1,
			},
			{
				fieldname: "address_line1",
				fieldtype: "Data",
				label: __("Address Line 1"),
				reqd: 1,
			},
			{ fieldname: "address_line2", fieldtype: "Data", label: __("Address Line 2") },
			{ fieldname: "city", fieldtype: "Data", label: __("City"), reqd: 1 },
			{ fieldname: "state", fieldtype: "Data", label: __("State / Province") },
			{ fieldname: "pincode", fieldtype: "Data", label: __("Postal Code") },
			{
				fieldname: "country",
				fieldtype: "Link",
				options: "Country",
				label: __("Country"),
				reqd: 1,
			},
			{
				fieldname: "address_title",
				fieldtype: "Data",
				label: __("Address Title"),
				description: __("Leave blank to use the employee's name."),
			},
		];
	}

	function buildSalaryFields(draft) {
		const company = draft.job && draft.job.company;
		return [
			{
				fieldname: "salary_structure",
				fieldtype: "Link",
				options: "Salary Structure",
				label: __("Salary Structure"),
				reqd: 1,
				get_query: () => ({ filters: { company, docstatus: 1, is_active: "Yes" } }),
				description: __("Only submitted, active structures for {0} are listed.", [
					company || __("the selected company"),
				]),
			},
			{
				fieldname: "from_date",
				fieldtype: "Date",
				label: __("Start Date"),
				reqd: 1,
				default: (draft.job && draft.job.date_of_joining) || undefined,
			},
			{
				fieldname: "base",
				fieldtype: "Currency",
				label: __("Base Pay"),
				description: __("Optional: leave blank to set it later on the record."),
			},
			{
				fieldname: "income_tax_slab",
				fieldtype: "Link",
				options: "Income Tax Slab",
				label: __("Income Tax Slab"),
				get_query: () => ({ filters: { docstatus: 1, disabled: 0 } }),
				description: __("Only required if the salary structure deducts income tax."),
			},
		];
	}

	function buildLeaveFields(draft) {
		const company = draft.job && draft.job.company;
		return [
			{
				fieldname: "leave_policy",
				fieldtype: "Link",
				options: "Leave Policy",
				label: __("Leave Policy"),
				reqd: 1,
				get_query: () => ({ filters: { docstatus: 1 } }),
			},
			{
				fieldname: "assignment_based_on",
				fieldtype: "Select",
				options: fieldOptions(["Leave Period", "Joining Date"]),
				label: __("Leave Period Is Based On"),
				reqd: 1,
			},
			{
				fieldname: "leave_period",
				fieldtype: "Link",
				options: "Leave Period",
				label: __("Leave Period"),
				depends_on: 'eval:doc.assignment_based_on=="Leave Period"',
				mandatory_depends_on: 'eval:doc.assignment_based_on=="Leave Period"',
				get_query: () => ({ filters: { company, is_active: 1 } }),
			},
		];
	}

	function buildFieldsForStep(stepId, context, draft) {
		switch (stepId) {
			case "who":
				return buildWhoFields(context);
			case "job":
				return buildJobFields(context, draft);
			case "address":
				return buildAddressFields(context);
			case "salary":
				return buildSalaryFields(draft);
			case "leave":
				return buildLeaveFields(draft);
			default:
				return null;
		}
	}

	// ---- The wizard --------------------------------------------------------------------------------------

	class EmployeeOnboardingWizard {
		constructor(page) {
			this.page = page;
			this.user = currentUser();
			this.draft = loadDraft(this.user);
			this.draft.index = Math.max(
				0,
				Math.min(WIZARD_STEPS.length - 1, this.draft.index || 0),
			);
			this.context = null;
			this.form = null;
			this.completed = null;
			this.busy = false;

			this.$wrapper = $('<div class="adhd-onboarding-wizard"></div>').appendTo(page.body);
			this.$header = $('<div class="adhd-onb-header"></div>').appendTo(this.$wrapper);
			this.$errors = $(
				'<div class="adhd-onb-errors" role="alert" aria-live="assertive"></div>',
			).appendTo(this.$wrapper);
			this.$body = $('<div class="adhd-onb-body"></div>').appendTo(this.$wrapper);
			this.$footer = $('<div class="adhd-onb-footer"></div>').appendTo(this.$wrapper);

			this._onModeChange = this._onModeChange.bind(this);
			if (erpnext.adhd.onStateChange) erpnext.adhd.onStateChange(this._onModeChange);
			else this._onModeChange(Boolean(frappe.boot && frappe.boot.adhd_mode));

			erpnext.adhd.onboardingWizard.current = this;
		}

		// Display only: Focus mode never gates whether the wizard works (ADHD-014 test 12).
		_onModeChange(active) {
			this.$wrapper.toggleClass("adhd-mode-on", Boolean(active));
		}

		async start() {
			this.context = await frappe.xcall(METHOD_CONTEXT);
			this.render();
		}

		// ---- persistence ----

		_persist() {
			saveDraft(this.user, this.draft);
		}

		_captureCurrentStepValues() {
			const step = stepAt(this.draft.index);
			if (step.id === "education") {
				this.draft.education = this._readEducationRows();
			} else if (this.form) {
				this.draft[step.id] = Object.assign(
					{},
					this.draft[step.id],
					this.form.get_values(true) || {},
				);
			}
			this._persist();
		}

		// ---- rendering ----

		render() {
			if (this.completed) return this.renderSuccess();
			this.$errors.empty().hide();
			this.renderHeader();
			this.renderStep();
			this.renderFooter();
		}

		renderHeader() {
			const index = this.draft.index;
			this.$header.empty();
			$(
				`<progress class="adhd-onb-progress" max="${WIZARD_STEPS.length}" value="${
					index + 1
				}" aria-label="${escapeHtml(progressText(index))}"></progress>`,
			).appendTo(this.$header);
			$(
				`<div class="adhd-onb-step-label" role="status" aria-live="polite">${escapeHtml(
					progressText(index),
				)}</div>`,
			).appendTo(this.$header);
		}

		renderStep() {
			const step = stepAt(this.draft.index);
			this.$body.empty();
			this.form = null;

			if (step.id === "education") {
				this._renderEducationStep();
			} else if (step.id === "review") {
				this._renderReviewStep();
			} else {
				const $formTarget = $('<div class="adhd-onb-form"></div>').appendTo(this.$body);
				const fields = buildFieldsForStep(step.id, this.context, this.draft);
				this.form = new frappe.ui.FieldGroup({
					fields,
					body: $formTarget[0],
					no_submit_on_enter: true,
				});
				this.form.make();
				this.form.set_values(this.draft[step.id] || {});
				this.form.wrapper &&
					this.form.wrapper
						.find("input, select, textarea")
						.on("change", () => this._captureCurrentStepValues());
				this.form.focus_on_first_input && this.form.focus_on_first_input();
			}

			if (step.skippable) {
				$(
					`<button type="button" class="btn btn-link btn-xs adhd-onb-skip">${escapeHtml(
						__("Skip — remind me in {0} days", [
							(this.context && this.context.reminder_days) || 7,
						]),
					)}</button>`,
				)
					.appendTo(this.$body)
					.on("click", () => this.skipStep());
			}
		}

		// -- education: a short repeatable list, not a full grid, so it stays keyboard-simple --

		_renderEducationStep() {
			const $intro = $(
				`<p class="adhd-onb-hint">${escapeHtml(
					__(
						"Add one row per qualification. You can add more on the employee record later.",
					),
				)}</p>`,
			).appendTo(this.$body);
			this.$education_rows = $('<div class="adhd-onb-education-rows"></div>').appendTo(
				this.$body,
			);
			$intro.attr("id", "adhd-onb-education-hint");

			const rows =
				this.draft.education && this.draft.education.length ? this.draft.education : [{}];
			rows.forEach((row) => this._addEducationRow(row));

			const cap =
				(this.context && this.context.max_education_rows) || MAX_EDUCATION_ROWS_FALLBACK;
			this.$addEducationBtn = $(
				`<button type="button" class="btn btn-default btn-xs adhd-onb-education-add">${escapeHtml(
					__("+ Add another qualification"),
				)}</button>`,
			)
				.appendTo(this.$body)
				.on("click", () => {
					if (this.$education_rows.children().length >= cap) return;
					this._addEducationRow({});
					this._captureCurrentStepValues();
				});
		}

		_addEducationRow(values) {
			const levels = fieldOptions((this.context && this.context.education_levels) || []);
			const levelOptions = levels
				.split("\n")
				.map((option) => {
					const selected = option && option === values.level ? " selected" : "";
					return `<option value="${escapeHtml(option)}"${selected}>${escapeHtml(
						option || __("Level"),
					)}</option>`;
				})
				.join("");
			const $row = $(`
				<div class="adhd-onb-education-row">
					<input type="text" class="form-control" data-field="qualification" placeholder="${escapeHtml(
						__("Qualification"),
					)}" value="${escapeHtml(values.qualification || "")}" aria-label="${escapeHtml(
						__("Qualification"),
					)}">
					<input type="text" class="form-control" data-field="school_univ" placeholder="${escapeHtml(
						__("School / University"),
					)}" value="${escapeHtml(values.school_univ || "")}" aria-label="${escapeHtml(
						__("School / University"),
					)}">
					<input type="number" class="form-control" data-field="year_of_passing" placeholder="${escapeHtml(
						__("Year"),
					)}" value="${escapeHtml(
						values.year_of_passing || "",
					)}" aria-label="${escapeHtml(__("Year of Passing"))}">
					<select class="form-control" data-field="level" aria-label="${escapeHtml(
						__("Level"),
					)}">${levelOptions}</select>
					<button type="button" class="btn btn-link btn-xs adhd-onb-education-remove" aria-label="${escapeHtml(
						__("Remove this qualification"),
					)}">&times;</button>
				</div>
			`).appendTo(this.$education_rows);
			$row.find("input, select").on("change", () => this._captureCurrentStepValues());
			$row.find(".adhd-onb-education-remove").on("click", () => {
				$row.remove();
				if (!this.$education_rows.children().length) this._addEducationRow({});
				this._captureCurrentStepValues();
			});
		}

		_readEducationRows() {
			if (!this.$education_rows) return this.draft.education || [];
			const rows = [];
			this.$education_rows.find(".adhd-onb-education-row").each((_, el) => {
				const $el = $(el);
				const row = {
					qualification: $el.find('[data-field="qualification"]').val() || "",
					school_univ: $el.find('[data-field="school_univ"]').val() || "",
					year_of_passing: $el.find('[data-field="year_of_passing"]').val() || "",
					level: $el.find('[data-field="level"]').val() || "",
				};
				if (row.qualification || row.school_univ || row.year_of_passing || row.level)
					rows.push(row);
			});
			return rows;
		}

		// -- review: read-only summary built from the server's own description of the draft --

		_renderReviewStep() {
			this.$body.append(
				`<p class="adhd-onb-hint">${escapeHtml(
					__("Nothing has been saved yet. Check this over, then confirm."),
				)}</p>`,
			);
			const $summary = $('<div class="adhd-onb-summary-preview">').appendTo(this.$body);
			$summary.text(__("Checking your answers…"));

			const $submitToggle = $(`
				<label class="adhd-onb-submit-toggle">
					<input type="checkbox" ${this.draft.submit_now ? "checked" : ""}>
					${escapeHtml(
						__(
							"Submit the salary and leave assignments now, so payroll and leave balances are ready immediately",
						),
					)}
				</label>
			`).appendTo(this.$body);
			$submitToggle.find("input").on("change", (e) => {
				this.draft.submit_now = Boolean(e.target.checked);
				this._persist();
			});

			frappe
				.xcall(METHOD_CHECK, { payload: JSON.stringify(buildPayload(this.draft)) })
				.then((result) => this._renderReviewResult($summary, result))
				.catch(() => {
					$summary.empty();
					$summary.append(
						`<p class="adhd-onb-error">${escapeHtml(
							__(
								"Could not check your answers. Check your connection and try Back, then Next again.",
							),
						)}</p>`,
					);
				});
		}

		_renderReviewResult($summary, result) {
			$summary.empty();
			if (!result || !result.ok) {
				const errors = (result && result.errors) || [];
				this._showErrors(errors);
				$summary.append(
					`<p class="adhd-onb-error">${escapeHtml(
						__(
							"Some answers need fixing before this can be confirmed. Use Back to reach them.",
						),
					)}</p>`,
				);
				this.$confirmBtn && this.$confirmBtn.prop("disabled", true);
				return;
			}
			const $list = $('<ul class="adhd-onb-summary-list"></ul>').appendTo($summary);
			(result.summary || []).forEach((line) => {
				$(`<li>${escapeHtml(line.text)}</li>`).appendTo($list);
			});
			this.$confirmBtn && this.$confirmBtn.prop("disabled", false);
		}

		renderFooter() {
			const step = stepAt(this.draft.index);
			this.$footer.empty();
			if (this.draft.index > 0) {
				$(
					`<button type="button" class="btn btn-default adhd-onb-back">${escapeHtml(
						__("Back"),
					)}</button>`,
				)
					.appendTo(this.$footer)
					.on("click", () => this.goBack());
			}
			if (step.id === "review") {
				this.$confirmBtn = $(
					`<button type="button" class="btn btn-primary adhd-onb-confirm" disabled>${escapeHtml(
						__("Confirm and Create"),
					)}</button>`,
				)
					.appendTo(this.$footer)
					.on("click", () => this.confirm());
			} else {
				$(
					`<button type="button" class="btn btn-primary adhd-onb-next">${escapeHtml(
						__("Next"),
					)}</button>`,
				)
					.appendTo(this.$footer)
					.on("click", () => this.goNext());
			}
		}

		_showErrors(errors) {
			this.$errors.empty();
			if (!errors || !errors.length) return this.$errors.hide();
			const $list = $('<ul class="adhd-onb-error-list"></ul>');
			errors.forEach((error) => {
				const label = error.label ? `${escapeHtml(error.label)}: ` : "";
				$(`<li>${label}${escapeHtml(error.message)}</li>`).appendTo($list);
				if (error.field && this.form && this.form.fields_dict[error.field]) {
					const field = this.form.fields_dict[error.field];
					field.$wrapper && field.$wrapper.addClass("has-error");
					field.$wrapper &&
						field.$wrapper
							.find(".adhd-field-error")
							.remove()
							.end()
							.append(
								`<div class="adhd-field-error">${escapeHtml(error.message)}</div>`,
							);
				}
			});
			this.$errors.empty().append($list).show();
		}

		// ---- navigation ----

		async goNext() {
			if (this.busy) return;
			this._captureCurrentStepValues();
			const step = stepAt(this.draft.index);
			this.busy = true;
			try {
				const result = await frappe.xcall(METHOD_CHECK, {
					payload: JSON.stringify(buildPayload(this.draft)),
					step: step.id,
				});
				if (!result || !result.ok) {
					this._showErrors((result && result.errors) || []);
					return;
				}
				this.draft.index = Math.min(WIZARD_STEPS.length - 1, this.draft.index + 1);
				this._persist();
				this.render();
			} catch (e) {
				this._showErrors([
					{
						message: __(
							"Could not reach the server to check this step. Check your connection and try again.",
						),
					},
				]);
			} finally {
				this.busy = false;
			}
		}

		goBack() {
			if (this.busy) return;
			this._captureCurrentStepValues();
			this.draft.index = Math.max(0, this.draft.index - 1);
			this._persist();
			this.render();
		}

		skipStep() {
			if (this.busy) return;
			const step = stepAt(this.draft.index);
			if (!step.skippable) return;
			if (!this.draft.skipped.includes(step.id)) this.draft.skipped.push(step.id);
			this.draft.index = Math.min(WIZARD_STEPS.length - 1, this.draft.index + 1);
			this._persist();
			frappe.show_alert({
				message: __("{0} will be created as a reminder for later.", [step.label]),
				indicator: "blue",
			});
			this.render();
		}

		async confirm() {
			if (this.busy) return;
			this.busy = true;
			this.$confirmBtn && this.$confirmBtn.prop("disabled", true).text(__("Creating…"));
			try {
				const result = await frappe.xcall(METHOD_CREATE, {
					payload: JSON.stringify(buildPayload(this.draft)),
				});
				if (!result || !result.ok) {
					this._renderCreateFailure(result);
					return;
				}
				this.completed = result;
				clearDraft(this.user);
				this.render();
			} catch (e) {
				this._renderCreateFailure(null);
			} finally {
				this.busy = false;
				this.$confirmBtn &&
					this.$confirmBtn.prop("disabled", false).text(__("Confirm and Create"));
			}
		}

		_renderCreateFailure(result) {
			const message =
				(result && result.message) ||
				__(
					"Nothing was created. The server could not be reached; check your connection and try again.",
				);
			this.$errors.empty();
			const $card = $('<div class="adhd-onb-create-error"></div>').appendTo(this.$errors);
			$card.append(`<p>${escapeHtml(message)}</p>`);
			if (result && result.step && result.step !== "review") {
				const targetIndex = stepIndex(result.step);
				if (targetIndex >= 0) {
					$(
						`<button type="button" class="btn btn-default btn-xs">${escapeHtml(
							__("Go back to {0}", [step_label_text(result)]),
						)}</button>`,
					)
						.appendTo($card)
						.on("click", () => {
							this.draft.index = targetIndex;
							this._persist();
							this.render();
						});
				}
			}
			this.$errors.show();
		}

		renderSuccess() {
			this.$header.empty();
			this.$errors.empty().hide();
			this.$footer.empty();
			this.$body.empty();

			const result = this.completed;
			this.$body.append(
				`<h4 class="adhd-onb-summary-heading">${escapeHtml(
					__("{0} onboarded successfully", [result.employee_name]),
				)}</h4>`,
			);

			const $list = $('<ul class="adhd-onb-summary-list"></ul>').appendTo(this.$body);
			(result.documents || []).forEach((doc) => {
				const link = frappe.utils.get_form_link(doc.doctype, doc.name, true);
				$(
					`<li>${escapeHtml(doc.label)}: ${link} (${escapeHtml(doc.status)})</li>`,
				).appendTo($list);
			});

			if (result.reminders && result.reminders.length) {
				const $reminders = $(
					`<div class="adhd-onb-pending"><strong>${escapeHtml(
						__("Pending ToDos"),
					)}</strong></div>`,
				).appendTo(this.$body);
				const $reminderList = $("<ul></ul>").appendTo($reminders);
				result.reminders.forEach((reminder) => {
					const link = frappe.utils.get_form_link("ToDo", reminder.name, true);
					$(`<li>${escapeHtml(reminder.label)}: ${link}</li>`).appendTo($reminderList);
				});
			}

			$(
				`<button type="button" class="btn btn-primary adhd-onb-restart">${escapeHtml(
					__("Onboard Another Employee"),
				)}</button>`,
			)
				.appendTo(this.$body)
				.on("click", () => this.reset());

			$(
				`<button type="button" class="btn btn-default adhd-onb-goto-employee">${escapeHtml(
					__("Go to Employee Record"),
				)}</button>`,
			)
				.appendTo(this.$body)
				.on("click", () => frappe.set_route("Form", "Employee", result.employee));

			if (this.context && this.context.can_create && this.context.can_create.onboarding) {
				$(
					`<button type="button" class="btn btn-link adhd-onb-start-onboarding">${escapeHtml(
						__("Start an Employee Onboarding checklist for {0}", [
							result.employee_name,
						]),
					)}</button>`,
				)
					.appendTo(this.$body)
					.on("click", () =>
						frappe.new_doc("Employee Onboarding", {
							employee: result.employee,
							employee_name: result.employee_name,
						}),
					);
			}
		}

		reset() {
			this.draft = emptyDraft();
			this.completed = null;
			clearDraft(this.user);
			this.render();
		}

		// Exposed for the ADHD-020 test harness / other callers that want to introspect a running wizard.
		getState() {
			return {
				index: this.draft.index,
				step: stepAt(this.draft.index).id,
				skipped: this.draft.skipped.slice(),
				completed: Boolean(this.completed),
				draft: JSON.parse(JSON.stringify(this.draft)),
			};
		}
	}

	function step_label_text(result) {
		return (
			result.step_label ||
			(stepIndex(result.step) >= 0 ? stepAt(stepIndex(result.step)).label : result.step)
		);
	}

	frappe.pages[PAGE_ROUTE].on_page_load = function (wrapper) {
		const page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __("Onboard New Employee"),
			single_column: true,
		});
		const wizard = new EmployeeOnboardingWizard(page);
		wizard.start();
		wrapper.employee_onboarding_wizard = wizard;
	};

	// Pure, DOM-free helpers a test can call directly without building the whole page.
	Object.assign(erpnext.adhd.onboardingWizard, {
		WIZARD_STEPS,
		stepIndex,
		stepAt,
		progressText,
		draftKey,
		emptyDraft,
		loadDraft,
		saveDraft,
		clearDraft,
		buildPayload,
		buildFieldsForStep,
	});
})();
