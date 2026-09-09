# Hermes AMPLAI Plugin

This package is a disabled-by-default Hermes integration asset. It exposes five
fixed-schema AMPLAI request/status tools. It does not expose activation, shell,
Git, file-edit, delegation, raw MCP, or canonical-knowledge write operations.

Configure a loopback AMPLAI endpoint, a project-scoped service token, and the
project ID outside this repository. Keep `feature_flags.enabled` false until the
provider-scoped Slack activation evidence gate is complete.

Validate the shipped profile:

```bash
python integrations/hermes-amplai/verify_profile.py \
  integrations/hermes-amplai/slack-profile.example.yaml
```
