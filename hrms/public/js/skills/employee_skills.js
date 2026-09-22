// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

// Adds a "Skills" section to the Employee form: what the Skills Cloud engine has inferred (with the
// evidence sentence behind every suggestion), the employee's confirmed skills, and any gaps against
// their own designation. Registered on its own - composes with the existing `frappe.ui.form.on`
// handlers for Employee instead of editing employee.js.
//
// Not gated on Focus Mode (this is a product feature, not an ADHD aid), but keeps the same calm,
// one-thing-at-a-time spirit: a handful of short, explained suggestions, never a wall of options.

frappe.ui.form.on("Employee", {
	refresh(frm) {
		hrms.skills_cloud.render_section(frm);
	},
});

hrms.skills_cloud = {
	render_section(frm) {
		if (frm.is_new()) return;

		// Avoid piling up duplicate sections across refreshes.
		frm.$wrapper.find(".skills-cloud-section").remove();

		const $body = frm.dashboard.add_section(
			`<div class="skills-cloud-section"><span class="text-muted">${__(
				"Loading skills...",
			)}</span></div>`,
			__("Skills"),
		);
		$body.closest(".form-dashboard-section").addClass("skills-cloud-section");

		frappe.call({
			method: "hrms.hr.skills_cloud.get_employee_skill_summary",
			args: { employee: frm.doc.name },
			callback: (r) => {
				if (!r || !r.message) {
					$body.html(
						`<span class="text-muted">${__("No skills data available.")}</span>`,
					);
					return;
				}
				hrms.skills_cloud.render_body(frm, $body, r.message);
			},
			error: () => {
				// A manager/HR-only or permission-denied case: say nothing rather than show a scary error.
				$body.closest(".form-dashboard-section").remove();
			},
		});
	},

	render_body(frm, $body, data) {
		const esc = frappe.utils.escape_html;
		const parts = [];

		parts.push(`<div class="skills-cloud-toolbar" style="margin-bottom: 10px;">
			<button class="btn btn-xs btn-default skills-cloud-refresh">${__(
				"Check for new suggestions",
			)}</button>
			<a class="btn btn-xs btn-default" href="/app/talent-marketplace">${__(
				"Open Talent Marketplace",
			)}</a>
		</div>`);

		// --- confirmed skills ---
		parts.push(`<div class="skills-cloud-current" style="margin-bottom: 14px;">`);
		if (data.skills && data.skills.length) {
			parts.push(
				data.skills
					.map((s) => {
						const stars = Math.round((s.proficiency || 0) * 5);
						return `<span class="indicator-pill blue" style="margin: 0 6px 6px 0;" title="${esc(
							__("{0} out of 5", [stars]),
						)}">${esc(s.skill)} &middot; ${stars}/5</span>`;
					})
					.join(""),
			);
		} else {
			parts.push(`<span class="text-muted">${__("No confirmed skills yet.")}</span>`);
		}
		parts.push(`</div>`);

		// --- suggestions, each with its evidence line ---
		parts.push(`<div class="skills-cloud-suggestions">`);
		if (data.suggestions && data.suggestions.length) {
			parts.push(
				`<div class="text-muted small" style="margin-bottom: 6px;">${__(
					"Suggested from your recent work",
				)}</div>`,
			);
			data.suggestions.forEach((s) => {
				parts.push(`
					<div class="skills-cloud-suggestion" data-name="${esc(
						s.name,
					)}" style="border: 1px solid var(--border-color); border-radius: var(--border-radius); padding: 8px 10px; margin-bottom: 6px;">
						<div><strong>${esc(s.skill)}</strong> <span class="text-muted small">${esc(
							__("{0}% confidence", [s.confidence]),
						)}</span></div>
						<div class="text-muted small" style="margin: 4px 0;">&ldquo;${esc(s.evidence)}&rdquo;</div>
						<button class="btn btn-xs btn-primary skills-cloud-accept">${__("Accept")}</button>
						<button class="btn btn-xs btn-default skills-cloud-dismiss">${__("Dismiss")}</button>
					</div>
				`);
			});
		} else {
			parts.push(`<span class="text-muted">${__("No new suggestions right now.")}</span>`);
		}
		parts.push(`</div>`);

		// --- gaps vs. own designation ---
		if (data.gaps && data.gaps.length) {
			parts.push(`<div class="skills-cloud-gaps" style="margin-top: 14px;">`);
			parts.push(
				`<div class="text-muted small" style="margin-bottom: 6px;">${__(
					"Gaps vs. your designation",
				)}</div>`,
			);
			parts.push(
				data.gaps
					.map((g) => {
						const label =
							g.gap_type === "missing"
								? __("missing")
								: __("below the wanted level ({0}/5)", [
										Math.round((g.minimum_proficiency || 0) * 5),
								  ]);
						return `<span class="indicator-pill orange" style="margin: 0 6px 6px 0;">${esc(
							g.skill,
						)} &middot; ${esc(label)}</span>`;
					})
					.join(""),
			);
			parts.push(`</div>`);
		}

		$body.html(parts.join(""));

		$body.find(".skills-cloud-refresh").on("click", () => {
			frappe.call({
				method: "hrms.hr.skills_cloud.refresh_my_suggestions",
				args: { employee: frm.doc.name },
				freeze: true,
				freeze_message: __("Checking for new suggestions..."),
				callback: () => hrms.skills_cloud.render_section(frm),
			});
		});

		$body.find(".skills-cloud-accept").on("click", function () {
			const name = $(this).closest(".skills-cloud-suggestion").data("name");
			frappe.call({
				method: "hrms.hr.skills_cloud.accept_suggestion",
				args: { name },
				callback: () => {
					frappe.show_alert({ message: __("Skill added"), indicator: "green" });
					hrms.skills_cloud.render_section(frm);
				},
			});
		});

		$body.find(".skills-cloud-dismiss").on("click", function () {
			const name = $(this).closest(".skills-cloud-suggestion").data("name");
			frappe.call({
				method: "hrms.hr.skills_cloud.dismiss_suggestion",
				args: { name },
				callback: () => hrms.skills_cloud.render_section(frm),
			});
		});
	},
};
