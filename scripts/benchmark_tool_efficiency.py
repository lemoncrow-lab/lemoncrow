#!/usr/bin/env python3
import os

# Adjust python path so we can import atelier
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../src")))

from atelier.core.capabilities.tool_supervision.search_read import search_read


def main():
    print("🚀 Running Atelier vs Native Tool Efficiency Benchmark\n")

    queries = [("ReasonBlock", "."), ("def check_plan", "."), ("mcp_server", ".")]

    total_naive = 0
    total_atelier = 0
    total_time = 0

    for query, path in queries:
        print(f"🔎 Testing Query: '{query}' in path: '{path}'")
        t0 = time.time()

        # Run the highly optimized Atelier search_read
        result = search_read(query=query, path=path, max_files=5, context_lines=8)

        duration = time.time() - t0
        total_time += duration

        naive_tokens = result.total_tokens + result.tokens_saved_vs_naive
        atelier_tokens = result.total_tokens

        total_naive += naive_tokens
        total_atelier += atelier_tokens

        print(f"  ⏱  Time taken: {duration:.2f} seconds")
        print(f"  🔥 Native/Naive Tokens: {naive_tokens}")
        print(f"  ⚡ Atelier Tokens:      {atelier_tokens}")
        if naive_tokens > 0:
            savings_pct = (1.0 - (atelier_tokens / naive_tokens)) * 100
            print(f"  💰 Saved:               {result.tokens_saved_vs_naive} tokens ({savings_pct:.1f}% reduction)\n")
        else:
            print("  💰 Saved:               0 tokens\n")

    print("==================================================")
    print("📊 BENCHMARK SUMMARY")
    print(f"Total Naive Tokens (Grep + Read file contents): {total_naive}")
    print(f"Total Atelier Tokens (Search tool):             {total_atelier}")
    if total_naive > 0:
        total_pct = (1.0 - (total_atelier / total_naive)) * 100
        print(f"Total Average Reduction:                        {total_pct:.1f}%")
    print(f"Total Execution Time:                           {total_time:.2f} seconds")
    print("==================================================")


if __name__ == "__main__":
    main()
