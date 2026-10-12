"""Phrase migration / 词组迁移: legacy content never aliases ECDICT tables."""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.modules.phrases.schema import MIGRATIONS, _archive_legacy_verdicts, _rename_legacy_inventory


class PhraseMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(':memory:')
        self.addCleanup(self.conn.close)
        self.conn.execute("ATTACH DATABASE ':memory:' AS dict")
        self.conn.executescript("""
            CREATE TABLE dict.phrases (id INTEGER PRIMARY KEY, phrase TEXT);
            INSERT INTO dict.phrases VALUES (17,'take into account'),(29,'in addition');
        """)

    def dictionary_rows(self):
        return self.conn.execute('SELECT * FROM dict.phrases ORDER BY id').fetchall()

    def test_fresh_inventory_does_not_rename_the_attached_dictionary(self) -> None:
        self.conn.executescript(next(m.apply for m in MIGRATIONS if m.version == 1))
        _archive_legacy_verdicts(self.conn)
        _rename_legacy_inventory(self.conn)
        self.assertEqual(self.dictionary_rows(), [(17, 'take into account'), (29, 'in addition')])
        self.conn.execute("INSERT INTO main.phrase_list (id,text,source,source_key,tier,created_at)"
                          " VALUES (41,'as well as','collins','well','A','2026-10-10T00:00:00+00:00')")
        self.assertEqual(self.conn.execute('SELECT id,text FROM main.phrase_list').fetchall(),
                         [(41, 'as well as')])
        self.assertEqual(self.dictionary_rows(), [(17, 'take into account'), (29, 'in addition')])

    def test_legacy_inventory_keeps_content_identity_and_dictionary(self) -> None:
        self.conn.executescript("CREATE TABLE main.phrases (id INTEGER PRIMARY KEY, text TEXT);"
                                "INSERT INTO main.phrases VALUES (53,'in addition');")
        _rename_legacy_inventory(self.conn)
        _rename_legacy_inventory(self.conn)
        self.assertEqual(self.conn.execute('SELECT * FROM main.phrase_list').fetchall(),
                         [(53, 'in addition')])
        self.assertEqual(self.dictionary_rows(), [(17, 'take into account'), (29, 'in addition')])

    def test_mixed_layout_is_rejected_without_rewriting_either_inventory(self) -> None:
        self.conn.executescript("""
            CREATE TABLE main.phrases (id INTEGER PRIMARY KEY, text TEXT);
            CREATE TABLE main.phrase_list (id INTEGER PRIMARY KEY, text TEXT);
            INSERT INTO main.phrases VALUES (53,'in addition');
            INSERT INTO main.phrase_list VALUES (61,'rather than');
        """)
        with self.assertRaisesRegex(RuntimeError, '冲突'):
            _rename_legacy_inventory(self.conn)
        self.assertEqual(self.conn.execute('SELECT * FROM main.phrases').fetchall(), [(53, 'in addition')])
        self.assertEqual(self.conn.execute('SELECT * FROM main.phrase_list').fetchall(), [(61, 'rather than')])
        self.assertEqual(self.dictionary_rows(), [(17, 'take into account'), (29, 'in addition')])

    def test_legacy_verdicts_survive_source_rebuild_and_repeat_migration(self) -> None:
        self.conn.executescript("""
            CREATE TABLE main.reading_phrases (
                id INTEGER,article_id INTEGER,sentence_id INTEGER,phrase TEXT,
                start_seq INTEGER,end_seq INTEGER,surface TEXT,verdict INTEGER,
                judged_at TEXT,created_at TEXT);
            INSERT INTO main.reading_phrases VALUES (7,553,11632,'in addition',2,3,
                'in addition',1,'2026-10-10T00:00:00+00:00','2026-10-10T00:00:00+00:00');
        """)
        _archive_legacy_verdicts(self.conn)
        self.conn.execute('DROP TABLE main.reading_phrases')
        _archive_legacy_verdicts(self.conn)
        self.assertEqual(self.conn.execute('SELECT id,phrase,verdict FROM main.phrase_verdicts_legacy').fetchall(),
                         [(7, 'in addition', 1)])
        self.assertEqual(self.dictionary_rows(), [(17, 'take into account'), (29, 'in addition')])


if __name__ == '__main__':
    unittest.main()
