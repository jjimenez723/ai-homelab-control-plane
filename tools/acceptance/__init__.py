"""Throwaway acceptance corpus + drill for the file-intelligence pipeline.

Everything here synthesizes disposable fixtures into a scratch directory and
runs the *real* `organize` CLI against a scratch T7 root. Nothing in this
package ever points at the owner's real Desktop/Documents/Downloads or at the
real Angel volume; `tools/acceptance/drill.py` refuses a `--root` inside the
home directory's real working folders or under `/Volumes/`.
"""
