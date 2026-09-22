// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// Feature A: Journeys. The Journey form's own board: tasks grouped Now / Next / Later / Done instead of
// one flat table, with a one-click "Mark done" on whatever is actually actionable right now. The plain
// child table (below, collapsed) still exists for anyone who wants to see or export every row.

frappe.provide("hrms.journeys");

(() => {
	const COMPLETE_TASK = "hrms.hr.journeys.complete_task";
	const SKIP_TASK = "hrms.hr.journeys.skip_task";

	function esc(value) {
		return frappe.utils.escape_html(value == null ? "" : String(value));
	}

	// Now: open. Done: Done/Skipped. Next: blocked but one step away (its dependency is currently Open,
	// or it has no dependency at all and simply has not been opened yet). Later: blocked behind something
	// that is itself still blocked.
	function groupTasks(tasks) {
		const groups = { now: [], next: [], later: [], done: [] };
		const byTitle = {};
		(tasks || []).forEach((task) => {
			byTitle[task.title] = task;
		});

		(tasks || []).forEach((task) => {
			if (task.status === "Open") {
				groups.now.push(task);
			} else if (task.status === "Done" || task.status === "Skipped") {
				groups.done.push(task);
			} else {
				const dep = task.depends_on ? byTitle[task.depends_on] : null;
				const oneStepAway =
					!dep ||
					dep.status === "Open" ||
					dep.status === "Done" ||
					dep.status === "Skipped";
				(oneStepAway ? groups.next : groups.later).push(task);
			}
		});
		return groups;
	}

	function statusLabel(task) {
		if (task.status === "Done") return __("Done");
		if (task.status === "Skipped") return __("Skipped");
		return "";
	}

	function taskCard(task, { actionable }) {
		const meta = [];
		if (task.owner_user) meta.push(esc(task.owner_user));
		if (task.due_date) meta.push(esc(task.due_date));
		const metaHtml = meta.length
			? `<div class="journeys-card-meta">${meta.join(" &middot; ")}</div>`
			: "";

		const desc = task.description
			? `<div class="journeys-card-desc">${esc(task.description)}</div>`
			: "";

		let actions = "";
		if (actionable) {
			actions = `<div class="journeys-card-actions">
				<button type="button" class="btn btn-xs btn-primary journeys-mark-done" data-task="${esc(
					task.name,
				)}">${esc(__("Mark done"))}</button>
				<button type="button" class="btn btn-xs btn-link journeys-skip" data-task="${esc(
					task.name,
				)}">${esc(__("Skip"))}</button>
			</div>`;
		} else if (task.status === "Done" || task.status === "Skipped") {
			actions = `<div class="journeys-card-meta">${esc(statusLabel(task))}${
				task.completed_by ? " &middot; " + esc(task.completed_by) : ""
			}</div>`;
		}

		return `<div class="journeys-card" data-task="${esc(task.name)}">
			<div class="journeys-card-title">${esc(task.title)}</div>
			${desc}
			${metaHtml}
			${actions}
		</div>`;
	}

	function column(label, tasks, options) {
		const cards = tasks.length
			? tasks.map((t) => taskCard(t, options)).join("")
			: `<p class="journeys-col-empty">${esc(__("Nothing here."))}</p>`;
		return `<div class="journeys-col">
			<div class="journeys-col-head">${esc(label)} <span class="journeys-col-count">${
				tasks.length
			}</span></div>
			<div class="journeys-col-body">${cards}</div>
		</div>`;
	}

	function buildBoardHtml(tasks) {
		const groups = groupTasks(tasks);
		return `<div class="journeys-board">
			${column(__("Now"), groups.now, { actionable: true })}
			${column(__("Next"), groups.next, { actionable: false })}
			${column(__("Later"), groups.later, { actionable: false })}
			${column(__("Done"), groups.done, { actionable: false })}
		</div>`;
	}

	async function markTask(frm, method, task) {
		try {
			await frappe.call({ method, args: { journey: frm.doc.name, task } });
			await frm.reload_doc();
		} catch (error) {
			// frappe.call already shows the server's error message to the user
		}
	}

	function renderBoard(frm) {
		const field = frm.fields_dict.tasks_board;
		if (!field || !field.$wrapper) return;

		field.$wrapper.empty().append(buildBoardHtml(frm.doc.tasks || []));

		field.$wrapper.find(".journeys-mark-done").on("click", (event) => {
			markTask(frm, COMPLETE_TASK, $(event.currentTarget).attr("data-task"));
		});
		field.$wrapper.find(".journeys-skip").on("click", (event) => {
			const task = $(event.currentTarget).attr("data-task");
			frappe.confirm(__("Skip this step?"), () => markTask(frm, SKIP_TASK, task));
		});
	}

	hrms.journeys.renderBoard = renderBoard;

	frappe.ui.form.on("Journey", {
		refresh(frm) {
			if (frm.is_new && frm.is_new()) return;
			renderBoard(frm);

			if (frm.doc.status !== "Cancelled" && frm.doc.status !== "Completed") {
				frm.add_custom_button(__("Cancel Journey"), () => {
					frappe.confirm(
						__("Cancel this journey? Its open tasks will be closed."),
						async () => {
							await frappe.call({
								method: "hrms.hr.journeys.cancel_journey",
								args: { journey: frm.doc.name },
							});
							frm.reload_doc();
						},
					);
				});
			}
		},
		tasks_on_form_rendered(frm) {
			// the grid re-rendered (e.g. after a reload); keep the board in sync
			renderBoard(frm);
		},
	});
})();
