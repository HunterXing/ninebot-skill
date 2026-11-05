#!/usr/bin/env python3
"""Tests for the public sample board.

The README picture is rendered from a fabricated fixture and lives in a
public repository. Two failure modes matter, and neither shows up in a
screenshot:

* the fixture drifts toward real data — a place name, a coordinate, the
  owner's actual riding distances;
* the renderer reaches past the fixture and picks up real data anyway —
  the seven-day histogram falls back to the local ride-record cache
  whenever the snapshot has no records, which is exactly how real rides
  got into the first version of this picture.

So the fixture is checked for placeholders, and the generator is checked
for the things that would let real data in.
"""
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import make_sample_board as sample  # noqa: E402


class SampleBoardPrivacyTest(unittest.TestCase):
    """The sample must carry nothing but invented data."""

    def test_the_fixture_has_no_real_place_names(self):
        geo = sample._sample()["geo"]
        for field in ("poi", "street", "district", "city", "province",
                      "township", "address", "formattedAddress", "short"):
            value = str(geo.get(field) or "")
            self.assertTrue(
                not value or value.startswith("某"),
                f"geo.{field} = {value!r} is not a placeholder")

    def test_the_fixture_has_no_real_coordinates(self):
        snap = sample._sample()
        pairs = [(snap["status"].get("lat"), snap["status"].get("lon")),
                 (snap["geo"]["coords"].get("lat"),
                  snap["geo"]["coords"].get("lon"))]
        for lat, lon in pairs:
            self.assertEqual(0.0, float(lat))
            self.assertEqual(0.0, float(lon))

    def test_the_fixture_carries_no_serial_number(self):
        self.assertEqual("", sample._sample()["device"]["sn"])

    def test_the_sample_is_labelled_as_sample_data(self):
        """A picture that travels to strangers must say it is invented."""
        page = sample._mark_as_sample("<html><body><div class=\"title\">x</div>"
                                       "</body></html>")
        self.assertIn("示例数据", page)

    def test_no_real_ride_distances_leak_into_the_fixture(self):
        """The owner's totals are 15.4 / 8.6 / 2.4 km. The sample must
        not reuse them, or the picture reads as a real record."""
        mileage = sample._sample()["mileage"]
        for section in ("day", "week", "month", "year", "all"):
            km = float(mileage[section]["km"])
            self.assertNotIn(km, (15.4, 8.6, 2.4),
                             f"mileage.{section} reuses a real figure")


class SampleBoardLeakTest(unittest.TestCase):
    """The generator must not reach for real data behind the fixture."""

    @staticmethod
    def _read(relpath: str) -> str:
        with open(os.path.join(ROOT, relpath), encoding="utf-8") as fh:
            return fh.read()

    def test_the_ride_cache_is_kept_out_of_the_picture(self):
        """The histogram reads the local ride-record cache when the
        snapshot has none. Pointing it at the real cache is how real
        rides ended up in the README picture."""
        source = self._read("scripts/make_sample_board.py")
        self.assertIn("NINEBOT_TRAVEL_CACHE", source)
        self.assertRegex(source, r"NINEBOT_TRAVEL_CACHE\"?\]?\s*=\s*"
                                 r"os\.path\.join\(\s*ROOT")
        # The default must not be the real cache path.
        self.assertNotIn("ninebot_travel_months.json", source)

    def test_the_signed_picture_url_is_never_written_to_the_repo(self):
        """The picture URL carries a signature that rotates; it is
        runtime data, not a repository asset."""
        source = self._read("scripts/make_sample_board.py")
        self.assertNotIn("nbchecksignv1", source)
        self.assertNotRegex(source, r"oms-oss-public\.ninebot\.com")

    def test_the_readme_points_at_the_sample_picture(self):
        readme = self._read("README.md")
        self.assertIn("docs/images/board-sample.png", readme)
        self.assertTrue(os.path.exists(
            os.path.join(ROOT, "docs", "images", "board-sample.png")))

    def test_the_committed_picture_is_not_the_live_one(self):
        """The live board renders the owner's address; the committed
        picture must differ from it."""
        live = os.path.expanduser(
            os.environ.get("NINEBOT_SNAPSHOT",
                           "~/.hermes/state/ninebot/snapshot.json"))
        if not os.path.exists(live):
            self.skipTest("no live snapshot on this machine")
        import json
        with open(live, encoding="utf-8") as fh:
            snap = json.load(fh)
        real = (snap.get("geo") or {}).get("poi") or ""
        if not real:
            self.skipTest("live snapshot has no POI")
        import ninebot_card_image as board
        page = board.build_html(snap, None)
        sample_page = board.build_html(sample._sample(), None)
        self.assertIn(real, page)
        self.assertNotIn(real, sample_page)


class RepoPrivacyGuardTest(unittest.TestCase):
    """The whole repository is public. Nothing identifying may land in it.

    The sample-board fixture has its own checks, but those only cover the
    picture. A test file that hard-codes the vehicle serial or the VIN is
    just as much a leak, and it is invisible in a screenshot — so scan
    the source tree instead of trusting review.
    """

    # Patterns are built from pieces so this file does not itself match.
    PATTERNS = [
        ("vehicle serial", r"26DAA" + r"[0-9A-F]{9}"),
        ("VIN / identification number", r"\b38812" + r"2604056296\b"),
        ("owner phone", r"\b15555" + r"306200\b"),
        ("owner nickname", "行走大师" + "0930"),
    ]

    SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "docs", "node_modules"}
    SCAN_SUFFIXES = (".py", ".md", ".json", ".txt", ".yml", ".yaml", ".sh")

    def _repo_files(self):
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if d not in self.SKIP_DIRS]
            for name in filenames:
                if name.endswith(self.SCAN_SUFFIXES):
                    yield os.path.join(dirpath, name)

    def test_no_live_identifier_is_committed(self):
        offenders = []
        for path in self._repo_files():
            rel = os.path.relpath(path, ROOT)
            if rel == os.path.relpath(__file__, ROOT):
                continue
            try:
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
            except (OSError, UnicodeDecodeError):
                continue
            for label, pattern in self.PATTERNS:
                if re.search(pattern, text):
                    offenders.append(f"{rel}: {label}")
        self.assertEqual([], offenders,
                         "live identifiers committed to a public repo")


if __name__ == "__main__":
    unittest.main()
