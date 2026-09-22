// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// Feature A: Journeys. A calm "Journeys" panel on the Employee form: this employee's active journeys,
// each with a progress bar and only the next open step (never the whole checklist), plus a "Start a
// journey" button for HR. This is a product feature, not a Focus-Mode aid, so it is always shown (subject
// to what the server lets this viewer read) — but it still keeps things quiet: one panel, one button.

frappe.provide("hrms.journeys");

(() => {
	const PANEL_CLASS = "journeys-panel";
	const GET_JOURNEYS = "hrms.hr.journeys.get_employee_journeys";
	const GET_TEMPLATES = "hrms.hr.journeys.get_applicable_templates";
	const START_JOURNEY = "hrms.hr.journeys.start_journey";
	const HR_ROLES = ["HR Manager", "HR User", "System Manager"];

	function esc(value) {
		return frappe.utils.escape_html(value == null ? "" : String(value));
	}

	function isHR() {
		const roles =
			frappe.user_roles || (frappe.boot && frappe.boot.user && frappe.boot.user.roles) || [];
		return HR_ROLES.some((role) => roles.includes(role));
	}

	function journeyLink(name, text) {
		return frappe.utils.get_form_link("Journey", name, true, esc(text));
	}

	function progressBar(percent) {
		const pct = Math.max(0, Math.min(100, Number(percent) || 0));
		return `<div class="journeys-progress" role="progressbar" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100">
			<div class="journeys-progress-fill" style="width: ${pct}%"></div>
		</div>`;
	}

	function journeyRow(journey) {
		const nextTask = journey.next_task
			? esc(journey.next_task.title) +
			  (journey.next_task.due_date
					? ` <span class="journeys-due">(${esc(journey.next_task.due_date)})</span>`
					: "")
			: `<span class="journeys-done-note">${esc(__("Nothing open right now"))}</span>`;

		return `<div class="journeys-row">
			<div class="journeys-row-head">
				${journeyLink(journey.name, journey.journey_type || journey.journey_template)}
				<span class="journeys-status journeys-status-${esc(
					(journey.status || "").toLowerCase().replace(/ /g, "-"),
				)}">${esc(journey.status)}</span>
			</div>
			${progressBar(journey.progress_percent)}
			<div class="journeys-next">${nextTask}</div>
		</div>`;
	}

	function buildPanelHtml(journeys, canStart) {
		const rows = (journeys || []).map(journeyRow).join("");
		const body = rows || `<p class="journeys-empty">${esc(__("No active journeys."))}</p>`;
		const button = canStart
			? `<button type="button" class="btn btn-xs btn-default journeys-start-btn">${esc(
					__("Start a journey"),
			  )}</button>`
			: "";

		return `<div class="${PANEL_CLASS}">
			<div class="journeys-panel-head">
				<span class="journeys-panel-label">${esc(__("Journeys"))}</span>
				${button}
			</div>
			<div class="journeys-panel-body">${body}</div>
		</div>`;
	}

	function removePanel(frm) {
		if (frm && frm.$wrapper) frm.$wrapper.find(`.${PANEL_CLASS}`).remove();
	}

	function insertPanel(frm, $panel) {
		const $layout =
			frm.layout && frm.layout.wrapper
				? frm.layout.wrapper
				: frm.$wrapper.find(".form-layout").first();
		$layout.prepend($panel);
	}

	async function fetchJourneys(employee) {
		try {
			const response = await frappe.call({
				method: GET_JOURNEYS,
				args: { employee },
				silent: true,
			});
			return (response && response.message) || [];
		} catch (error) {
			return [];
		}
	}

	async function openStartDialog(frm) {
		let templates = [];
		try {
			const response = await frappe.call({
				method: GET_TEMPLATES,
				args: { employee: frm.doc.name },
				silent: true,
			});
			templates = (response && response.message) || [];
		} catch (error) {
			templates = [];
		}

		if (!templates.length) {
			frappe.msgprint(__("No journey templates apply to this employee yet."));
			return;
		}

		const dialog = new frappe.ui.Dialog({
			title: __("Start a journey"),
			fields: [
				{
					fieldname: "journey_template",
					fieldtype: "Select",
					label: __("Journey"),
					reqd: 1,
					options: templates.map((t) => ({
						label: `${t.title} (${t.journey_type})`,
						value: t.name,
					})),
				},
				{
					fieldname: "start_date",
					fieldtype: "Date",
					label: __("Start Date"),
					default: frappe.datetime.get_today(),
					reqd: 1,
				},
			],
			primary_action_label: __("Start"),
			primary_action: async (values) => {
				dialog.hide();
				await frappe.call({
					method: START_JOURNEY,
					args: {
						template: values.journey_template,
						employee: frm.doc.name,
						start_date: values.start_date,
					},
				});
				frappe.show_alert({ message: __("Journey started."), indicator: "green" });
				renderJourneysPanel(frm);
			},
		});
		dialog.show();
	}

	async function renderJourneysPanel(frm) {
		if (!frm || !frm.doc || frm.doc.__islocal || (frm.is_new && frm.is_new())) return;

		// an older render that answers after a newer one (or after the form navigated away) is dropped
		const token = (frm._journeysToken || 0) + 1;
		frm._journeysToken = token;

		const employee = frm.doc.name;
		const journeys = await fetchJourneys(employee);

		if (token !== frm._journeysToken || !frm.doc || frm.doc.name !== employee) return;

		removePanel(frm);
		const $panel = $(buildPanelHtml(journeys, isHR()));
		insertPanel(frm, $panel);
		$panel.find(".journeys-start-btn").on("click", () => openStartDialog(frm));
	}

	hrms.journeys.renderJourneysPanel = renderJourneysPanel;

	frappe.ui.form.on("Employee", {
		// Frappe has no "after_load" form event; refresh runs on the first open, after a save and on reload
		refresh(frm) {
			renderJourneysPanel(frm);
		},
	});
})();
