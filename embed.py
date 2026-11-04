"""Embeddings locales (ONNX, CPU, multilingue): comparar lo que busca el visitante con cada pantalla por significado."""
import numpy as np

MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
_m = None


def embed(texts):
    global _m
    if _m is None:
        from fastembed import TextEmbedding
        _m = TextEmbedding(MODEL)
    X = np.array(list(_m.embed(list(texts))), dtype=np.float32)
    return X / np.linalg.norm(X, axis=1, keepdims=True)


def node_text(n):
    return f"{n.get('name', '')}. {n.get('desc', '')} {' '.join(n.get('tags', []))}"
