#!/usr/bin/env python3
"""End-to-end test of the orchestrator with real graph backend."""

import asyncio
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from trifourier.agents.orchestrator import get_engine, query_service_dependencies, get_service_context
from trifourier.graph.backend import get_graph_backend
from scripts.seed_graph import seed


async def main():
    """Run E2E test."""
    print("🚀 Trifourier E2E Test\n")

    # 1. Seed the graph
    print("Step 1: Seeding knowledge graph...")
    await seed()

    # 2. Test graph queries
    print("\nStep 2: Testing graph queries...")
    try:
        print("\n  Query: checkout-api dependencies")
        deps = query_service_dependencies("checkout-api", max_depth=2)
        print(f"  Result: {deps[:200]}...\n")

        print("  Query: checkout-api context")
        context = get_service_context("checkout-api")
        print(f"  Result: {context[:200]}...\n")
    except Exception as e:
        print(f"  ❌ Graph query failed: {e}\n")

    # 3. Test orchestrator
    print("Step 3: Running orchestrator investigation...")
    try:
        engine = get_engine()
        result = await engine.investigate(
            symptom="checkout-api latency increased to 2.3s (p99)",
            namespace="production"
        )

        print(f"  ✓ Investigation ID: {result.investigation_id}")
        print(f"  ✓ Status: {result.status}")
        print(f"  ✓ Phase: {result.phase}")
        print(f"  ✓ Root Cause: {result.root_cause}")
        print(f"  ✓ Confidence: {result.aggregate_confidence:.2f}")
        print(f"  ✓ Affected Services: {result.affected_services}")
        print(f"  ✓ Findings Count: {len(result.findings)}")

    except Exception as e:
        print(f"  ❌ Orchestrator failed: {e}")
        import traceback
        traceback.print_exc()

    # Cleanup
    await (await get_graph_backend()).close()
    print("\n✅ Test complete!")


if __name__ == "__main__":
    asyncio.run(main())
