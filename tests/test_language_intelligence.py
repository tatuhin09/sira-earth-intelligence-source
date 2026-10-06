from pathlib import Path
import sys,tempfile,unittest
ROOT=Path(__file__).resolve().parents[1]
SRC=ROOT/"src"
sys.path[:]=[str(SRC)]+[x for x in sys.path if x!=str(SRC)]
from sira.language_intelligence import analyze_language,analyze_language_with_store,unicode_tokens
from sira.learning_consolidation import LearningConsolidationStore

class LanguageIntelligenceTests(unittest.TestCase):
    def test_profiles(self):
        self.assertEqual(analyze_language("আমি বাংলা লিখছি").language_label,"bn")
        b=analyze_language("ami akhon code korbo kono somossa hole amk bolo")
        self.assertEqual(b.language_label,"bn-Latn-banglish")
        self.assertTrue(b.needs_semantic_teacher)
        self.assertEqual(analyze_language("আমি code run korbo").language_label,"bn-en-mixed")
        e=analyze_language("Please run the code and test this memory module")
        self.assertEqual(e.language_label,"en")
        self.assertFalse(e.needs_semantic_teacher)

    def test_unicode_tokens_and_original_preservation(self):
        tokens=unicode_tokens("বাংলা স্মৃতি English русский")
        self.assertIn("বাংলা",tokens); self.assertIn("русский",tokens)
        raw="  আমি   code   run korbo  "
        p=analyze_language(raw)
        self.assertEqual(p.original_text,raw)
        self.assertEqual(p.normalized_text,"আমি code run korbo")

    def test_only_consolidated_mapping_expands(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); store=LearningConsolidationStore(root)
            store.record_language_mapping("korbo","will do",evidence_id="m:a",
                confidence=.95,verifier="v",verified=True)
            self.assertIsNone(analyze_language_with_store(root,"ami korbo").learned_gloss)
            store.record_language_mapping("korbo","will do",evidence_id="m:b",
                confidence=.95,verifier="v",verified=True)
            store.consolidate_language_mapping("korbo")
            self.assertIn("will do",analyze_language_with_store(root,"ami korbo").learned_gloss)

if __name__=="__main__": unittest.main()
