# Copyright (C) 2026 Retriva.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.  See the License for the specific language governing
# permissions and limitations under the License.

"""Deterministic integrity check for the project-wide spec/ADR registry
(Constitution v1.2 §43).

Runs in the core test suite (the repository-standard gate) and is
wired into continuous integration when CI infrastructure exists
(Spec 024 plan.md §9).  Fails on:

- registry entries whose artifact path does not exist;
- malformed entries (missing required fields, unknown status);
- duplicate future allocations (specifications above the historical
  backfill boundary 023; ADRs above 028 and outside the grandfathered
  4-digit gateway-local series);
- within-repository number duplicates that are not explicitly
  documented historical collisions (the 014 collision).

Historical artifacts (001–023 specs, 001–028 project-wide ADRs, the
gateway-local series) are grandfathered: numbers and paths are
preserved, never renumbered (F-2 disposition).
"""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
REGISTRY = (HERE.parent / "docs" / "governance"
            / "spec-adr-registry.yaml")
WORKSPACE = HERE.parent.parent

ALLOWED_STATUSES = {
    "allocated", "proposed", "accepted", "superseded", "rejected",
    "withdrawn",
}
REQUIRED_FIELDS = {"number", "title", "repository", "path", "status",
                   "allocated"}
#: Historical backfill boundary: the highest pre-registry number in
#: the project-wide series (Specs 019–023; ADRs up to 028).  Numbers
#: above these are registry-era allocations and must be unique
#: project-wide.
SPEC_HISTORICAL_MAX = 23
ADR_HISTORICAL_MAX = 28


def _load():
    with REGISTRY.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


class SpecAdrRegistryIntegrity(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.data = _load()
        cls.specs = cls.data.get("specifications") or []
        cls.adrs = cls.data.get("adrs") or []
        cls.repo_roots = {
            repo: (WORKSPACE / repo)
            for repo in sorted({entry["repository"]
                                 for entry in cls.specs + cls.adrs})
        }

    def _artifact_path(self, entry):
        repo = self.repo_roots.get(entry["repository"])
        if repo is None:
            return None
        return repo / entry["path"]

    def _assert_entries_well_formed(self, entries, kind):
        self.assertTrue(entries, f"{kind} section is empty")
        for entry in entries:
            missing = REQUIRED_FIELDS - set(entry)
            self.assertFalse(
                missing,
                f"{kind} entry missing fields {missing}: {entry}")
            self.assertIn(
                entry["status"], ALLOWED_STATUSES,
                f"unknown status in {kind} entry: {entry}")
            number = str(entry["number"]).strip()
            self.assertRegex(
                number, r"^\d+$",
                f"{kind} number must be digits: {entry}")

    def test_specifications_well_formed(self):
        self._assert_entries_well_formed(self.specs, "specification")

    def test_adrs_well_formed(self):
        self._assert_entries_well_formed(self.adrs, "adr")

    def test_all_artifact_paths_exist(self):
        for entry in self.specs + self.adrs:
            path = self._artifact_path(entry)
            self.assertIsNotNone(
                path, f"unknown repository {entry['repository']}")
            self.assertTrue(
                path.exists(),
                f"registry entry points at a missing artifact: "
                f"{entry['repository']}/{entry['path']}")

    def test_no_duplicate_future_spec_allocations(self):
        future = [
            entry for entry in self.specs
            if int(entry["number"]) > SPEC_HISTORICAL_MAX
        ]
        seen = {}
        for entry in future:
            number = entry["number"]
            self.assertNotIn(
                number, seen,
                f"duplicate future spec allocation {number}: "
                f"{seen.get(number)} vs {entry}")
            seen[number] = entry

    def test_no_duplicate_future_adr_allocations(self):
        future = [
            entry for entry in self.adrs
            if len(str(entry["number"]).lstrip("0")) <= 3
            and int(entry["number"]) > ADR_HISTORICAL_MAX
        ]
        seen = {}
        for entry in future:
            number = entry["number"]
            self.assertNotIn(
                number, seen,
                f"duplicate future ADR allocation {number}: "
                f"{seen.get(number)} vs {entry}")
            seen[number] = entry

    def test_within_repository_duplicates_are_documented_collisions(
            self):
        for entries in (self.specs, self.adrs):
            by_repo_number = {}
            for entry in entries:
                key = (entry["repository"], entry["number"])
                by_repo_number.setdefault(key, []).append(entry)
            for (repo, number), group in by_repo_number.items():
                if len(group) == 1:
                    continue
                notes = " ".join(
                    str(g.get("notes", "")) for g in group).lower()
                self.assertIn(
                    "collision", notes,
                    f"undocumented within-repository duplicate "
                    f"{repo} #{number}; document the collision or "
                    "allocate a fresh number")

    def test_gateway_local_series_is_grandfathered(self):
        # The gateway-local ADR series uses 4-digit numbers recorded
        # with their repository; they must not collide with the
        # project-wide 3-digit series.
        gateway_adrs = [
            entry for entry in self.adrs
            if entry["repository"] == "retriva-gateway"]
        self.assertGreaterEqual(len(gateway_adrs), 2)
        for entry in gateway_adrs:
            self.assertRegex(
                str(entry["number"]), r"^0\d{3}$|^0{0,2}\d{1,3}$")

    def test_new_artifacts_are_registered(self):
        # Spec 024 and ADR-029 must be present and accepted (the
        # governed change that created this registry).
        spec_024 = [e for e in self.specs
                   if e["number"] == "024"]
        self.assertEqual(len(spec_024), 1)
        self.assertEqual(spec_024[0]["status"], "accepted")
        self.assertEqual(spec_024[0]["repository"], "retriva-core")
        self.assertTrue(
            (WORKSPACE / spec_024[0]["repository"]
             / spec_024[0]["path"]).exists())

        adr_029 = [e for e in self.adrs if e["number"] == "029"]
        self.assertEqual(len(adr_029), 1)
        self.assertEqual(adr_029[0]["status"], "accepted")
        self.assertEqual(adr_029[0]["repository"],
                         "retriva-crm-assistant")


if __name__ == "__main__":
    unittest.main()
