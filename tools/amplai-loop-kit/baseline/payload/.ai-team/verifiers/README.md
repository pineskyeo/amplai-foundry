# Generic Verifier Profile

registry.json is the executable source of truth. Run
`python3 .ai-team/verifiers/run.py --profile fast` for baseline integrity and shell checks.
standard adds validation of the selected feature's task manifests; full and v2 include
that same mandatory core. The Slice Eval executes its actual acceptance commands.
A generic PASS is baseline evidence, not product, deployment or minimum-runtime proof.

Add repository-specific checks to this registry; keep mandatory checks and profile names.
Missing required tools or inputs are unavailable, not skipped success. The Foundry source
repository retains its separate full product verification profile.
