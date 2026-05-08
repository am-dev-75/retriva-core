
try:
    from docling.datamodel.pipeline_options import PdfPipelineOptions, ImagePipelineOptions
    print("Both PdfPipelineOptions and ImagePipelineOptions available")
except ImportError:
    try:
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        print("Only PdfPipelineOptions available")
    except ImportError:
        print("None available")
