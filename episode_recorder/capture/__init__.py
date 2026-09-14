"""Capture: getting both streams to disk, and nothing more.

No pairing, no decoding, no parsing beyond a sequence number happens here. That work is
offline, in build/, which nothing in this package may import.
"""
