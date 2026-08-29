# Portable contract tests

Run from the repository root on Windows or macOS:

```text
python -m unittest discover -s Tests/Portable -v
python -m SeparationWorker.protocol.selftest
python -m SeparationWorker.engine.fixture_harness
```

Fixtures are deterministically generated from numeric samples in project source;
no third-party audio is included. Their thresholds are synthetic oracles with a
`CALIBRATION REQUIRED` boundary. These tests prove portable contracts only, not
native macOS audio, perceptual/model quality, packaging, or commercial readiness.
