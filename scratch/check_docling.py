
from docling.document_converter import DocumentConverter
import inspect

print(f"DocumentConverter init signature: {inspect.signature(DocumentConverter.__init__)}")
try:
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    print("PdfPipelineOptions available")
    options = PdfPipelineOptions()
    print(f"PdfPipelineOptions attributes: {dir(options)}")
except ImportError:
    print("PdfPipelineOptions NOT available")
