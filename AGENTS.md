# Repository Guidelines

## Commands

```bash
# TODO: Add your project's build, test, and lint commands
# Example:
# npm test
# pytest
# make lint
```

## Conventions

- When executing a multi-step plan, commit after each step so progress can be reverted if needed.
- Run tests after making changes if the project has tests.
- Commit messages must include a co-author line with your exact model name and version:
  ```
  Co-Authored-By: YOUR_MODEL_NAME VERSION <noreply@PROVIDER.com>
  ```
  For example: `Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>`, `Co-Authored-By: Gemini 2.5 Pro <noreply@google.com>`
- Keep changes focused — one logical change per commit.

## Code Style

<!-- TODO: Add project-specific style notes (e.g., formatting, naming conventions) -->

<!-- BEGIN codebase-assistant -->
## Reference Material

This project uses [codebase-assistant](/Users/katzw/GitHub/codebase-assistant) for reference codebases.

When you need information about related systems, consult in this order:
1. In-repo documentation (any docs already in this project)
2. Generated docs at `/Users/katzw/GitHub/codebase-assistant/codebases/CAVE/generated-docs/`, `/Users/katzw/GitHub/codebase-assistant/codebases/tos-ngauth/generated-docs/`
3. Source code at `/Users/katzw/GitHub/codebase-assistant/codebases/CAVE/repos/`, `/Users/katzw/GitHub/codebase-assistant/codebases/tos-ngauth/repos/`
4. Papers/specs at `/Users/katzw/GitHub/codebase-assistant/codebases/CAVE/papers/`, `/Users/katzw/GitHub/codebase-assistant/codebases/tos-ngauth/papers/` — only if generated docs don't cover it

### Available codebases
- **CAVE** — Connectome Annotation Versioning Engine - A system of microservices for collaborative neuroscience annotation — 24 repos
- **tos-ngauth** — Terms of Service and ngauth service — 1 repos
<!-- END codebase-assistant -->
