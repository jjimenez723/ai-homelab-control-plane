"""Retention triage: does this file belong in Angel?

Separate from naming on purpose. Naming asks "what is this called?"; triage
asks "is this worth keeping forever?". The answers are independent, and
conflating them is the specific mistake this package exists to avoid.

`IGNORE` means *do not add this to the durable Angel library*. It does not mean
delete it from the Mac, and nothing in this package can delete anything.
"""
