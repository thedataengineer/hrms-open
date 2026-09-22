# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""The three Hubble pages a person reaches from the module navigation: the onboarding wizard, the payroll
cycle checklist and the talent marketplace. Each is a sidebar entry of its module (the per-module `Sidebar`
export that `bench migrate` imports) and a shortcut card on the module's home workspace. Files only, no site
needed, except the last test, which reads the site when one is connected."""

import json
import unittest
from pathlib import Path

import frappe

import hrms

APP = Path(hrms.__file__).parent
FRAPPE = Path(frappe.__file__).parent
SHIPPED_ON = "2026-08-07 12:00:00.000000"  # the upstream export the entries were added to

# page route -> (module folder, sidebars that list it, workspace that carries a card)
PAGES = {
	"employee-onboarding-wizard": ("hr", ["tenure"], "tenure"),
	"focus-payroll-checklist": ("payroll", ["payroll"], "payroll"),
	"talent-marketplace": ("hr", ["performance", "recruitment"], "performance"),
}


def _sidebar(module):
	return json.loads((APP / module / "sidebar" / module / f"{module}.json").read_text())


def _workspace(module):
	return json.loads((APP / module / "workspace" / module / f"{module}.json").read_text())


def _page(route, module):
	slug = route.replace("-", "_")
	return json.loads((APP / module / "page" / slug / f"{slug}.json").read_text())


class TestFocusShortcuts(unittest.TestCase):
	def test_every_page_is_a_standard_page_of_the_app(self):
		for route, (module, _sidebars, _workspace) in PAGES.items():
			page = _page(route, module)
			self.assertEqual(page["name"], route)
			self.assertEqual(page["standard"], "Yes")
			self.assertTrue(page["title"])

	def test_each_module_sidebar_lists_its_page_under_the_page_title(self):
		for route, (module, sidebars, _workspace) in PAGES.items():
			title = _page(route, module)["title"]
			for sidebar in sidebars:
				doc = _sidebar(sidebar)
				items = [item for item in doc["items"] if item.get("link_to") == route]
				self.assertEqual(len(items), 1, f"{sidebar}: {route}")
				item = items[0]
				self.assertEqual(item["type"], "Link")
				self.assertEqual(item["link_type"], "Page")
				self.assertEqual(item["label"], title)
				self.assertEqual(item["child"], 0, "a top-level entry, not one folded under a section")
				self.assertTrue(item["icon"], f"{sidebar}: {route} has no icon")
				# an entry is imported only when the file is newer than the row the site holds
				self.assertGreater(doc["modified"], SHIPPED_ON, sidebar)

	def test_every_icon_exists_in_the_desk_icon_sprite(self):
		sprite = (FRAPPE / "public" / "icons" / "lucide" / "icons.svg").read_text()
		for route, (_module, sidebars, _workspace) in PAGES.items():
			for sidebar in sidebars:
				icon = next(item for item in _sidebar(sidebar)["items"] if item.get("link_to") == route)[
					"icon"
				]
				self.assertIn(f'id="icon-{icon}"', sprite, f"{sidebar}: icon {icon}")

	def test_each_home_workspace_carries_a_shortcut_card_for_its_page(self):
		for route, (module, _sidebars, workspace) in PAGES.items():
			title = _page(route, module)["title"]
			doc = _workspace(workspace)
			rows = [row for row in doc.get("shortcuts", []) if row.get("link_to") == route]
			self.assertEqual(len(rows), 1, f"{workspace}: {route}")
			self.assertEqual(rows[0]["type"], "Page")
			self.assertEqual(rows[0]["label"], title)
			blocks = [
				block
				for block in json.loads(doc["content"])
				if block.get("type") == "shortcut" and block.get("data", {}).get("shortcut_name") == title
			]
			self.assertEqual(len(blocks), 1, f"{workspace}: card block for {title}")
			self.assertGreater(doc["modified"], SHIPPED_ON, workspace)

	def test_nothing_a_person_reads_says_adhd_or_the_old_names(self):
		for route, (module, sidebars, workspace) in PAGES.items():
			texts = [_page(route, module)["title"]]
			texts += [
				item["label"]
				for s in sidebars
				for item in _sidebar(s)["items"]
				if item.get("link_to") == route
			]
			texts += [row["label"] for row in _workspace(workspace).get("shortcuts", [])]
			for text in texts:
				for word in ("ADHD", "ERPNext", "Frappe HR", "HRMS"):
					self.assertNotIn(word, text)

	@unittest.skipUnless(getattr(frappe.local, "db", None), "no site connected")
	def test_the_site_holds_the_entries_once_migrated(self):
		for route, (_module, sidebars, workspace) in PAGES.items():
			for sidebar in sidebars:
				sidebar_name = _sidebar(sidebar)["name"]
				self.assertTrue(
					frappe.db.exists(
						"Sidebar Item", {"parent": sidebar_name, "parenttype": "Sidebar", "link_to": route}
					),
					f"Sidebar {sidebar_name} on the site lacks {route}: run bench migrate",
				)
			workspace_name = _workspace(workspace)["name"]
			self.assertTrue(
				frappe.db.exists(
					"Workspace Shortcut",
					{"parent": workspace_name, "parenttype": "Workspace", "link_to": route},
				),
				f"Workspace {workspace_name} on the site lacks {route}: run bench migrate",
			)


if __name__ == "__main__":
	unittest.main()
