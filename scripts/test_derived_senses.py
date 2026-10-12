"""Derived-sense behavior tests; all databases live in a disposable directory.

Run: uv run python scripts/test_derived_senses.py
The real MDX files are reference inputs, never application database snapshots.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.modules.senses import derived, repository
from scripts.import_collins import load_dictionary
from scripts.import_derived_senses import setup


class DerivedSenses(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="p13-derived-")
        cls.previous = {k: os.environ.get(k) for k in ("ER_DATA_DIR", "ER_ADMIN_SECRET")}
        os.environ["ER_ADMIN_SECRET"] = "isolated-derived-test"
        from backend.core.config import get_settings
        get_settings.cache_clear()
        setup(Path(cls.temp.name))
        cls.html12 = load_dictionary(Path("data/dictionaries/collins-cobuild-2012.mdx"))["severe"]
        cls.html17 = load_dictionary(Path("data/dictionaries/collins-youdao-2017.mdx"))["severe"]
        cls.candidates, cls.issues = derived.scan_entry("severe", cls.html12, cls.html17, {"severely"})
        from backend.core.db import get_connection
        cls.conn = get_connection("content")
        cls.conn.execute("CREATE TABLE reading_tokens (id INTEGER PRIMARY KEY, headword TEXT,"
                         " kind TEXT, sense_id INTEGER, sense_ordinal INTEGER, position INTEGER)")
        cls.conn.commit()

    @classmethod
    def tearDownClass(cls):
        from backend.core.db import close_connections
        from backend.core.config import get_settings
        close_connections()
        get_settings.cache_clear()
        cls.temp.cleanup()
        for key, value in cls.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def setUp(self):
        for table in ("derived_sense_sources", "sense_examples", "senses", "senses_retired",
                      "reading_tokens", "word_families"):
            self.conn.execute(f"DELETE FROM {table}")
        self.conn.commit()

    def store(self, candidate, concept="in a way that causes great harm or damage"):
        return repository.store_derived_sense(candidate.payload(), concept, provider="fixture-provider",
                                              model="fixture-model", prompt="fixture evidence-bound prompt",
                                              response=json.dumps({"concept_en": concept}))

    def test_real_severely_boundaries_and_fields(self):
        self.assertEqual(self.issues, [])
        self.assertEqual(len(self.candidates), 2)
        self.assertEqual([c.parent_block for c in self.candidates], [0, 1])
        self.assertEqual([c.runon_2017["gloss_zh"] for c in self.candidates], ["严重地", "严厉地"])
        self.assertTrue(all(c.runon_2017["pos"] == "ADV" for c in self.candidates))
        self.assertTrue(all(c.runon_2017["pos_zh"] == "副词" for c in self.candidates))
        self.assertNotEqual(self.candidates[0].source_key, self.candidates[1].source_key)

    def test_numbers_are_not_cross_edition_identity(self):
        changed = self.html17.replace('<span class="collinsOrder">1</span>',
                                      '<span class="collinsOrder">99</span>')
        found, issues = derived.scan_entry("severe", self.html12, changed, {"severely"})
        self.assertEqual(issues, [])
        self.assertEqual([c.source_key for c in found], [c.source_key for c in self.candidates])

    def test_missing_and_ambiguous_fields_are_reported(self):
        _, missing = derived.scan_entry("severe", self.html12, None, {"severely"})
        self.assertEqual({i["reason"] for i in missing}, {"missing_2017_entry"})
        tree = derived.Tree(self.html17)
        listings = [n for n in tree.root.walk() if n.has("ol")]
        listing = listings[0]
        runon = [n for n in listing.children if isinstance(n, derived.Node) and n.tag == "li"][1]
        listing.children.insert(listing.children.index(runon), copy.deepcopy(runon))
        found, issues = derived.scan_entry("severe", self.html12, tree.root.html(), {"severely"})
        self.assertEqual(len(found), 1)
        self.assertEqual(issues[0]["reason"], "ambiguous_2017_match")
        changed = self.html17.replace("严重地", "")
        _, issues = derived.scan_entry("severe", self.html12, changed, {"severely"})
        self.assertIn("missing_2017_fields", {i["reason"] for i in issues})

    def test_repeat_and_partial_resume_keep_ids_and_definition(self):
        repository.store_dictionary_senses("severe", [{
            "source_block": c.parent_block,
            "concept_en": c.parent_2012["concept_en"],
            "gloss_zh": c.parent_2012["gloss_zh"],
        } for c in self.candidates], source_dict="collins-cobuild-2012")
        parent_ids = {s["id"] for s in repository.senses_of("severe")}
        first = self.store(self.candidates[0])
        self.assertEqual(first, self.store(self.candidates[0], "a different definition must not overwrite"))
        second = self.store(self.candidates[1], "in a very harsh or strict way")
        self.assertNotEqual(first, second)
        self.assertTrue(parent_ids.isdisjoint({first, second}))
        self.assertEqual(first, repository.derived_sense_for(self.candidates[0].payload())["sense_id"])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM senses WHERE headword = 'severely'").fetchone()[0], 2)
        self.assertEqual(self.conn.execute("SELECT concept_en FROM senses WHERE id=?", (first,)).fetchone()[0],
                         "in a way that causes great harm or damage")
        row = repository.derived_sense(self.candidates[0].source_key)
        self.assertEqual(row["definition_provider"], "fixture-provider")
        provenance = json.loads(row["provenance"])
        self.assertEqual(provenance["parent_2012"]["block_index"], 0)
        self.assertEqual(provenance["runon_2017"]["gloss_zh"], "严重地")
        changed = self.candidates[0].payload()
        changed["runon_2017"]["gloss_zh"] = "不同来源"
        with self.assertRaises(ValueError):
            repository.derived_sense_for(changed)

    def test_existing_primary_inventory_is_not_overwritten(self):
        repository.store_dictionary_senses("severely", [{"source_block": 0,
            "concept_en": "a directly defined existing meaning", "gloss_zh": "既有释义"}], source_dict="original")
        existing = repository.senses_of("severely")[0]["id"]
        with self.assertRaises(ValueError):
            self.store(self.candidates[0])
        self.assertEqual(repository.senses_of("severely")[0]["id"], existing)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM derived_sense_sources").fetchone()[0], 0)

    def test_dictionary_sources_do_not_collide_ordinals(self):
        a = {"source_block": 0, "concept_en": "the first separately sourced meaning", "gloss_zh": "一"}
        b = {"source_block": 0, "concept_en": "another separately sourced meaning", "gloss_zh": "二"}
        repository.store_dictionary_senses("testword", [a], source_dict="source-a")
        repository.store_dictionary_senses("testword", [b], source_dict="source-b")
        before = [(s["id"], s["ordinal"]) for s in repository.senses_of("testword")]
        repository.store_dictionary_senses("testword", [a], source_dict="source-a")
        after = [(s["id"], s["ordinal"]) for s in repository.senses_of("testword")]
        self.assertEqual(before, after)
        self.assertEqual(len({ordinal for _, ordinal in after}), 2)

    def test_reopen_only_derived_content_zeros(self):
        sense_id = self.store(self.candidates[0])
        rows = [(1, "severely", "content", 0, 0, 5),
                (2, "severely", "content", -1, None, 12),
                (3, "severely", "content", sense_id, 1, 20),
                (4, "severe", "content", 0, 0, 25),
                (5, "severely", "proper", 0, 0, 30)]
        self.conn.executemany("INSERT INTO reading_tokens VALUES (?,?,?,?,?,?)", rows)
        self.conn.commit()
        self.assertEqual(repository.reopen_derived_zero_tokens(["severely", "severe"]), [1])
        after = [tuple(r) for r in self.conn.execute("SELECT * FROM reading_tokens ORDER BY id")]
        self.assertEqual(after[0], (1, "severely", "content", None, None, 5))
        self.assertEqual(after[1:], rows[1:])
        self.assertEqual(repository.reopen_derived_zero_tokens(["severely"]), [])

    def test_empty_corpus_does_not_replay_legacy_cutovers(self):
        from types import SimpleNamespace
        from scripts.import_derived_senses import reannotation
        self.conn.execute("ALTER TABLE reading_tokens RENAME TO token_fixture")
        self.conn.commit()
        before = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        try:
            result = reannotation(SimpleNamespace(word=[], resume=None, provider=None, wait_timeout=5))
            self.assertEqual(result, 0)
            after = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual(before, after)
        finally:
            self.conn.execute("ALTER TABLE token_fixture RENAME TO reading_tokens")
            self.conn.commit()

    def test_incomplete_corpus_reports_initialization_prerequisite(self):
        from types import SimpleNamespace
        from scripts.import_derived_senses import reannotation
        with self.assertRaisesRegex(ValueError, "迁移"):
            reannotation(SimpleNamespace(word=[], resume=None, provider=None, wait_timeout=5))

    def test_dictionary_confirmation_preserves_target_grading(self):
        from backend.modules.wordfamily import repository as families
        for source, grade in (("rule", "A"), ("llm", "C")):
            with self.subTest(source=source, grade=grade):
                self.conn.execute("DELETE FROM word_families")
                self.conn.execute(
                    "INSERT INTO word_families (member,root,affix,grade,source)"
                    " VALUES ('severely','severe','-ly',?,?)", (grade, source))
                self.conn.commit()
                families.clear_caches()
                families.store_dictionary_family(
                    "severely", "severe", "-ly", breakdown_zh="词典确认的派生关系")
                self.assertEqual(families.family_of("severely")["grade"], grade)
                self.assertEqual(families.transparent_root("severely"),
                                 "severe" if grade == "A" else None)

    def test_formation_is_dictionary_relationship_not_chinese_wordplay(self):
        from backend.modules.senses.derived_worker import formation
        relation = formation(self.candidates[0].payload())
        self.assertEqual((relation["root"], relation["affix"]), ("severe", "-ly"))
        self.assertIn("独立义项", relation["breakdown_zh"])
        found, issues = derived.scan_entry("drought", load_dictionary(
            Path("data/dictionaries/collins-cobuild-2012.mdx"))["drought"], None, {"drought"})
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
