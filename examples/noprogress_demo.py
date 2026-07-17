#!/usr/bin/env python3
"""Example: No-progress loop detection (stdlib + embeddings)."""

from agentic_system.no_progress import NoProgressDetector, make_embedding_similarity
from agentic_system.embedding_similarity import make_embedding_similarity as make_emb


def demo_stdlib():
    """Stdlib difflib - catches verbatim/near-verbatim loops."""
    print("=== Stdlib difflib (verbatim loops) ===")
    det = NoProgressDetector(window=3, threshold=0.9)

    turns = [
        "I'll try to fix the bug by adding a null check.",
        "Let me add a null check to fix the bug.",
        "Adding a null check should fix the bug.",
        "I'll add a null check to resolve this.",
        "Let me try a different approach - maybe check for None first.",
    ]

    for i, turn in enumerate(turns):
        looped = det.record(turn)
        print(f"  Turn {i+1}: {'🔄 LOOP' if looped else '✅'} - {turn[:50]}...")

    print()


def demo_semantic_sentence_transformers():
    """Semantic embeddings via sentence-transformers (requires [embeddings] extra)."""
    print("=== Semantic (sentence-transformers) ===")
    try:
        sim = make_embedding_similarity(backend="sentence-transformers")
        det = NoProgressDetector(window=3, threshold=0.85, similarity=sim)

        turns = [
            "I'll fix the bug by adding a null check before accessing the property.",
            "Let me add a guard clause to check for None before the method call.",
            "Adding a None check at the start of the function should prevent the error.",
            "I'll implement a defensive check for null values before proceeding.",
            "Let me try a completely different approach - using optional chaining instead.",
        ]

        for i, turn in enumerate(turns):
            looped = det.record(turn)
            print(f"  Turn {i+1}: {'🔄 LOOP' if looped else '✅'} - {turn[:60]}...")

    except ImportError:
        print("  ⏭️  sentence-transformers not installed")
    print()


def demo_deterministic():
    """Deterministic hash-based similarity (no deps, fast, reproducible)."""
    print("=== Deterministic (hash-based) ===")
    sim = make_emb(backend="deterministic")
    det = NoProgressDetector(window=3, threshold=0.8, similarity=sim)

    turns = [
        "I'll fix the bug by adding a null check.",
        "Let me add a null check to fix this.",
        "Adding a null check should resolve the issue.",
        "I'll implement a null check here.",
    ]

    for i, turn in enumerate(turns):
        looped = det.record(turn)
        print(f"  Turn {i+1}: {'🔄 LOOP' if looped else '✅'} - {turn}")
    print()


def demo_realistic():
    """More realistic agent loop scenario."""
    print("=== Realistic Agent Loop ===")
    det = NoProgressDetector(window=4, threshold=0.85)

    # Simulate an agent trying to fix a test failure
    agent_turns = [
        "The test fails because the mock isn't configured correctly. I'll update the mock setup.",
        "Let me fix the mock configuration so the test passes.",
        "I'll adjust the mock return value to match the expected output.",
        "Updating the mock setup should make the test pass.",
        "Let me try a different mock configuration approach.",
        "I'll change the mock to return the expected value directly.",
        "Configuring the mock differently might work.",
        "OK, completely different approach - let me refactor the test itself.",
    ]

    for i, turn in enumerate(agent_turns):
        looped = det.record(turn)
        status = "🔄 LOOP DETECTED" if looped else "✅"
        print(f"  {i+1:2}. {status} | {turn}")

    print(f"\nLoop detected at turn {agent_turns.index('Let me try a different mock configuration approach.') + 1}")
    print("(The agent kept trying mock variations without stepping back)")


def main():
    demo_stdlib()
    demo_semantic_sentence_transformers()
    demo_deterministic()
    demo_realistic()

    print("=== Usage in Agent Loop ===")
    print("""
from agentic_system.no_progress import NoProgressDetector

detector = NoProgressDetector(window=3, threshold=0.9)

while True:
    response = agent.think_and_act()
    if detector.record(response):
        print("Loop detected! Escalating to human or changing strategy.")
        break
""")


if __name__ == "__main__":
    main()