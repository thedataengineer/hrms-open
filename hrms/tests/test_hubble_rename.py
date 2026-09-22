# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""The people-facing name is "Hubble", not "Frappe HR". The Python package, module paths and doctype names stay
`hrms`: they are technical names no page shows (files only, no site needed)."""

import json
import re
import unittest
from pathlib import Path

import hrms

APP = Path(hrms.__file__).parent


def _hooks_value(name):
	text = (APP / "hooks.py").read_text()
	match = re.search(rf'^{name}\s*=\s*(?:"{{3}}|")(.+?)(?:"{{3}}|")\s*$', text, re.MULTILINE)
	return match.group(1) if match else None


class TestHubbleName(unittest.TestCase):
	def test_the_app_is_called_hubble(self):
		self.assertEqual(_hooks_value("app_title"), "Hubble")
		self.assertEqual(_hooks_value("app_description"), "Run your people, not your paperwork")
		# the technical name is unchanged: every import and other app depends on it
		self.assertEqual(_hooks_value("app_name"), "hrms")

	def test_every_logo_the_app_points_at_exists(self):
		text = (APP / "hooks.py").read_text() + (APP / "desktop_icon" / "hubble.json").read_text()
		text += (APP / "subscription_utils.py").read_text()
		logos = re.findall(r"assets/hrms/images/([\w.-]+)", text)
		self.assertTrue(logos)
		for name in logos:
			self.assertTrue((APP / "public" / "images" / name).exists(), name)
			self.assertNotIn("frappe-hr", name)

	def test_no_desktop_icon_carries_the_old_name(self):
		for path in (APP / "desktop_icon").glob("*.json"):
			doc = json.loads(path.read_text())
			for key in ("name", "label", "parent_icon", "link_to"):
				self.assertNotIn("Frappe HR", str(doc.get(key) or ""), f"{path.name}: {key}")

	def test_no_translatable_string_says_frappe_hr(self):
		pattern = re.compile(r"""\b_{1,2}\(\s*(["'`])(?:(?!\1).)*Frappe HR""", re.DOTALL)
		found = []
		for suffix in ("*.js", "*.py", "*.html"):
			for path in APP.rglob(suffix):
				if {"tests", "patches", "frontend", "roster", "dist", "node_modules"} & set(path.parts):
					continue
				if path.name.startswith("test_"):
					continue
				if pattern.search(path.read_text(errors="ignore")):
					found.append(str(path.relative_to(APP)))
		self.assertEqual(found, [])

	def test_the_rename_patch_runs_after_the_model_sync(self):
		lines = (APP / "patches.txt").read_text().splitlines()
		patch = "hrms.patches.v17_0.rename_frappe_hr_to_hubble"
		self.assertIn(patch, lines)
		self.assertGreater(lines.index(patch), lines.index("[post_model_sync]"))
