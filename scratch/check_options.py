# Copyright (C) 2026 Andrea Marson (am.dev.75@gmail.com)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

try:
    from docling.datamodel.pipeline_options import PdfPipelineOptions, ImagePipelineOptions
    print("Both PdfPipelineOptions and ImagePipelineOptions available")
except ImportError:
    try:
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        print("Only PdfPipelineOptions available")
    except ImportError:
        print("None available")
