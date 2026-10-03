import math
import unittest

from evaluation.retrieval_eval.metrics import evaluate_ranking
from evaluation.retrieval_eval.statistics import bootstrap_mean_ci, percentile


class RetrievalMetricsV2Test(unittest.TestCase):
    def test_hit_recall_mrr_and_ndcg(self):
        metrics = evaluate_ranking([8, 3, 4, 9, 10], {3, 4})

        self.assertEqual(metrics["hit_at_1"], 0)
        self.assertEqual(metrics["hit_at_3"], 1)
        self.assertEqual(metrics["hit_at_5"], 1)
        self.assertEqual(metrics["recall_at_5"], 1)
        self.assertEqual(metrics["mrr_at_5"], 0.5)
        expected_dcg = 1 / math.log2(3) + 1 / math.log2(4)
        expected_idcg = 1 + 1 / math.log2(3)
        self.assertAlmostEqual(metrics["ndcg_at_5"], expected_dcg / expected_idcg)

    def test_duplicate_gold_page_occupies_slot_without_duplicate_gain(self):
        metrics = evaluate_ranking([2, 2, 3, 7, 8], {2, 3})

        self.assertEqual(metrics["recall_at_5"], 1)
        self.assertEqual(metrics["mrr_at_5"], 1)
        expected_dcg = 1 + 1 / math.log2(4)
        expected_idcg = 1 + 1 / math.log2(3)
        self.assertAlmostEqual(metrics["ndcg_at_5"], expected_dcg / expected_idcg)

    def test_missing_gold_has_zero_quality_metrics(self):
        metrics = evaluate_ranking([1, 2, 3, 4, 5], {9})

        for key in ("hit_at_1", "hit_at_3", "hit_at_5", "recall_at_5", "mrr_at_5", "ndcg_at_5"):
            self.assertEqual(metrics[key], 0)
        self.assertIsNone(metrics["first_relevant_rank"])

    def test_percentile_uses_linear_interpolation(self):
        self.assertEqual(percentile([1, 2, 3, 4, 5], 0.50), 3)
        self.assertAlmostEqual(percentile([1, 2, 3, 4, 5], 0.95), 4.8)

    def test_bootstrap_confidence_interval_is_reproducible(self):
        first = bootstrap_mean_ci([0, 1, 1, 0, 1], samples=500, seed=42)
        second = bootstrap_mean_ci([0, 1, 1, 0, 1], samples=500, seed=42)

        self.assertEqual(first, second)
        self.assertLessEqual(first["lower"], first["mean"])
        self.assertGreaterEqual(first["upper"], first["mean"])


if __name__ == "__main__":
    unittest.main()
