import sys
import os
import asyncio
sys.path.append(os.path.join(os.getcwd(), 'src'))
from retriva.qa.answerer import ask_question
from retriva.config import settings
from retriva.registry import CapabilityRegistry
from retriva.qa.prompting import DefaultPromptBuilder
from retriva.qa.retriever import DefaultRetriever

# Ensure capabilities are registered
CapabilityRegistry().register("retriever", DefaultRetriever, priority=100)
CapabilityRegistry().register("prompt_builder", DefaultPromptBuilder, priority=100)

question = "Elenca tutti i documenti che conosci che parlano di apollo."

# Try with field "project"
filters = [{"field": "project", "operator": "eq", "value": "apollo"}]
print("Test 1: project eq apollo (Hard mode)")
res = ask_question(question, metadata_filters=filters, metadata_filter_mode="hard")
print("Retrieved chunks:", len(res['retrieved_chunks']))
print("Answer:", res['answer'][:200])

# Try with field "progetto"
filters2 = [{"field": "progetto", "operator": "eq", "value": "apollo"}]
print("\nTest 2: progetto eq apollo (Hard mode)")
res2 = ask_question(question, metadata_filters=filters2, metadata_filter_mode="hard")
print("Retrieved chunks:", len(res2['retrieved_chunks']))
print("Answer:", res2['answer'][:200])
