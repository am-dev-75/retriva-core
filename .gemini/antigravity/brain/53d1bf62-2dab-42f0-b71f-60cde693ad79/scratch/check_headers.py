import os

header = """# Copyright (C) 2026 Andrea Marson (am.dev.75@gmail.com)
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
"""

files_to_check = []
for root, dirs, files in os.walk('.'):
    if 'node_modules' in dirs:
        dirs.remove('node_modules')
    if '.git' in dirs:
        dirs.remove('.git')
    if '.venv' in dirs:
        dirs.remove('.venv')
    for file in files:
        if file.endswith('.py'):
            files_to_check.append(os.path.join(root, file))

mismatched_files = []
for file_path in files_to_check:
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read(len(header))
            if content != header:
                mismatched_files.append(file_path)
    except Exception as e:
        print(f"Error reading {file_path}: {e}")

if mismatched_files:
    print("Files with mismatched or missing headers:")
    for f in sorted(mismatched_files):
        print(f)
else:
    print("All Python files have the correct header.")
