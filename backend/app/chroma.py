import chromadb

from app.config import settings

COLLECTIONS = ["code_chunks", "learning_memory"]


def get_client():
    return chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port)


def ensure_collections():
    client = get_client()
    for name in COLLECTIONS:
        # embedding_function=None: we haven't chosen an embedding model yet
        client.get_or_create_collection(name=name, embedding_function=None)
    return [getattr(c, "name", c) for c in client.list_collections()]
