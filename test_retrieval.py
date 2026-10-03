import os
import unittest
from unittest.mock import Mock, patch

from langchain_core.documents import Document
from server.config import load_config
from server.rag.retrieval import (
    BM25Index, PDFRetriever, CrossEncoderReranker, build_retriever,
    reciprocal_rank_fusion,
)


def doc(text, page=0):
    return Document(page_content=text, metadata={'page': page})


class RetrievalTest(unittest.TestCase):
    def test_bm25_matches_english_and_chinese_and_ignores_absent_terms(self):
        english, chinese = doc('Specific ZX900 protocol'), doc('\u6df7\u5408\u68c0\u7d22\u65b9\u6cd5')
        index = BM25Index([english, chinese, doc('')])
        self.assertEqual(index.search('zx900', 5), [english])
        self.assertEqual(index.search('\u68c0\u7d22', 5), [chinese])
        self.assertEqual(index.search('absent', 5), [])
        self.assertEqual(BM25Index([]).search('x', 5), [])

    def test_persisted_chroma_can_rebuild_bm25_without_embedding(self):
        import tempfile
        from langchain_core.embeddings import Embeddings
        from langchain_community.vectorstores import Chroma
        from server.rag.vectorstore import load_vectorstore

        class OfflineEmbeddings(Embeddings):
            def embed_documents(self, texts):
                return [[1.0, float(len(text) % 3)] for text in texts]

            def embed_query(self, text):
                return [1.0, 0.0]

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            embeddings = OfflineEmbeddings()
            original = Chroma.from_documents(
                [doc('ZX900 protocol', 2), doc('unrelated material', 8)],
                embeddings, persist_directory=directory,
            )
            restored = load_vectorstore(embeddings, directory)
            retriever = PDFRetriever(restored, top_k=2, candidate_k=2)
            self.assertEqual(retriever.bm25.search('ZX900', 2), [doc('ZX900 protocol', 2)])
            hits = retriever.invoke('ZX900')
            self.assertEqual(len(hits), 2)
            self.assertEqual({hit.metadata['page'] for hit in hits}, {2, 8})
            original.delete_collection()

    def test_bm25_length_normalization(self):
        short, long = doc('needle'), doc('needle ' + 'other ' * 100)
        self.assertEqual(BM25Index([long, short]).search('needle', 2), [short, long])

    def test_fusion_promotes_agreement_and_preserves_distinct_pages(self):
        a, b, c = doc('a'), doc('b'), doc('c')
        self.assertEqual(reciprocal_rank_fusion([[a, b], [c, b]], 3)[0], b)
        self.assertEqual(reciprocal_rank_fusion([[a, a], [a]], 5), [a])
        other_page = doc('a', 1)
        self.assertEqual(len(reciprocal_rank_fusion([[a, other_page]], 5)), 2)

    def test_hybrid_recovers_lexical_hit_before_reranking(self):
        dense, lexical = doc('general text'), doc('ZX900 protocol', 2)
        store = Mock()
        store.get.return_value = {
            'documents': [dense.page_content, lexical.page_content],
            'metadatas': [dense.metadata, lexical.metadata],
        }
        store.similarity_search.return_value = [dense]
        reranker = Mock()
        reranker.rerank.side_effect = lambda query, docs: list(reversed(docs))
        retriever = PDFRetriever(store, top_k=1, candidate_k=3, reranker=reranker)
        self.assertEqual(retriever.invoke('ZX900'), [lexical])
        self.assertEqual(len(reranker.rerank.call_args.args[1]), 2)
        retriever.invoke('ZX900')
        store.get.assert_called_once()

    def test_disabled_hybrid_and_reranker_need_no_corpus_or_model(self):
        store = Mock()
        store.similarity_search.return_value = [doc('answer')]
        retriever = build_retriever(store, {
            'HYBRID_RETRIEVAL_ENABLED': False, 'RERANKER_ENABLED': False,
        })
        self.assertEqual(retriever.invoke('q'), [doc('answer')])
        store.get.assert_not_called()
        store.reset_mock()
        self.assertEqual(retriever.invoke('  '), [])
        store.similarity_search.assert_not_called()

    def test_reranker_fallback_and_strict_mode(self):
        store, reranker = Mock(), Mock()
        store.similarity_search.return_value = [doc('answer')]
        reranker.rerank.side_effect = RuntimeError('offline')
        retriever = PDFRetriever(store, hybrid=False, reranker=reranker)
        with self.assertLogs('server.rag.retrieval', level='WARNING'):
            self.assertEqual(retriever.invoke('q'), [doc('answer')])
        retriever.fallback = False
        with self.assertRaisesRegex(RuntimeError, 'offline'):
            retriever.invoke('q')

    def test_cross_encoder_orders_scores_and_rejects_nan(self):
        model = Mock()
        model.predict.return_value = [0.1, 0.9]
        docs = [doc('a'), doc('b')]
        with patch('server.rag.retrieval._MODELS', {('test', 'cpu'): model}):
            reranker = CrossEncoderReranker('test')
            self.assertEqual(reranker.rerank('q', docs), docs[::-1])
            self.assertEqual(model.predict.call_args.args[0], [('q', 'a'), ('q', 'b')])
            model.predict.return_value = [float('nan'), 0]
            with self.assertRaises(ValueError):
                reranker.rerank('q', docs)

    def test_invalid_configuration(self):
        for environment in [
            {'RETRIEVAL_TOP_K': '21'}, {'RETRIEVAL_CANDIDATE_K': '0'},
            {'RERANKER_ENABLED': 'maybe'}, {'RERANKER_BATCH_SIZE': '0'},
        ]:
            with self.subTest(environment=environment), patch('server.config.load_dotenv'), patch.dict(os.environ, environment, clear=True):
                with self.assertRaises(ValueError):
                    load_config()


if __name__ == '__main__':
    unittest.main()
