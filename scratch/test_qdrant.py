import sys
import os
sys.path.append(os.path.join(os.getcwd(), 'src'))
from retriva.indexing.qdrant_store import get_client, list_documents, COLLECTION_NAME
from qdrant_client.models import Filter, FieldCondition, MatchValue

client = get_client()
docs = list_documents(client)
for d in docs:
    print(f"Doc: {d.get('filename')}, User metadata: {d.get('user_metadata')}")

# Try filtering
hits, _ = client.scroll(
    collection_name=COLLECTION_NAME,
    scroll_filter=Filter(
        must=[
            FieldCondition(key="user_metadata.project", match=MatchValue(value="apollo"))
        ]
    ),
    limit=10,
    with_payload=True
)
print(f"\nFiltered hits: {len(hits)}")
for h in hits:
    print(h.payload.get('filename'))

