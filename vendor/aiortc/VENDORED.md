# Vendored aiortc (temporary)

This is upstream [aiortc](https://github.com/aiortc/aiortc) **1.15.0** with a
single change: the `av` upper pin is relaxed from `<18.0.0` to `<20.0.0`.

## Why

Home Assistant 2026.10 pins `av==19.0.0` in its global `package_constraints.txt`
(applied to every integration), while stock aiortc declares `av<18.0.0`, so no
PyPI aiortc is installable. aiortc 1.15.0 runs fine on av 19 (verified), so the
upstream pin is simply conservative.

The integration's `manifest.json` installs this copy via a git subdirectory:

```
aiortc @ git+https://github.com/lhw/jetkvm-integration@<sha>#subdirectory=vendor/aiortc
```

## Remove this when

Upstream aiortc allows av 19.x. Then delete `vendor/` and go back to
`"requirements": ["aiortc==1.15.0"]` (or newer).
