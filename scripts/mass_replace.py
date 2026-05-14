import os
import re

dir_path = "/home/pankaj/Projects/leanchain/atelier/integrations"
extensions = (".md", ".py", ".sh", ".json", ".yaml")

replacements = [
    (r"mcp__atelier__task", r"mcp__atelier__context"),
    (r"get_reasoning_context", r"get_context"),
    (r"/atelier:reasoning", r"/atelier:context"),
    (r"Call `task`", r"Call `context`"),
    (r"Call task", r"Call context"),
    (r"atelier task", r"atelier context"),
    (r"atelier reasoning", r"atelier context"),
]

for root, _, files in os.walk(dir_path):
    for file in files:
        if file.endswith(extensions):
            path = os.path.join(root, file)
            if not os.path.exists(path) or os.path.islink(path):
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    content = f.read()

                new_content = content
                for old, new in replacements:
                    new_content = re.sub(old, new, new_content)

                if file.endswith(".md"):
                    # Make sure we don't accidentally match something like _task(
                    new_content = re.sub(r"\btask\(", "context(", new_content)

                if new_content != content:
                    print(f"Updated {path}")
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(new_content)
            except Exception as e:
                print(f"Skipping {path}: {e}")
