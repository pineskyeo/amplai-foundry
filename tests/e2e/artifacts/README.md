# Render acceptance artifacts

- `golden-registry.json` — human-approved baseline digests for the rendered views in
  the `offline_views` entries of `.ai-team/policy/documentation.json`. `approval_ref` points to the R6 review.
  Sealed by `registry_sha256`; a builder that edits a golden breaks the seal (T-059).
- Regenerate only after a new human review: `RenderAcceptance.seal_registry(...)`.
- Browser screenshots are produced by `RenderAcceptance.browser_layer` only when a qualified
  renderer (playwright) is installed; otherwise the suite records `BROWSER_UNAVAILABLE` and the
  overall outcome is `inconclusive`, never `pass`.
