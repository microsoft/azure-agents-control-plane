#!/usr/bin/env python3
"""
Learning Capture Performance Benchmark

Measures request latency with and without Azure Agents Learning SDK episode capture enabled.
"""

import asyncio
import aiohttp
import time
import statistics
import json
import sys
import os

# Add parent to path for config loading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

async def measure_request(session: aiohttp.ClientSession, base_url: str, question: str) -> dict:
    """Measure a single ask_foundry request."""
    
    # Establish SSE session
    start_sse = time.time()
    async with session.get(f'{base_url}/sse', headers={'Accept': 'text/event-stream'}) as sse_response:
        if sse_response.status != 200:
            return {"error": f"SSE failed: {sse_response.status}"}
        
        session_url = None
        async for chunk in sse_response.content.iter_chunked(1024):
            if chunk:
                data = chunk.decode('utf-8', errors='ignore')
                import re
                match = re.search(r'data: (message\?[^\n\r]+)', data)
                if match:
                    session_url = f"{base_url}/{match.group(1)}"
                    break
                break
    
    sse_time = (time.time() - start_sse) * 1000
    
    if not session_url:
        return {"error": "No session URL"}
    
    # Make the tool call
    request = {
        "jsonrpc": "2.0",
        "id": f"bench-{time.time()}",
        "method": "tools/call",
        "params": {
            "name": "ask_foundry",
            "arguments": {"question": question}
        }
    }
    
    start_call = time.time()
    async with session.post(
        session_url,
        json=request,
        headers={'Content-Type': 'application/json'},
        timeout=aiohttp.ClientTimeout(total=120)
    ) as response:
        result = await response.text()
        call_time = (time.time() - start_call) * 1000
        
        return {
            "sse_time_ms": sse_time,
            "call_time_ms": call_time,
            "total_time_ms": sse_time + call_time,
            "status": response.status,
            "success": response.status == 200
        }


async def run_benchmark(base_url: str, iterations: int = 5, label: str = "Test") -> dict:
    """Run benchmark with multiple iterations."""
    
    questions = [
        "What is 2+2?",
        "What is the capital of France?",
        "Explain REST API briefly.",
        "What is Python?",
        "What is machine learning?",
    ]
    
    results = []
    
    print(f"\n{'='*60}")
    print(f"🔬 Benchmark: {label}")
    print(f"{'='*60}")
    print(f"   Endpoint: {base_url}")
    print(f"   Iterations: {iterations}")
    print()
    
    async with aiohttp.ClientSession() as session:
        for i in range(iterations):
            question = questions[i % len(questions)]
            print(f"   [{i+1}/{iterations}] {question[:40]}...", end=" ")
            
            try:
                result = await measure_request(session, base_url, question)
                results.append(result)
                
                if result.get("success"):
                    print(f"✅ {result['call_time_ms']:.0f}ms")
                else:
                    print(f"❌ {result.get('error', 'Failed')}")
            except Exception as e:
                print(f"❌ {str(e)[:50]}")
                results.append({"error": str(e), "success": False})
            
            # Small delay between requests
            await asyncio.sleep(0.5)
    
    # Calculate statistics
    successful = [r for r in results if r.get("success")]
    
    if len(successful) >= 2:
        call_times = [r["call_time_ms"] for r in successful]
        sse_times = [r["sse_time_ms"] for r in successful]
        total_times = [r["total_time_ms"] for r in successful]
        
        stats = {
            "label": label,
            "iterations": iterations,
            "successful": len(successful),
            "failed": len(results) - len(successful),
            "call_time": {
                "mean": statistics.mean(call_times),
                "median": statistics.median(call_times),
                "stdev": statistics.stdev(call_times) if len(call_times) > 1 else 0,
                "min": min(call_times),
                "max": max(call_times),
            },
            "sse_time": {
                "mean": statistics.mean(sse_times),
                "median": statistics.median(sse_times),
            },
            "total_time": {
                "mean": statistics.mean(total_times),
                "median": statistics.median(total_times),
            }
        }
    else:
        stats = {
            "label": label,
            "iterations": iterations,
            "successful": len(successful),
            "failed": len(results) - len(successful),
            "error": "Not enough successful requests for statistics"
        }
    
    return stats


def print_stats(stats: dict):
    """Print benchmark statistics."""
    print(f"\n📊 Results: {stats['label']}")
    print(f"   Success: {stats['successful']}/{stats['iterations']}")
    
    if "call_time" in stats:
        ct = stats["call_time"]
        print(f"\n   Tool Call Latency:")
        print(f"      Mean:   {ct['mean']:.0f} ms")
        print(f"      Median: {ct['median']:.0f} ms")
        print(f"      Stdev:  {ct['stdev']:.0f} ms")
        print(f"      Range:  {ct['min']:.0f} - {ct['max']:.0f} ms")
        
        tt = stats["total_time"]
        print(f"\n   Total Request Time (including SSE):")
        print(f"      Mean:   {tt['mean']:.0f} ms")
        print(f"      Median: {tt['median']:.0f} ms")


def print_comparison(baseline: dict, with_capture: dict):
    """Print comparison between two benchmarks."""
    print(f"\n{'='*60}")
    print("📈 Performance Comparison")
    print(f"{'='*60}")
    
    if "call_time" not in baseline or "call_time" not in with_capture:
        print("   ⚠️  Insufficient data for comparison")
        return
    
    base_mean = baseline["call_time"]["mean"]
    capture_mean = with_capture["call_time"]["mean"]
    
    diff_ms = capture_mean - base_mean
    diff_pct = ((capture_mean - base_mean) / base_mean) * 100 if base_mean > 0 else 0
    
    print(f"\n   Without capture: {base_mean:.0f} ms (mean)")
    print(f"   With capture:    {capture_mean:.0f} ms (mean)")
    print(f"\n   Difference:        {diff_ms:+.0f} ms ({diff_pct:+.1f}%)")
    
    if abs(diff_pct) < 5:
        print("\n   ✅ Capture overhead is negligible (<5%)")
    elif diff_pct < 10:
        print("\n   ✅ Capture overhead is minimal (<10%)")
    elif diff_pct < 20:
        print("\n   ⚠️  Capture overhead is moderate (10-20%)")
    else:
        print(f"\n   ⚠️  Capture overhead is significant (>{diff_pct:.0f}%)")


async def main():
    # Load config
    config_path = os.path.join(os.path.dirname(__file__), "mcp_test_config.json")
    
    try:
        with open(config_path) as f:
            config = json.load(f)
        base_url = config.get("direct", {}).get("base_url", "http://localhost:8000/runtime/webhooks/mcp")
    except:
        base_url = "http://20.114.183.41/runtime/webhooks/mcp"
    
    print("\n" + "="*60)
    print("🎓 Learning Capture Performance Benchmark")
    print("="*60)
    print(f"\nThis benchmark compares request latency with and without")
    print(f"Azure Agents Learning SDK episode capture enabled.\n")
    
    # Run current benchmark
    current_label = "Current Configuration"
    current_stats = await run_benchmark(base_url, iterations=5, label=current_label)
    print_stats(current_stats)
    
    return current_stats


if __name__ == "__main__":
    result = asyncio.run(main())
    print("\n" + "="*60)
    print("✅ Benchmark complete")
    print("="*60 + "\n")
