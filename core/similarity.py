"""
類似度チェックモジュール

TF-IDF + コサイン類似度で過去投稿との重複を検出する。
日本語テキストに対してはキャラクターn-gramを使用（MeCab不要）。
"""

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
import numpy as np
from typing import List, Tuple
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import SIMILARITY_THRESHOLD


class SimilarityChecker:
    def __init__(self, threshold: float = SIMILARITY_THRESHOLD):
        self.threshold = threshold
        # analyzer='char_wb' = キャラクターn-gram（日本語対応、MeCab不要）
        self.vectorizer = TfidfVectorizer(
            analyzer='char_wb',
            ngram_range=(2, 4),
            min_df=1,
        )
        self._corpus: List[str] = []
        self._fitted = False

    def load_history(self, posts: List[str]) -> None:
        """過去投稿をコーパスとしてロード"""
        self._corpus = posts
        if posts:
            self.vectorizer.fit(posts)
            self._fitted = True

    def is_duplicate(self, new_post: str) -> Tuple[bool, float]:
        """
        新しい投稿が過去投稿と類似しているか判定する。

        Returns:
            (is_duplicate, max_similarity_score)
        """
        if not self._fitted or not self._corpus:
            return False, 0.0

        # コーパス + 新投稿をベクトル化
        all_texts = self._corpus + [new_post]
        try:
            tfidf_matrix = self.vectorizer.transform(all_texts)
        except Exception:
            # 語彙に含まれない場合は再fit
            self.vectorizer.fit(all_texts)
            tfidf_matrix = self.vectorizer.transform(all_texts)

        # 新投稿ベクトルと既存コーパスとのコサイン類似度
        new_vec = tfidf_matrix[-1]
        corpus_vecs = tfidf_matrix[:-1]

        similarities = cosine_similarity(new_vec, corpus_vecs)[0]
        max_sim = float(np.max(similarities))

        return max_sim >= self.threshold, max_sim

    def add_to_corpus(self, post: str) -> None:
        """投稿後にコーパスへ追加"""
        self._corpus.append(post)
        # コーパスが増えたら再fit
        if len(self._corpus) % 20 == 0:
            self.vectorizer.fit(self._corpus)
            self._fitted = True

    def get_most_similar(self, new_post: str, top_k: int = 3) -> List[Tuple[int, float, str]]:
        """最も類似した過去投稿を返す（デバッグ用）"""
        if not self._fitted or not self._corpus:
            return []

        all_texts = self._corpus + [new_post]
        try:
            tfidf_matrix = self.vectorizer.transform(all_texts)
        except Exception:
            self.vectorizer.fit(all_texts)
            tfidf_matrix = self.vectorizer.transform(all_texts)

        new_vec = tfidf_matrix[-1]
        corpus_vecs = tfidf_matrix[:-1]
        similarities = cosine_similarity(new_vec, corpus_vecs)[0]

        top_indices = np.argsort(similarities)[::-1][:top_k]
        return [(int(i), float(similarities[i]), self._corpus[i]) for i in top_indices]
