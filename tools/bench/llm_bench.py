"""CLI entry point for model-free or injected local model benchmark runs."""
from .corpus import load_corpus
from .harness import run, write_jsonl

def main(provider, output=None, **kwargs):
    results = run(provider, load_corpus(), **kwargs)
    if output: write_jsonl(results, output)
    return results
