
import logging
import os
from retriva.ingestion.docling_parser import DoclingParser
from retriva.config import settings

# Set up logging to see our debug message
logging.basicConfig(level=logging.DEBUG)

print(f"Current accelerator_device setting: {settings.accelerator_device}")

try:
    parser = DoclingParser()
    converter = parser._get_converter()
    print("Successfully initialized Docling converter with device settings.")
except Exception as e:
    print(f"Failed to initialize: {e}")
    import traceback
    traceback.print_exc()
