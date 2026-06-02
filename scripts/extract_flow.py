import json
import os
import sys
from pathlib import Path

from mitmproxy.io import FlowReader


def extract(path, output_file):
    print(f"=== PROCESSING: {path} -> {output_file} ===")
    try:
        with open(path, "rb") as f, open(output_file, "w") as out:
            reader = FlowReader(f)
            for flow in reader.stream():
                if flow.request and "v1/messages" in flow.request.url:
                    try:
                        content_str = flow.request.content.decode("utf-8", errors="ignore")
                        if not content_str.strip():
                            continue
                        req_body = json.loads(content_str)
                        out.write("\n--- INTERACTION ---\n")
                        if "system" in req_body:
                            out.write("System:\n")
                            out.write(json.dumps(req_body["system"], indent=2) + "\n")
                        if "messages" in req_body:
                            out.write("Messages:\n")
                            out.write(json.dumps(req_body["messages"], indent=2) + "\n")

                        if flow.response and flow.response.content:
                            resp_content_str = flow.response.content.decode("utf-8", errors="ignore")
                            if resp_content_str.strip():
                                resp_body = json.loads(resp_content_str)
                                out.write("--- RESPONSE ---\n")
                                if "content" in resp_body:
                                    out.write("Content:\n")
                                    out.write(json.dumps(resp_body["content"], indent=2) + "\n")
                                if "usage" in resp_body:
                                    out.write("Usage:\n")
                                    out.write(json.dumps(resp_body["usage"], indent=2) + "\n")
                    except json.JSONDecodeError:
                        pass
                    except (OSError, UnicodeError, AttributeError, TypeError) as e:
                        print(f"Error processing interaction: {e}")
        print(f"Saved to {output_file}")
    except (OSError, RuntimeError, UnicodeError, ValueError, TypeError) as e:
        print(f"Error reading/writing file: {e}")


def process_path(target_path):
    path = Path(target_path)
    if path.is_dir():
        for root, _, files in os.walk(path):
            for file in files:
                if file.endswith(".flow"):
                    full_path = Path(root) / file
                    output_file = full_path.with_suffix(".flow_dump.txt")
                    extract(str(full_path), str(output_file))
    elif path.exists():
        output_file = path.with_suffix(".flow_dump.txt")
        extract(str(path), str(output_file))
    else:
        print(f"Path not found: {target_path}")


if __name__ == "__main__":
    process_path(sys.argv[1])
