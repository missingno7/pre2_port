"""Recovered-native asset codecs for Prehistorik 2.

Clean, deterministic numeric reimplementations of original game routines, kept
strictly outside the VM layer (no CPU registers, no segment:offset addressing,
no UI). Each codec is verified byte-for-byte against the original ASM (the
oracle) before any thin VM hook is allowed to call it.
"""
