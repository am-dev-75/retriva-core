import sys
import os
sys.path.append(os.path.join(os.getcwd(), 'src'))
from retriva.qa.retriever import retrieve_top_chunks
from retriva.indexing.embeddings import get_embeddings
from retriva.indexing.qdrant_store import search_chunks, get_client

client = get_client()
query = "Elenca tutti i documenti che conosci che parlano di apollo."
q_vec = get_embeddings([query])[0]

chunks = search_chunks(client, q_vec, 20)
print(f"Total chunks retrieved: {len(chunks)}")

doc_counts = {}
for c in chunks:
    fname = c.get('filename', 'Unknown')
    doc_counts[fname] = doc_counts.get(fname, 0) + 1
    
for k, v in doc_counts.items():
    print(f"{k}: {v} chunks")
