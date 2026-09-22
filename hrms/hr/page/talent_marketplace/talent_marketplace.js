// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.pages["talent-marketplace"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Talent Marketplace"),
		single_column: true,
	});

	hrms.talent_marketplace.make(page);
};

hrms.talent_marketplace = {
	make(page) {
		const me = this;
		me.page = page;
		me.$wrapper = $(`<div class="talent-marketplace">
			<div class="tm-tabs" style="margin-bottom: 14px;">
				<button class="btn btn-sm btn-primary tm-tab" data-tab="for-you">${__(
					"Opportunities for you",
				)}</button>
				<button class="btn btn-sm btn-default tm-tab" data-tab="owner">${__("For owners")}</button>
			</div>
			<div class="tm-for-you"></div>
			<div class="tm-owner" style="display: none;"></div>
		</div>`).appendTo(page.main);

		page.set_primary_action(
			__("Post an Opportunity"),
			() => frappe.new_doc("Talent Opportunity"),
			"add",
		);

		me.$wrapper.find(".tm-tab").on("click", function () {
			const tab = $(this).data("tab");
			me.$wrapper.find(".tm-tab").removeClass("btn-primary").addClass("btn-default");
			$(this).removeClass("btn-default").addClass("btn-primary");
			me.$wrapper.find(".tm-for-you").toggle(tab === "for-you");
			me.$wrapper.find(".tm-owner").toggle(tab === "owner");
			if (tab === "owner") me.render_owner_tab();
		});

		me.render_for_you_tab();
	},

	render_for_you_tab() {
		const me = this;
		const $tab = me.$wrapper.find(".tm-for-you");
		$tab.html(`<div class="text-muted">${__("Loading opportunities...")}</div>`);

		frappe.call({
			method: "hrms.hr.skills_cloud.list_open_opportunities",
			args: { page_length: 50 },
			callback: (r) => {
				const rows = (r && r.message) || [];
				if (!rows.length) {
					$tab.html(
						`<div class="marketplace-empty text-muted">${__(
							"No open opportunities right now.",
						)}</div>`,
					);
					return;
				}
				// Highest match first, unmatched (no skills overlap yet) last but still visible.
				rows.sort((a, b) => (b.match_score || 0) - (a.match_score || 0));
				$tab.html(rows.map((o) => me.render_opportunity_card(o)).join(""));
				me.wire_interest_buttons($tab);
			},
		});
	},

	render_opportunity_card(o) {
		const esc = frappe.utils.escape_html;
		const why = (o.why || [])
			.map((w) => `<span class="indicator-pill gray">${esc(w)}</span>`)
			.join("");
		const action = o.already_interested
			? `<button class="btn btn-xs btn-default tm-withdraw" data-opportunity="${esc(
					o.name,
			  )}">${__("Withdraw interest")}</button>`
			: `<button class="btn btn-xs btn-primary tm-express" data-opportunity="${esc(
					o.name,
			  )}">${__("Express interest")}</button>`;

		return `<div class="opportunity-card" data-opportunity="${esc(o.name)}">
			<div style="display: flex; justify-content: space-between; align-items: baseline;">
				<div>
					<span class="opportunity-title">${esc(o.title)}</span>
					<span class="indicator-pill blue" style="margin-left: 8px;">${esc(o.type)}</span>
				</div>
				<div class="opportunity-match">${o.match_score || 0}%</div>
			</div>
			<div class="text-muted small" style="margin-top: 4px;">
				${o.department ? esc(o.department) + " &middot; " : ""}${
					o.hours_per_week ? esc(String(o.hours_per_week)) + " " + __("hrs/week") : ""
				}
			</div>
			<div class="opportunity-why">${why}</div>
			<div style="margin-top: 8px;">${action}</div>
		</div>`;
	},

	wire_interest_buttons($tab) {
		const me = this;
		$tab.find(".tm-express").on("click", function () {
			const opportunity = $(this).data("opportunity");
			frappe.call({
				method: "hrms.hr.skills_cloud.express_interest",
				args: { opportunity },
				callback: (r) => {
					frappe.show_alert({
						message: __("Interest sent - {0}% match", [
							(r.message && r.message.match_score) || 0,
						]),
						indicator: "green",
					});
					me.render_for_you_tab();
				},
			});
		});
		$tab.find(".tm-withdraw").on("click", function () {
			const opportunity = $(this).data("opportunity");
			frappe.call({
				method: "hrms.hr.skills_cloud.withdraw_interest",
				args: { opportunity },
				callback: () => me.render_for_you_tab(),
			});
		});
	},

	render_owner_tab() {
		const me = this;
		const $tab = me.$wrapper.find(".tm-owner");
		if ($tab.data("loaded")) return;
		$tab.data("loaded", true);

		frappe.call({
			method: "hrms.hr.skills_cloud.list_my_opportunities",
			callback: (r) => {
				const opportunities = (r && r.message) || [];
				if (!opportunities.length) {
					$tab.html(
						`<div class="marketplace-empty text-muted">${__(
							"You don't own any opportunities yet.",
						)}</div>`,
					);
					return;
				}
				const options = opportunities
					.map(
						(o) =>
							`<option value="${frappe.utils.escape_html(
								o.name,
							)}">${frappe.utils.escape_html(o.title)} (${frappe.utils.escape_html(
								o.status,
							)})</option>`,
					)
					.join("");
				$tab.html(`
					<div style="margin-bottom: 10px;">
						<select class="form-control tm-owner-select" style="max-width: 360px; display: inline-block;">${options}</select>
					</div>
					<div class="tm-owner-candidates"></div>
				`);
				$tab.find(".tm-owner-select").on("change", function () {
					me.render_owner_candidates($tab, $(this).val());
				});
				me.render_owner_candidates($tab, opportunities[0].name);
			},
		});
	},

	render_owner_candidates($tab, opportunity) {
		const esc = frappe.utils.escape_html;
		const $out = $tab.find(".tm-owner-candidates");
		$out.html(`<div class="text-muted">${__("Loading candidates...")}</div>`);

		frappe.call({
			method: "hrms.hr.skills_cloud.get_opportunity_interests",
			args: { opportunity },
			callback: (r) => {
				const interested = (r && r.message) || [];
				const interestedHtml = interested.length
					? interested
							.map(
								(c) => `<div class="candidate-card">
						<div><strong>${esc(c.employee_name)}</strong> <span class="text-muted small">${esc(
							c.status,
						)}</span></div>
						<div>${c.match_score || 0}%</div>
					</div>`,
							)
							.join("")
					: `<div class="text-muted small">${__(
							"No one has expressed interest yet.",
					  )}</div>`;

				frappe.call({
					method: "hrms.hr.skills_cloud.get_matching_candidates",
					args: { opportunity },
					callback: (r2) => {
						const suggested = (r2 && r2.message) || [];
						const suggestedHtml = suggested.length
							? suggested
									.map(
										(c) => `<div class="candidate-card">
								<div><strong>${esc(c.employee_name)}</strong> <span class="text-muted small">${esc(
									c.designation || "",
								)}</span></div>
								<div>${c.match_score || 0}%</div>
							</div>`,
									)
									.join("")
							: `<div class="text-muted small">${__(
									"No algorithmic matches yet.",
							  )}</div>`;

						$out.html(`
							<div style="margin-bottom: 16px;">
								<div class="text-muted small" style="margin-bottom: 6px;">${__("Expressed interest")}</div>
								${interestedHtml}
							</div>
							<div>
								<div class="text-muted small" style="margin-bottom: 6px;">${__("Suggested candidates")}</div>
								${suggestedHtml}
							</div>
						`);
					},
				});
			},
		});
	},
};
