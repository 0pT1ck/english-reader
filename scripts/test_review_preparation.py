"""Bilingual preparation / 双语内容准备 regressions, with in-memory content only.

Run with `uv run python scripts/test_review_preparation.py`. No server, model,
real database or notification channel is used.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from contextlib import ExitStack, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.modules.llm import jobs, providers
from backend.modules.llm.parsing import ItemFailed
from backend.modules.llm.schema import MIGRATIONS as LLM_MIGRATIONS
from backend.modules.review import preparation, sentences, translate
from backend.modules.review.schema import MIGRATIONS as REVIEW_MIGRATIONS


class InlinePool:
    """Keep runner ordering deterministic without starting test threads."""

    def __init__(self, **kwargs: object) -> None:
        pass

    def __enter__(self) -> InlinePool:
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def map(self, fn: object, values: object) -> object:
        return map(fn, values)


class PreparationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.executescript("""
            CREATE TABLE review_sentences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_type TEXT NOT NULL DEFAULT 'word', item_key TEXT NOT NULL,
                sense_id INTEGER NOT NULL, text TEXT NOT NULL,
                blank_start INTEGER NOT NULL, blank_end INTEGER NOT NULL,
                surface TEXT NOT NULL, source TEXT NOT NULL,
                article_id INTEGER, sentence_id INTEGER, model TEXT,
                created_at TEXT NOT NULL, text_zh TEXT, zh_start INTEGER, zh_end INTEGER,
                UNIQUE(item_type,item_key,sense_id,text)
            );
            CREATE TABLE learner_pool (
                learner_id INTEGER, item_type TEXT, item_key TEXT,
                sense_id INTEGER, pool TEXT
            );
            CREATE TABLE reading_sentences (
                id INTEGER PRIMARY KEY, text TEXT, char_start INTEGER
            );
            CREATE TABLE reading_tokens (
                surface TEXT, char_start INTEGER, char_end INTEGER,
                article_id INTEGER, sentence_id INTEGER,
                sense_id INTEGER, in_phrase INTEGER, headword TEXT
            );
        """)
        self.conn.executescript(LLM_MIGRATIONS[0].apply)
        self.conn.executescript(next(m.apply for m in LLM_MIGRATIONS if m.version == 3))
        self.conn.executescript(next(m.apply for m in REVIEW_MIGRATIONS if m.version == 5))
        self.stack = self.enterContext(ExitStack())
        for module in (sentences, preparation, translate, jobs):
            self.stack.enter_context(patch.object(module, "get_connection", return_value=self.conn))
        self.stack.enter_context(patch.object(jobs, "log", Mock()))
        self.stack.enter_context(patch.dict(jobs._running, clear=True))
        self.stack.enter_context(patch.object(jobs, "trace", side_effect=lambda: nullcontext("test")))
        self.stack.enter_context(patch.object(jobs.events, "emit", Mock()))
        self.stack.enter_context(patch.object(jobs, "ThreadPoolExecutor", InlinePool))
        self.stack.enter_context(patch.dict(jobs._workers, {
            sentences.KIND: sentences.WORKER, translate.KIND: translate.WORKER,
        }))
        self.settings = {
            "review_pool_target": 2, "review_generate_batch": 3,
            "review_translate_batch": 2, "review_translate_provider": "translate-test",
            "review_gen_provider": "gen-test", "llm_spend_cap": 0,
            "review_clock_offset_days": 0,
            "llm_concurrency": 1, "llm_max_attempts": 3,
        }
        self.stack.enter_context(patch.object(sentences.runtime_config, "get",
                                             side_effect=lambda key: self.settings[key]))
        self.stack.enter_context(patch.object(jobs, "configured_provider", return_value=None))
        self.stack.enter_context(patch.object(jobs, "configured_thinking", return_value=None))
        self.gen = providers.Provider("gen-test", "Generation", "openai", "https://example.invalid",
                                      "generation-model", 1, 2, "CNY", True, True)
        self.trans = providers.Provider("translate-test", "Translation", "openai", "https://example.invalid",
                                        "translation-model", 3, 4, "CNY", True, False)
        self.stack.enter_context(patch.object(providers, "get", side_effect=lambda key: {
            self.gen.id: self.gen, self.trans.id: self.trans,
        }[key]))
        self.stack.enter_context(patch.object(providers, "default_provider", return_value=self.gen))
        self.real_start = jobs.start
        self.start = self.stack.enter_context(patch.object(jobs, "start"))
        self.complete = self.stack.enter_context(patch.object(sentences.client, "complete"))
        self.payload = {"item_type": "word", "item_key": "bank", "sense_id": 1}

    def add_sentence(self, seq: int, *, key: str = "bank", sense: int = 1,
                     zh: str | None = None, source: str = "generated") -> int:
        text = f"The {key} can provide useful services for local people today {seq}."
        start = text.index(key)
        cursor = self.conn.execute(
            "INSERT INTO review_sentences (item_type,item_key,sense_id,text,blank_start,blank_end,"
            " surface,source,created_at,text_zh) VALUES ('word',?,?,?,?,?,?,?,'now',?)",
            (key, sense, text, start, start + len(key), key, source, zh),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def completion(self, text: str, *, model: str = "translation-model") -> SimpleNamespace:
        return SimpleNamespace(text=text, model=model, tokens_in=10, tokens_out=20)

    def request(self) -> int | None:
        return sentences.start_for([self.payload], provider_id=self.gen.id)

    def test_existing_bilingual_corpus_satisfies_target_without_model(self) -> None:
        ids = [self.add_sentence(i, zh="银行为社区提供服务。", source="corpus") for i in range(2)]
        self.assertIsNone(self.request())
        self.assertEqual(preparation.status("word", "bank", 1)["preparation_state"], "ready")
        outcome = sentences._run(self.gen, self.payload, {})
        self.assertEqual(json.loads(outcome.result)["bilingual_stock"], 2)
        self.complete.assert_not_called()
        self.assertEqual([r[0] for r in self.conn.execute("SELECT id FROM review_sentences")], ids)

    def test_only_english_or_invalid_blank_is_not_ready(self) -> None:
        self.add_sentence(1)
        invalid = self.add_sentence(2, zh="银行提供服务。")
        self.conn.execute("UPDATE review_sentences SET blank_end=999 WHERE id=?", (invalid,))
        self.assertEqual(sentences.bilingual_count("bank", 1), 0)
        self.assertEqual(preparation.status("word", "bank", 1)["preparation_state"], "pending")

    def test_harvest_then_translate_only_the_requested_identity(self) -> None:
        unrelated = self.add_sentence(1, key="bridge", sense=2)
        for seq in range(1, 3):
            text = f"The bank offers useful services for local people in district {seq}."
            self.conn.execute("INSERT INTO reading_sentences VALUES (?,?,0)", (seq, text))
            self.conn.execute("INSERT INTO reading_tokens VALUES ('bank',4,8,1,?,1,0,'bank')", (seq,))
        self.conn.commit()
        self.complete.return_value = self.completion('{"#1":"【银行】提供服务。","2":"【银行】服务居民。"}')
        outcome = sentences._run(self.gen, self.payload, {})
        self.assertEqual(json.loads(outcome.result)["bilingual_stock"], 2)
        self.assertEqual(self.complete.call_count, 1)
        prompt = self.complete.call_args.args[1][1]["content"]
        self.assertNotIn("bridge", prompt)
        self.assertEqual(self.complete.call_args.args[0], self.trans)
        self.assertEqual(outcome.cost, self.trans.cost_of(10, 20))
        self.assertIsNone(self.conn.execute("SELECT text_zh FROM review_sentences WHERE id=?", (unrelated,)).fetchone()[0])

    def test_partial_translation_keeps_progress_and_can_resume(self) -> None:
        ids = [self.add_sentence(i) for i in range(2)]
        self.complete.return_value = self.completion('{"1":"【银行】提供服务。","2":"English only"}')
        with self.assertRaises(ItemFailed):
            sentences._run(self.gen, self.payload, {})
        self.assertEqual(preparation.status("word", "bank", 1)["preparation_state"], "failed")
        self.assertEqual(sentences.bilingual_count("bank", 1), 1)
        self.complete.return_value = self.completion('{"1":"【银行】服务居民。"}')
        sentences._run(self.gen, self.payload, {})
        self.assertEqual(sentences.bilingual_count("bank", 1), 2)
        self.assertEqual([r[0] for r in self.conn.execute("SELECT id FROM review_sentences")], ids)

    def test_provider_failure_is_visible_without_credentials(self) -> None:
        self.add_sentence(1)
        self.complete.side_effect = RuntimeError("Authorization: Bearer hidden-secret")
        with self.assertRaises(jobs.ItemFailure):
            sentences._run(self.gen, self.payload, {})
        status = preparation.status("word", "bank", 1)
        self.assertEqual(status["preparation_state"], "failed")
        self.assertIn("翻译", status["preparation_error"])
        self.assertNotIn("hidden-secret", status["preparation_error"])

    def test_filtered_generation_shortage_is_failed_not_done(self) -> None:
        sense = {"gloss_zh": '["银行"]', "pos": "N-COUNT", "concept_en": "A financial institution"}
        self.complete.side_effect = [
            self.completion("The bank can provide useful services for local people today.\n"
                            "The bank bad sentence is rejected by the checker.", model="generation-model"),
            self.completion('{"1":"【银行】提供服务。"}'),
        ]
        with patch.object(sentences.senses, "sense_by_id", return_value=sense), \
             patch.object(sentences, "validate", side_effect=[(True, ""), (False, "rejected")]), \
             patch.object(sentences, "locate", return_value=(4, 8, "bank")):
            with self.assertRaises(jobs.ItemFailure) as failed:
                sentences._run(self.gen, self.payload, {})
        self.assertEqual(self.complete.call_count, 2)
        self.assertEqual(sentences.bilingual_count("bank", 1), 1)
        self.assertEqual(preparation.status("word", "bank", 1)["preparation_state"], "failed")
        self.assertEqual(failed.exception.outcome.cost,
                         self.gen.cost_of(10, 20) + self.trans.cost_of(10, 20))

    def test_generation_and_translation_complete_with_separate_provider_costs(self) -> None:
        sense = {"gloss_zh": '["银行"]', "pos": "N-COUNT", "concept_en": "A financial institution"}
        self.complete.side_effect = [
            self.completion("The bank can provide useful services for local people today.\n"
                            "The bank offers useful accounts for local families every day.", model="generation-model"),
            self.completion('{"1":"【银行】提供服务。","2":"【银行】服务居民。"}'),
        ]
        with patch.object(sentences.senses, "sense_by_id", return_value=sense), \
             patch.object(sentences, "validate", return_value=(True, "")), \
             patch.object(sentences, "locate", return_value=(4, 8, "bank")):
            outcome = sentences._run(self.gen, self.payload, {})
        self.assertEqual(preparation.status("word", "bank", 1)["preparation_state"], "ready")
        self.assertEqual([s["provider"] for s in json.loads(outcome.result)["stages"]],
                         [self.gen.id, self.trans.id])
        self.assertEqual(outcome.cost, self.gen.cost_of(10, 20) + self.trans.cost_of(10, 20))
        self.assertEqual((outcome.tokens_in, outcome.tokens_out), (20, 40))
        self.assertEqual({r[0] for r in self.conn.execute("SELECT model FROM review_sentences")},
                         {"generation-model"})

    def test_duplicate_snapshot_claims_once_but_new_identity_is_not_dropped(self) -> None:
        self.conn.execute("INSERT INTO learner_pool VALUES (1,'word','bank',1,'reviewing')")
        first = sentences.start_for(provider_id=self.gen.id)
        self.assertIsNotNone(first)
        self.assertIsNone(sentences.start_for(provider_id=self.gen.id))
        self.conn.execute("INSERT INTO learner_pool VALUES (1,'word','bridge',2,'reviewing')")
        second = sentences.start_for(provider_id=self.gen.id)
        self.assertNotEqual(first, second)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM llm_job_items").fetchone()[0], 2)
        self.assertEqual(self.start.call_count, 2)

    def test_graduated_snapshot_does_not_request_new_content(self) -> None:
        self.conn.execute("INSERT INTO learner_pool VALUES (1,'word','bank',1,'graduated')")
        self.assertIsNone(sentences.start_for(provider_id=self.gen.id))
        self.complete.assert_not_called()

    def test_paused_and_capped_jobs_are_failed_and_do_not_duplicate(self) -> None:
        job_id = self.request()
        for job_state in ("paused", "capped"):
            with self.subTest(job_state=job_state):
                self.conn.execute("UPDATE llm_jobs SET status=?,error='secret' WHERE id=?", (job_state, job_id))
                status = preparation.status("word", "bank", 1)
                self.assertEqual(status["preparation_state"], "failed")
                self.assertNotIn("secret", status["preparation_error"])
                self.assertIsNone(self.request())
        for i in range(2):
            self.add_sentence(i, zh="银行提供服务。")
        self.assertEqual(preparation.status("word", "bank", 1),
                         {"preparation_state": "ready", "preparation_error": None})

    def test_restart_rebinds_and_resumes_interrupted_work_not_explicit_pause(self) -> None:
        job_id = self.request()
        self.start.reset_mock()
        self.conn.execute("DELETE FROM review_preparations")
        preparation.resume_interrupted()
        self.start.assert_called_once_with(job_id)
        self.assertIsNone(self.request())
        self.start.reset_mock()
        self.conn.execute("UPDATE llm_jobs SET status='paused' WHERE id=?", (job_id,))
        preparation.set_state("word", "bank", 1, "running")
        preparation.resume_interrupted()
        self.start.assert_called_once_with(job_id)
        self.start.reset_mock()
        preparation.on_job_finished({"kind": sentences.KIND, "job_id": job_id, "status": "paused"})
        preparation.resume_interrupted()
        self.start.assert_not_called()

    def test_failed_reply_usage_survives_retries_and_enforces_spend_cap(self) -> None:
        for i in range(2):
            self.add_sentence(i)
        self.settings["review_translate_batch"] = 1
        job_id = self.request()
        paid = self.trans.cost_of(10, 20)
        self.conn.execute("UPDATE llm_jobs SET spend_cap=? WHERE id=?", (paid * 1.5, job_id))
        self.complete.side_effect = [
            self.completion('{"1":"【银行】提供服务。"}'),
            self.completion("not a JSON reply"),
        ]
        jobs._run(job_id)
        job = jobs.get(job_id)
        self.assertEqual(job["status"], "paused")
        self.assertAlmostEqual(job["cost"], paid * 2)
        self.assertEqual((job["tokens_in"], job["tokens_out"]), (20, 40))
        self.assertEqual(sentences.bilingual_count("bank", 1), 1)
        jobs._run(job_id)
        self.assertEqual(jobs.get(job_id)["status"], "capped")
        self.assertEqual(self.complete.call_count, 2)
        self.conn.execute("UPDATE llm_jobs SET spend_cap=0 WHERE id=?", (job_id,))
        self.complete.side_effect = None
        self.complete.return_value = self.completion('{"1":"【银行】服务居民。"}')
        jobs._run(job_id)
        job = jobs.get(job_id)
        self.assertEqual(job["status"], "done")
        self.assertAlmostEqual(job["cost"], paid * 3)
        self.assertEqual((job["tokens_in"], job["tokens_out"]), (30, 60))
        self.assertEqual((job["done"], job["failed"]), (1, 0))
        self.assertEqual(jobs.items(job_id)[0]["attempts"], 2)

    def test_explicit_pause_is_durable_before_any_finished_event(self) -> None:
        job_id = self.request()
        self.start.reset_mock()
        jobs.pause(job_id)
        self.assertEqual(jobs.get(job_id)["pause_requested"], 1)
        preparation.resume_interrupted()
        self.start.assert_not_called()
        self.assertEqual(preparation.status("word", "bank", 1)["preparation_state"], "failed")
        with patch.object(jobs.threading, "Thread", return_value=Mock()):
            self.real_start(job_id)
        self.assertEqual(jobs.get(job_id)["pause_requested"], 0)
        self.assertEqual(jobs.get(job_id)["status"], "pending")
        self.assertEqual(preparation.status("word", "bank", 1)["preparation_state"], "pending")
        self.conn.execute("UPDATE llm_jobs SET status='running' WHERE id=?", (job_id,))
        jobs.pause(job_id)
        self.assertTrue(jobs._running[job_id].stop.is_set())
        preparation.resume_interrupted()
        self.start.assert_not_called()

    def test_shared_retry_queued_for_second_learner_remains_pollable(self) -> None:
        self.conn.executemany("INSERT INTO learner_pool VALUES (?,?,?,?,?)", [
            (1, "word", "bank", 1, "reviewing"),
            (1, "word", "bridge", 2, "reviewing"),
            (2, "word", "bridge", 2, "reviewing"),
        ])
        job_id = sentences.start_for(learner_id=1, provider_id=self.gen.id)
        self.conn.execute("UPDATE llm_jobs SET status='paused' WHERE id=?", (job_id,))
        self.conn.execute("UPDATE llm_job_items SET status='failed',attempts=1 WHERE job_id=?", (job_id,))
        preparation.set_state("word", "bank", 1, "failed", "旧失败")
        preparation.set_state("word", "bridge", 2, "failed", "旧失败")
        for key, sense in (("bank", 1), ("bridge", 2)):
            for i in range(2):
                self.add_sentence(i, key=key, sense=sense)
        with patch.object(jobs.threading, "Thread", return_value=Mock()):
            self.real_start(job_id)
        self.assertEqual(preparation.status("word", "bridge", 2),
                         {"preparation_state": "pending", "preparation_error": None})
        self.assertIsNone(sentences.start_for(learner_id=2, provider_id=self.gen.id))

        def reply(provider: object, messages: list[dict[str, str]], **kwargs: object) -> SimpleNamespace:
            prompt = messages[1]["content"]
            if "目标词：bank" in prompt:
                self.assertEqual(preparation.status("word", "bridge", 2),
                                 {"preparation_state": "running", "preparation_error": None})
                return self.completion('{"1":"【银行】提供服务。","2":"【银行】服务居民。"}')
            return self.completion('{"1":"人们修建【桥梁】。","2":"居民使用【桥梁】。"}')

        self.complete.side_effect = reply
        jobs._run(job_id)
        self.assertEqual(jobs.get(job_id)["status"], "done")
        self.assertEqual(preparation.status("word", "bridge", 2)["preparation_state"], "ready")

    def test_startup_provider_error_is_not_treated_as_nothing_to_generate(self) -> None:
        with patch.object(providers, "get", side_effect=RuntimeError("key=secret")):
            with self.assertRaises(RuntimeError):
                self.request()
        status = preparation.status("word", "bank", 1)
        self.assertEqual(status["preparation_state"], "failed")
        self.assertNotIn("secret", status["preparation_error"])

    def test_old_contract_shape_is_pending_not_silently_ready(self) -> None:
        from backend.modules.review.contract import StudyItemSentences

        item = StudyItemSentences(**self.payload, sentences=[])
        self.assertEqual(item.preparation_state, "pending")
        self.assertIsNone(item.preparation_error)


if __name__ == "__main__":
    unittest.main()
