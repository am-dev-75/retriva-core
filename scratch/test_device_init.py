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
