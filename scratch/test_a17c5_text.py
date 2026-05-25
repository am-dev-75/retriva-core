import sys
import os
sys.path.append(os.path.join(os.getcwd(), 'src'))
from retriva.indexing.qdrant_store import get_client, list_documents, COLLECTION_NAME
from qdrant_client.models import Filter, FieldCondition, MatchValue

client = get_client()

hits, _ = client.scroll(
    collection_name=COLLECTION_NAME,
    scroll_filter=Filter(
        must=[
            FieldCondition(key="filename", match=MatchValue(value="A17C5_IT_UG_V2_20250724.pdf"))
        ]
    ),
    limit=5,
    with_payload=True
)

for i, h in enumerate(hits):
    print(f"--- Chunk {i} ---")
    print(h.payload.get('text', '')[:500])
    print("Contains 'apollo'?", 'apollo' in h.payload.get('text', '').lower())

