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

directories = ["src", "tests", "scripts"]
modified_count = 0

for d in directories:
    if not os.path.exists(d):
        continue
    for root, dirs, files in os.walk(d):
        for f in files:
            if f.endswith(".py"):
                path = os.path.join(root, f)
                with open(path, "r", encoding="utf-8") as file:
                    content = file.read()
                
                # Check if it already has the copyright string
                if "# Copyright (C) 2026 Andrea Marson" not in content:
                    with open(path, "w", encoding="utf-8") as file:
                        file.write(header + content)
                    modified_count += 1
                    print(f"Added header to {path}")

print(f"Total files modified: {modified_count}")
