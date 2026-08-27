import asyncio

from backend.knowledge import (
    IngestionPolicy,
    KnowledgeDocumentInput,
    KnowledgeIngestionService,
)


class Repository:
    def __init__(self):
        self.put = None
        self.published = None

    async def put_document(self, tenant_id, document, chunks):
        self.put = tenant_id, document, chunks

    async def publish_document_version(self, tenant_id, document_id, version):
        self.published = tenant_id, document_id, version


class Vector:
    def __init__(self):
        self.items = []

    async def put_embedding(self, principal, **kwargs):
        self.items.append((principal, kwargs))
        return True


class Embedder:
    async def embed_documents(self, texts):
        return [[float(index), 0.0] for index, _text in enumerate(texts)]


def test_ingestion_cleans_chunks_embeds_then_publishes_version():
    repository = Repository()
    vector = Vector()
    service = KnowledgeIngestionService(
        repository,
        vector,
        Embedder(),
        embedding_model="test-2",
        policy=IngestionPolicy(chunk_chars=200, overlap_chars=20),
    )
    count = asyncio.run(
        service.ingest(
            "tenant-a",
            KnowledgeDocumentInput(
                document_id="doc-1",
                version=2,
                title="Runbook",
                status="published",
                allowed_departments=("it",),
            ),
            " SSO   reset\n\n\n" + "step " * 90,
        )
    )

    assert count == len(vector.items) >= 2
    assert repository.put[1].status == "draft"
    assert repository.published == ("tenant-a", "doc-1", 2)
    assert all(item[0].tenant_id == "tenant-a" for item in vector.items)
