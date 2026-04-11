"""
類似度チェックモジュール

文字n-gram + コサイン類似度で過去投稿との重複を検出する。
scikit-learn不要の純粋なPython実装（Windows DLL問題を回避）。
"""

import math
from collections import Counter
from typing import List, Tuple
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import SIMILARITY_THRESHOLD


def _ngrams(text: str, n: int) -> List[str]:
    """文字n-gramを生成する"""
    return [text[i:i+n] for i in range(len(text) - n + 1)]


def _text_to_vector(text: str) -> Counter:
    """テキストを文字2〜4gramのカウントベクトルに変換する"""
    vec: Counter = Counter()
    for n in (2, 3, 4):
        vec.update(_ngrams(text, n))
    return vec


def _cosine_similarity(vec1: Counter, vec2: Counter) -> float:
    """2つのカウントベクトルのコサイン類似度を計算する"""
    if not vec1 or not vec2:
        return 0.0
    dot = sum(vec1[k] * vec2[k] for k in vec1 if k in vec2)
    norm1 = math.sqrt(sum(v * v for v in vec1.values()))
    norm2 = math.sqrt(sum(v * v for v in vec2.values()))
    if norm1 == 0 or norm2 == 0:
        return 0.0
    return dot / (norm1 * norm2)


class SimilarityChecker:
    def __init__(self, threshold: float = SIMILARITY_THRESHOLD):
        self.threshold = threshold
        self._corpus: List[str] = []
        self._corpus_vecs: List[Counter] = []

    def load_history(self, posts: List[str]) -> None:
        """過去投稿をコーパスとしてロード"""
        self._corpus = posts
        self._corpus_vecs = [_text_to_vector(p) for p in posts]

    def is_duplicate(self, new_post: str) -> Tuple[bool, float]:
        """
        新しい投稿が過去投稿と類似しているか判定する。

        Returns:
            (is_duplicate, max_similarity_score)
        """
        if not self._corpus:
            return False, 0.0

        new_vec = _text_to_vector(new_post)
        max_sim = max(
            (_cosine_similarity(new_vec, cv) for cv in self._corpus_vecs),
            default=0.0,
        )
        return max_sim >= self.threshold, max_sim

    def add_to_corpus(self, post: str) -> None:
        """投稿後にコーパスへ追加"""
        self._corpus.append(post)
        self._corpus_vecs.append(_text_to_vector(post))

    def get_most_similar(self, new_post: str, top_k: int = 3) -> List[Tuple[int, float, str]]:
        """最も類似した過去投稿を返す（デバッグ用）"""
        if not self._corpus:
            return []
        new_vec = _text_to_vector(new_post)
        sims = [(_cosine_similarity(new_vec, cv), i) for i, cv in enumerate(self._corpus_vecs)]
        sims.sort(reverse=True)
        return [(i, sim, self._corpus[i]) for sim, i in sims[:top_k]]
