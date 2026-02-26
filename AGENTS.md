# Repository Guidelines

## Repository layout

The git root is the top-level `DatasetGateway/` directory. Orient yourself
here before doing anything else — do not assume your working directory is
the repo root.

```
DatasetGateway/              ← git root
├── README.md             ← project overview, quick-start, env vars
├── CLAUDE.md
├── AGENTS.md             ← this file
├── .gitignore
├── docs/                 ← architecture, API reference, user manual
│   ├── architecture.md
│   ├── cave-auth-endpoints.md
│   ├── clio-support.md
│   ├── implemented-plan.md
│   └── user-manual.md
└── dsg/          ← Django project (the only Python package)
    ├── pyproject.toml    ← build config, dependencies, pixi config
    ├── pixi.lock
    ├── manage.py
    ├── Dockerfile
    ├── dsg/      ← Django settings package
    ├── core/             ← shared models, middleware
    ├── cave_api/         ← CAVE-compatible auth endpoints
    ├── auth_api/         ← Clio/neuprint auth endpoints
    ├── ngauth/           ← Neuroglancer GCS token endpoints
    ├── scim/             ← SCIM 2.0 provisioning
    └── web/              ← browser login/admin UI
```

**Important:** All Python code, tests, and the pixi environment live
under `dsg/`. Run `pixi` commands from that directory. The
top-level `DatasetGateway/` contains documentation and repo-level config
only.

## Commands

```bash
cd dsg
pixi install                              # create/update environment
pixi run python manage.py check           # verify Django loads
pixi run -e dev python -m pytest          # run tests (dev environment)
pixi shell                                # interactive shell in env
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
- If your changes add, remove, or rename files/directories shown in the
  "Repository layout" tree above, update that tree to match.

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
