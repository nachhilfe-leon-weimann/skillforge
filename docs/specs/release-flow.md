# Spec: Release flow (one release and deploy pipeline for the whole skill-platform)

> Status: Implemented - P0 on `main`, `v0.4.0` released and deployed through the flow (2026-09-20);
> P1: P1-1 implemented - `compose.yml` pins the deployed version, `v0.5.0` (2026-09-25) was the first release
> to move the pin; P1-4 in #125; P1-2 and P1-3 dropped. P2 *Shared workflows* done: the deploy lives in
> [`skill-platform-workflows`][workflows] since 2026-09-20 (#133).
> Platform arc (skillforge first, then skillbot and skillsite): skillbot adopted the flow (skillbot#10) and
> released `v0.1.0` through it on 2026-09-21.
> This spec is also the decision record (no separate ADR: *Decided defaults*, *Verified behavior* and
> *Trade-offs accepted* carry the why). Written in skillforge because it is the first adopter; the **platform
> contract** below is what the other repos copy. Every GitHub behavior this spec relies on was verified in a
> throwaway repo (see *Verified behavior*); nothing here assumes a feature GitHub does not have.

## Problem statement

The three deployable repos release in three different ways:

| Repo | Release trigger | Version bump | Deploy |
|---|---|---|---|
| skillforge | version in `pyproject.toml` has no GitHub release yet, checked after CI on `main` (the former `release.yml`) | manual workflow opens a bump PR (`version-bump.yml`, removed by P0-5) | deploy webhook, image `:latest` |
| skillsite | manual `workflow_dispatch` | a bot commits and tags directly on `main` | deploy webhook |
| skillbot | push to `main` -> dev, tag `v*` -> prod | none (tag by hand) | two deploy webhooks, unpinned actions |

Three mental models for one person is friction on every release. On top of that:

- **The deploy is fire-and-forget.** The webhook call succeeding only means Dokploy accepted the request. A
  failed migration or a crash-looping container leaves the workflow green.
- **`:latest` with `pull_policy: always`** ([`compose.yml`](../../compose.yml)) means any restart of the stack
  may pull a different version than the one that was released.
- **Nothing forces CI to be green on `main`.** The `main` ruleset requires signatures and linear history, but
  no status check.
- **`codeql.yml` produces alerts nobody reads.**

## Goals

1. **One flow, three repos.** Same workflow file names, same job names, same secrets, same `just` entry points.
   Knowing how one repo releases means knowing all of them.
2. **Releasing is one deliberate act:** shipping the release PR. Version, changelog, tag, image, publish and
   deploy follow from it without further manual steps.
3. **A readable `main`.** One commit per logical slice, conventional commit messages, one release commit per
   version. Tags on `main` plus `CHANGELOG.md` are the condensed view - no release branch.
4. **The deploy reports the truth.** The workflow talks to the Dokploy API, waits for the deployment to finish
   and verifies the running version. A failed deploy is a red workflow.
5. **`main` only accepts green commits.** PRs land by squash merge with `gh pr merge --auto`, which waits for the
   check; a local fast-forward (`git ship`) is only accepted when its tip is already green.
6. **Less machinery than today**, not more. Everything the platform does not need is removed.

## Non-goals

- **Release branches, backports, maintenance lines.** One prod, one supported version.
- **A dev or staging environment.** Only prod exists platform-wide. Nothing deploys on a plain push to `main`;
  skillbot's dev deploy is dropped (the bot runs locally via `just dev`).
- **Supply-chain hardening from `github-actions-playground`:** digest pinning, attestations, signed release
  tags, image promotion, release-overlap verification, multi-arch images. Deliberately out.
- **Automatic rollback.** An app rollback does not roll back an Alembic migration; pretending otherwise is
  worse than fixing forward. Rollback stays a documented manual procedure.
- **A CodeQL workflow.** GitHub's code scanning *default setup* can be switched on in the repo settings later
  without any file in the repo.
- **Local git hooks** (lefthook, pre-commit). Considered as P1-2 and dropped on 2026-09-20: CI is the gate, and
  with squash merges the PR title is the one commit message that counts.
- **Deploy notifications** (a Discord message per deploy). Considered as P1-3 and dropped on 2026-09-20: a failed
  deploy already is a red workflow run.
- **Sharing more than is identical.** The shared repo holds what is the same file in every repo - the deploy
  with its script, and `triage.yml`. `ci.yml`, `build.yml` and `release.yml` differ per stack and stay per repo,
  behind the same names.
- **skillcore.** A private library, not deployed; it may adopt the release half later (note: on the Free plan
  org secrets and rulesets do not apply to private repos).

## Decided defaults

| Topic | Decision | Rationale |
|---|---|---|
| **A - Release mechanism** | [release-please](https://github.com/googleapis/release-please) (`release-please-action`, pinned by SHA). It keeps one *release PR* up to date (version bump + `CHANGELOG.md`); shipping that PR is the release. | Same tool for Python and Node; version derived from conventional commits; replaces the hand-written bump workflow. Verified with `git ship` and the rulesets; squash merging is release-please's own default. |
| **B - Environments** | Prod only. One GitHub Environment `production` per repo. | Decided 2026-09-19. |
| **C - Branching** | Short-lived feature branches -> PR -> squash-merged onto `main` with `gh pr merge <n> -sd --auto` (waits for `check`, deletes the branch). `git ship` (local fast-forward) stays allowed, but only works when the branch tip already carries a green `check`. No release branch. Tags `vX.Y.Z` on `main`. | Linear history and signatures already enforced; a second branch would be a second history to keep in sync. |
| **D - History on `main`** | One PR per slice (not per spec); before shipping, fold fixups and "tick the spec" commits into the slice they belong to. Every commit on `main` is green on its own and conventional. A squash merge does the folding by itself: one PR, one commit, so the PR title must be the conventional message (for a single-commit PR GitHub takes that commit's subject instead). | ~6-8 commits for an arc like `crm-api.md` instead of ~40 - but not 1-2, which would destroy `git bisect` and the story. |
| **E - Release PR author** | An org-wide **GitHub App**; release-please runs with its installation token. The App is **`skill-platform-release`** - the installed `skillsite-release-bot`, renamed (no `bot` in the name: GitHub appends `[bot]`, and it keeps clear of *skillbot*) and extended to the other repos. It started as releases only; since 2026-09-20 it is the platform's one automation App and also puts issues and PRs on the project board ([`project-intake.md`](project-intake.md)) - each workflow narrows its token to the permissions it needs. | PRs opened with `GITHUB_TOKEN` get no CI run, so the required check (G) would reject the release PR. Verified; see below. |
| **F - Signatures** | Feature commits carry Leon's signature when they land via `git ship`, GitHub's when they are squash-merged. The one release commit per version is created through the GitHub API and carries GitHub's signature ("Verified"). Tags are lightweight and unsigned. | Satisfies the `required_signatures` rule; same trust level as today's bump PR. |
| **G - Gate on `main`** | The `main` ruleset additionally requires the status check **`check`**. Every repo's CI exposes a job with exactly this name. | A fast-forward keeps the commit SHA, so the green check from the PR still counts on push (verified); a squash merge enforces the check itself. A tip without a green check - unpushed, still running or cancelled - is rejected, which is why `--auto` is the default way to merge. |
| **H - Deploy transport** | Dokploy **API** (`x-api-key`): `compose.deploy`, then poll `deployment.allByCompose` until `done` / `error`, then verify the health endpoint. The deploy webhook is removed. | Authenticated, observable, fails loudly. Endpoints proven in `github-actions-playground`. |
| **I - Deployed version** | `compose.yml` pins `image: ...:vX.Y.Z`, rewritten by release-please in the release commit (P1-1; P0 deployed `:latest`). | One annotation per `image:` line; `main` then records what prod runs, and a restart cannot pull another version. |
| **J - Health contract** | Every service with a public HTTP endpoint answers `GET /health` with at least `status` and `version`. Services without HTTP (skillbot) are verified by the Dokploy deployment status alone. | Lets the deploy job prove that the *new* version is the one answering. |
| **K - Local hooks** | None. (Originally: lefthook calling `just` recipes; dropped on 2026-09-20 together with P1-2.) | CI is the gate; a hook can be skipped with `--no-verify` anyway. |
| **L - Pre-1.0 versioning** | `bump-minor-pre-major: true`: while `0.x`, a breaking change bumps the minor, `feat` bumps the minor, `fix` the patch. | Nothing is live yet; `1.0.0` should be a deliberate decision, not a side effect of one `!`. |

## Verified behavior

Probed on 2026-09-20 in a throwaway repo with the same rulesets as skillforge (`required_signatures`,
`required_linear_history`, no force-push, protected `v*` tags), `release-please-action` v5.0.0, three complete
release cycles shipped with the real `git ship` alias.

| Question | Result |
|---|---|
| Does release-please recognise a release PR merged by fast-forward push? | **Yes.** GitHub marks the PR `MERGED` (merge commit = PR head), the label flips from `autorelease: pending` to `autorelease: tagged`, tag and GitHub release are created. skillforge PRs #112/#113 show the same `MERGED` state. |
| Do the `release_created` / `tag_name` / `sha` outputs drive follow-up jobs in the same workflow? | **Yes.** This is how build and deploy are chained - events created with `GITHUB_TOKEN` never start *other* workflows, so an `on: release` trigger is not an option. |
| Does the release commit pass `required_signatures` + `required_linear_history`? | **Yes.** Commits created through the GitHub API are signed by GitHub (`web-flow`). Observed for `GITHUB_TOKEN` in the probe and confirmed live for the App token: release PR #117 was opened by `skill-platform-release[bot]`, its commit is Verified (committer GitHub), and CI ran on it. |
| Can `uv.lock` and `openapi.json` follow the version? | **Yes**, via `extra-files`: a `toml` updater with `$.package[?(@.name.value=='<package>')].version` and a `json` updater with `$.info.version`. `uv lock --check` stays consistent. The json updater re-serializes the whole file with JavaScript, so `dump_openapi.py` writes integer-valued floats as integers - otherwise `50.0` comes back as `50` and `just openapi-check` fails on the release PR (found in the final review, not in the probe). A JSONPath that stops matching is a **silent no-op** - CI's `uv sync --locked` is the backstop. |
| Can the image tag in `compose.yml` follow the version? | **Yes**, via a `generic` `extra-files` entry: on every line carrying `x-release-please-version` the updater replaces the first semver and leaves the `v` in front of it alone. Checked by running the `Generic` updater of release-please 17.6.0 - the version `release-please-action` v5.0.0 locks - against the file: exactly the three `image:` lines went from `v0.4.0` to `v0.5.0`. Not yet seen in a release PR. A line without the annotation is a **silent no-op**, like a stale JSONPath - `test_release_config.py` and the deploy's version check are the backstops. |
| Does release-please cope with squash-merged PRs? | **Yes** (live): #114-#121 were squash-merged; it reads the squash commit's conventional subject and rebuilt the release PR #117 after every merge. |
| What does Dokploy answer to `compose.deploy`, and in which order does `deployment.allByCompose` list? | Seen live with `v0.4.0`: the answer holds `composeId`, `message`, `success` - **no deployment id** - and the list is newest-first. So the script identifies its deployment as the newest id that did not exist before the request; that heuristic cannot be replaced by a returned id. |
| Tag format? | `include-component-in-tag: false` yields plain `vX.Y.Z`, matching the existing tags. |
| Required status check + `git ship` for a normal PR? | **Works** - same SHA, the green check is already there. **Only then:** live on 2026-09-20 a `git ship` was rejected because one local commit sat on top of the green PR tip (its CI run was cancelled). `gh pr merge <n> -sd --auto` waits for the check instead (used for #123). |
| ... and for the release PR opened with `GITHUB_TOKEN`? | **Rejected:** `Required status check "check" is expected`. The PR's CI run is created but never runs jobs. |
| Workarounds without an App? | Closing and reopening the PR as a user starts CI, then shipping works (tested). Starting CI on the release branch via `workflow_dispatch` produced a green check that did **not** satisfy the rule (tested). Hence decision E. |
| Does a workflow called from another repo get the caller's configuration? | **Yes** (live, `dry_run` dispatch after #133): inside `skill-platform-workflows`' `deploy.yml` the job's `environment: production` is this repo's environment - `secrets.DOKPLOY_API_KEY` (through `secrets: inherit`) and `vars.DOKPLOY_COMPOSE_ID` resolve, as does the org variable `DOKPLOY_BASE_URL`. `job.workflow_repository` / `job.workflow_sha` name the shared repo and the ref behind `@v1` (for an annotated tag the tag object's SHA, which `actions/checkout` resolves), so the script comes from the same ref as the workflow. The nesting `release.yml` -> `deploy.yml` -> shared `deploy.yml` with `secrets: inherit` on both levels is accepted. |
| What if a step after the action fails in the release-please job? | The release already exists but build and deploy are skipped - a half-done release. Hence P0-3's "nothing after the action" rule and the manual deploy entry point. |

## Trade-offs accepted

- **Commit messages become load-bearing.** The changelog and the version bump are derived from them; a sloppy
  message on `main` is a wrong changelog line or a missed bump. Nothing checks them locally (P1-2 was dropped);
  with a squash merge the PR title is the message that counts, so it is the thing to get right.
- **The release workflow holds a Dokploy API key**, which can do more than a single-purpose webhook URL. It
  lives only in the `production` environment, restricted to `main`.
- **One commit per release is signed by GitHub, not by Leon.** It is mechanical, reviewable as a PR, and the
  same trust level today's bump PR has. Squash-merged PRs are GitHub-signed in the same way.
- **Stacks only through GitHub.** A squash merge gives the lower commits new SHAs, so a branch stacked by hand
  conflicts with `main` - this happened to #118 and #119 after #114-#116 were squashed. GitHub's stacked pull
  requests (`gh stack`, in public preview) retarget and rebase the PRs above a merged one themselves, so
  dependent slices form such a stack; every other PR stays independent.
- **Feature commits on `main` are GitHub-signed, not Leon-signed**, because squash is the default. `git ship`
  would keep his signature, but needs a green tip and therefore a wait that `--auto` does for you.
- **The platform depends on one GitHub App and its private key.** If the App is unavailable, the fallback is
  to close and reopen the release PR as a user so CI runs on it.
- **Every repo's deploy follows a moving tag in another repo.** `@v1` is what makes one fix reach three repos
  without three PRs, and it means a bad `v1` breaks the deploy everywhere at once. Accepted: nothing deploys
  without a release, a broken deploy is a red run and not a broken prod, and the way back is moving `v1` onto
  the earlier release. The shared repo has the same gate on `main` (`check`: actionlint, shellcheck, the script
  against a fake Dokploy), and its `vX.Y.Z` tags cannot be moved.
- **For about two minutes per release `main` names an image that does not exist yet** - from the release commit
  until the image job has pushed `:vX.Y.Z` (longer if that job fails). Only a deploy started by hand in that
  window is affected: its pull fails and the deployment ends in `error` - compose pulls before it replaces a
  container, so the running ones stay.

## Target shape

```
feature branch -> PR (CI: job "check" green) -> gh pr merge -sd --auto -> main
                                                            |
                          release.yml: release-please (App token) keeps the release PR current
                                                            |
                    you merge the release PR (gh pr merge -sd --auto) = the release
                                                            |
        release.yml on that push: tag vX.Y.Z + GitHub release + CHANGELOG.md   (release_created == true)
                                                            |
               build image (:vX.Y.Z, :sha-..., :latest)  ->  repo-specific publish (forge: PyPI client)
                                                            |
        deploy.yml: Dokploy API compose.deploy -> wait for done/error -> GET /health (status + version)
```

## Platform contract

What is identical in every repo; everything else is repo-specific detail behind these names.

- **Workflows:** `ci.yml` (PRs and pushes to `main`; contains the job **`check`**), `release.yml` (push to
  `main`: release-please, then build / publish / deploy when `release_created`), `deploy.yml` (`workflow_call`
  + `workflow_dispatch`; the repo's entry point for a deploy). Outside the release flow, `triage.yml` puts
  issues and PRs on the project board - its contract lives in [`project-intake.md`](project-intake.md).
- **Shared workflows:** `deploy.yml` and `triage.yml` are callers of the workflows of the same name in the public
  repo [`skill-platform-workflows`][workflows], referenced as `...@v1` with `secrets: inherit`. Its `deploy.yml` and
  the script next to it are the only code that talks to Dokploy; it declares the `production` environment and the
  concurrency group `deploy-production` itself, so a caller declares neither. Moving the tag `v1` rolls a change
  out to every repo; a change callers have to follow is a `v2`. The README there has the callers to copy.
- **Config:** `release-please-config.json` and `.release-please-manifest.json` in the repo root; tags `vX.Y.Z`.
  `compose.yml` pins the deployed image to that tag (`x-release-please-version` on every `image:` line, a
  `generic` `extra-files` entry).
- **`just` entry points:** `just check` (everything that must be green before a push). CI's `check` job runs at
  least `just check`.
- **Org-level:** variable `RELEASE_APP_CLIENT_ID`, secret `RELEASE_APP_PRIVATE_KEY` (the names skillsite already
  uses), variable `DOKPLOY_BASE_URL`.
- **Environment `production` (per repo):** secret `DOKPLOY_API_KEY`, variables `DOKPLOY_COMPOSE_ID` and
  `HEALTH_URL` (empty for services without HTTP). Deployment branches restricted to `main`. No required
  reviewer - shipping the release PR already is the approval.
- **Merging:** `gh pr merge <n> --squash --delete-branch --auto`; the repo settings *Allow auto-merge* and
  *Automatically delete head branches* are on.
- **Rulesets:** `main` - no deletion, no force-push, linear history, signatures, required check `check`.
  Tags `v*` - no deletion, no update.
- **Conventions:** conventional commits; all actions pinned by SHA and kept current by Dependabot (grouped).

## Requirements

### Must-have (P0) - skillforge

**P0-1 - release-please with the org App.**
- *Technique:* rename `skillsite-release-bot` to `skill-platform-release` and extend its installation to
  skillforge (permissions: contents, pull requests, issues - read and write). Client ID and private key
  survive the rename; skillsite's current `release.yml` only carries the old name as a `git config` display
  string until it adopts this flow. Then move its credentials to the org-level variable/secret named in the
  contract. New `release.yml`: job `release-please` mints a token with `actions/create-github-app-token` and
  passes it to `release-please-action`; **no step follows the action in this job**. Outputs: `release_created`,
  `tag_name`, `version`, `sha`.
- *Technique:* `release-please-config.json` with `release-type: python`, `include-component-in-tag: false`,
  `bump-minor-pre-major: true`, `changelog-sections` that show only `feat`, `fix`, `perf` and `revert` (a dry
  run on today's history listed 34 documentation entries, mostly spec ticks, next to 26 features and 12
  fixes) and `extra-files` for `uv.lock` (package `skillforge`) and `openapi.json`;
  `.release-please-manifest.json` starts at the current version (`0.3.0`, tag `v0.3.0` exists).
- *Acceptance criteria:*
  - [x] After a `feat`/`fix` commit lands on `main`, a release PR exists whose diff touches exactly
        `pyproject.toml`, `uv.lock`, `openapi.json`, `CHANGELOG.md` and the manifest. *(release PR #117)*
  - [x] The release PR's head commit is shown as *Verified* and its CI run (job `check`) executes and is green.
        *(#117, authored by `skill-platform-release[bot]`; green since the `dump_openapi.py` fix, #121)*
  - [x] `just openapi-check` and `uv sync --locked` pass on the release PR (proves both `extra-files` paths match).
  - [x] Merging the release PR (`git ship` or squash) creates tag `vX.Y.Z` and a GitHub release; the PR label
        becomes `autorelease: tagged`. *(#117, squash-merged: tag and release `v0.4.0` on the merge commit)*

**P0-2 - CI is a required check.**
- *Technique:* add `required_status_checks` with context `check` to the `main` ruleset (after P0-1, otherwise
  the release PR cannot be shipped). The job in [`ci.yml`](../../.github/workflows/ci.yml) is already named `check`.
- *Acceptance criteria:*
  - [x] Pushing a commit without a green `check` to `main` is rejected. *(rule active, confirmed through the rules
        API; the rejection itself was shown in the probe - never test it with a push to `main`)*
  - [x] `git ship` of a green PR and of a green release PR both succeed, as does the squash button. *(both live under
        the rule: #122 by `git ship` one minute after the rule was added, #117 by squash)*

**P0-3 - Build and publish hang off `release_created`.**
- *Technique:* in `release.yml`, the existing image job ([`build.yml`](../../.github/workflows/build.yml), tags
  `:vX.Y.Z`, `:sha-...`, `:latest`, `linux/amd64`) and the PyPI client jobs run with
  `if: needs.release-please.outputs.release_created == 'true'` and build from the released `sha`. The
  GitHub release is created by release-please; the separate `gh release create` job goes away. Image and
  client coordinates move from the release notes into the README.
- *Acceptance criteria:*
  - [x] A push to `main` that is not a release runs release-please only; build, publish and deploy are skipped.
        *(seen on the pushes of #118, #119 and #121)*
  - [x] A release produces the image `ghcr.io/nachhilfe-leon-weimann/skillforge:vX.Y.Z` and
        `skillforge-client==X.Y.Z` on PyPI, both built from the tagged commit. *(`v0.4.0`: the image also carries
        the tagged commit's `sha-` tag)*

**P0-4 - Deploy through the Dokploy API, verified.**
- *Technique:* `deploy.yml` (environment `production`, concurrency group `deploy-production`, never cancelled)
  runs one script: abort if the latest deployment is `running`; `POST compose.deploy` with a title and a
  description carrying version, SHA and run id; poll `deployment.allByCompose` until that deployment is
  `done` (continue) or `error` / `cancelled` / timeout (fail); then poll `HEALTH_URL` until `status` is healthy
  and `version` equals the released version, or time out (fail). No rollback. `workflow_dispatch` input:
  the version to expect - this is the manual re-run path. A second dispatch input, `dry_run`, only verifies API
  access. A failed deployment listing *while waiting* is retried until the deadline (one transient 502 must not fail a
  deploy that is still running); the first listing and the `POST` stay fatal.
- *Technique:* `SystemHealthCheckResponse` in [`schemas.py`](../../app/services/system/schemas.py) gains
  `version`, filled from the app's own version (`request.app.version`, which `main.py` sets from
  `get_project_version()`) and reported for every status; `just openapi` afterwards (decision J).
- *Technique:* remove the secret `DEPLOY_WEBHOOK_URL`, rotate the webhook token in Dokploy so the old URL is
  dead, and make sure Dokploy's own auto-deploy on push is off for the service.
- *Acceptance criteria:*
  - [x] A successful release ends with a green `deploy` job whose summary names the Dokploy deployment and
        the verified version. *(`v0.4.0`: about two and a half minutes from merge to verified prod)*
  - [x] A deployment that ends in `error` (e.g. failing `migrate` service) turns the workflow red. *(script level:
        `test_a_failed_deployment_fails_the_script_with_dokploys_message`; not yet seen live)*
  - [x] A healthy container that reports the *old* version turns the workflow red after the timeout. *(script
        level: `test_an_old_version_on_health_fails_after_the_timeout`; not yet seen live)*
  - [x] `deploy.yml` can be dispatched by hand for the current release and passes.

**P0-5 - Remove what the new flow replaces.**
- *Technique:* delete `version-bump.yml`, `codeql.yml` and `.github/codeql/`; drop the `check-release` logic
  and the webhook job from `release.yml`; remove the `just` recipes `create-version-bump`, `version-info`,
  `release-version`, `bump-version` and `scripts/version.py` if nothing else uses them. Switch on secret
  scanning and push protection in the repo settings (free for public repos) - both enabled 2026-09-20.
- *Acceptance criteria:*
  - [x] `.github/workflows/` contains `ci.yml`, `build.yml`, `release.yml`, `deploy.yml` - nothing else.
  - [x] `CLAUDE.md`, `README.md` and [`ARCHITECTURE.md`](../ARCHITECTURE.md) describe the new flow and
        point to this spec for the why.

### Nice-to-have (P1)

**P1-1 - Pin the deployed version.**
- *Technique:* [`compose.yml`](../../compose.yml) references `...:vX.Y.Z` with an `x-release-please-version`
  annotation on each `image:` line, and `release-please-config.json` lists the file as a `generic`
  `extra-files` entry: the release commit moves the pin together with the version. `:latest` stays as a
  convenience tag only; nothing deploys from it. `pull_policy: always` stays too - `Build` can be dispatched
  again for a tag, and a redeploy has to pick that image up.
- *Technique:* [`test_release_config.py`](../../tests/test_release_config.py) fails `check` when an `image:`
  line is unpinned or lost its annotation, when the services disagree on the version, or when the
  `extra-files` entry is gone - each of them a silent no-op in the release PR. It deliberately does not compare
  the pin with the project version: a rollback pins an older one.
- *Technique:* rollback procedure in the [README](../../README.md#rolling-back): restore the earlier tag's
  whole `compose.yml` in a `chore(deploy)` PR, merge it, dispatch `deploy.yml` with that version. Rolling
  forward reverts the rollback PR first, restoring the current `compose.yml`; the next release PR then only
  moves the pins. It rolls back the app only: an earlier image cannot run `alembic upgrade head` against a
  schema that is ahead of it.
- *Acceptance criteria:*
  - [x] `compose.yml` names one released version on every `image:` line. *(`v0.4.0`, the image prod already
        runs - the PR itself deploys nothing)*
  - [x] release-please rewrites exactly those lines. *(see Verified behavior: its own `Generic` updater, run
        against the file)*
  - [x] After a release, `compose.yml` on `main` names the released version and prod runs exactly that image.
        *(`v0.5.0`, 2026-09-25: the release PR moved both `image:` lines, and the release run's deploy job went
        green, which it only does once `/health` reports the released version)*

**P1-2 - Local hooks.** *Dropped on 2026-09-20 - see Non-goals.* (Was: lefthook with `pre-commit`, `commit-msg`
and `pre-push` hooks calling `just` recipes.)

**P1-3 - Deploy notification.** *Dropped on 2026-09-20 - see Non-goals.* (Was: one Discord message per deploy
result.) skillbot's existing deploy notification therefore goes away when it adopts this flow.

**P1-4 - History convention in `CLAUDE.md`.** Decision D as a short rule set, including "one PR per slice" and
"fold `docs(specs): tick` commits into the slice". *Done: the "History on `main`" rule under Conventions in
[`CLAUDE.md`](../../CLAUDE.md).*

### Future considerations (P2)

- **Shared workflows.** *Done 2026-09-20:* `deploy.yml` and the deploy script moved into the public repo
  [`skill-platform-workflows`][workflows] (`v1.0.0`; a public repo cannot call workflows from a private one),
  together with `triage.yml` ([`project-intake.md`](project-intake.md)); this repo calls them with
  `uses: ...@v1` (#133). The script's tests moved with it. Extracted before the second adopter rather than on
  the third: skillbot and skillsite then start from the callers and never get a copy to keep in sync.
  - [x] The shared `deploy.yml` runs the script of the ref it was called at: it checks out
        `job.workflow_repository` at `job.workflow_sha` - documented by GitHub, unknown to actionlint 1.7.12
        (one `ignore` in the shared repo's `actionlint.yaml`).
  - [x] A `dry_run` dispatch of `Deploy` on `main` is green: the called workflow gets the caller's `production`
        environment (secret and variables) and the org variable, and finds its script. *(2026-09-20, right
        after #133: the job checked out `skill-platform-workflows` at the ref behind `v1` and the script
        reported "Dokploy API access and the compose service's deployment list verified; nothing deployed")*
  - [x] The next release deploys through it. *(`v0.5.0`, 2026-09-25: the release run's
        `Deploy / Deploy production via Dokploy API` job called the shared workflow and passed)*
- **Code scanning default setup**, if alerts start being read.
- **A required reviewer on `production`**, if someone other than Leon ever ships.

## Adoption in the other repos

Order: skillforge (this spec) -> skillbot -> skillsite. What is identical comes from the shared repo (see the
platform contract): a new adopter copies the two callers from its README and `ci.yml`, `build.yml`,
`release.yml` and the release-please config from here, then adapts those to its stack.

- **skillsite:** `release-type: node` on the root `package.json`; replaces its manual `release.yml` (bot commit
  + tag). Already has the App. Needs the `/health` `version` field and a job named `check`.
- **skillbot:** `release-type: python`; replaces `build-deploy.yml`; the dev deploy, both webhooks and the Discord
  deploy notification go away; actions get pinned. No HTTP health endpoint -> deployment status only.
  *Adopted 2026-09-21 (skillbot#10):* `ci.yml` and `release.yml` as here, `build.yml` with one differing line
  (`file: Dockerfile`), `deploy.yml` identical. Its vendoring went with it (see *Decided*), which is what made
  the copy that close. Without `/health`, a pin in `compose.yml` that stops moving would be a *green* deploy of
  the old image - there `test_release_config.py` is the only backstop, not one of two.
  - [x] The first push to `main` lets the App open a release PR whose diff touches exactly `pyproject.toml`,
        `uv.lock`, `compose.yml`, `CHANGELOG.md` and the manifest; its commit is *Verified* and its `check` is
        green. *(skillbot#12, `0.2.0`: no tag existed, so it collects the whole history)*
  - [x] A `dry_run` dispatch of `Deploy` is green against skillbot's compose service. *(2026-09-21)*
  - [x] The first release builds `ghcr.io/nachhilfe-leon-weimann/skillbot:vX.Y.Z` and deploys it. *(`v0.1.0`,
        2026-09-21: the image `v0.1.0` is published and the release run's deploy job is green)*

## Open questions

- **Dokploy API key scope.** The key acts as the user who created it. If the instance allows a restricted
  user, create one for deployments; otherwise accept the broader key as an environment secret.

## Decided (formerly open questions)

- **First release PR:** it collects everything since `v0.3.0` (CRM P0 + P1) into `0.4.0` and is not trimmed by
  hand. `changelog-sections` keeps it to what a consumer cares about: 26 features and 12 fixes instead of an
  additional 34 documentation entries.
- **No ADR.** This spec is the decision record; a separate ADR would repeat *Decided defaults* in prose.
- **Squash via `gh pr merge <n> -sd --auto` is the default merge path** (decisions C and G). `git ship` stays
  allowed but needs a green tip; once the required check was live it failed in daily use, which settled it.
- **No local hooks and no deploy notifications** - P1-2 and P1-3 were dropped on 2026-09-20; P1 is P1-1 and P1-4.
- **skillbot's Dokploy target** (decided at adoption, 2026-09-21): a repo `compose.yml` with the pinned image and
  a Dokploy *compose* service on it - the uniform option. The deploy script did not have to learn
  `application.deploy`.
- **skillbot's two lockfiles** (decided at adoption, 2026-09-21): the split is gone. skillbot vendored skillcore
  (`vendor/`, a second lock `uv.lock.prod` made with `--no-sources`, the real `uv.lock` git-ignored), and the
  tracked lock had gone stale unnoticed - it knew neither `skillforge-client` nor skillcore 0.2.0, so its image
  could not start. skillbot now takes skillcore from its git tag exactly as this repo does: one tracked `uv.lock`,
  one `extra-files` entry, `--locked` in CI and in the image. Rule for the next adopter: bring the repo in line
  with this one first, then copy the workflows - do not build the flow around legacy structure.

## Success metrics

- *Manual steps per release:* **1** (ship the release PR). Today: 3 (dispatch bump, merge bump PR, watch webhook).
- *Flows to remember:* **1** for three repos. Today: 3.
- *Silent deploy failures:* **0** - every failed deployment or version mismatch is a red workflow.
- *Workflow files in skillforge:* 4 (today 5), with roughly half the YAML.

## Timeline / phasing

One PR per requirement, `just check` green on each:

1. **`/health` reports `version`** (the app half of P0-4) - independent, and the first new-flow deploy needs it.
   *Done: #115.*
2. **P0-1 + P0-3 together** - the new `release.yml` replaces the old one in a single PR, keeping the webhook
   deploy job for now. They cannot be staged: the old version-driven check would find the GitHub release that
   release-please created seconds earlier and skip build and deploy. *Done: #116.*
3. **P0-2** - required check (settings only, no PR), once a release PR from the App has shown a green `check`.
   *Done 2026-09-20 (verified through the rules API, never with a probe push).*
4. **P0-4** - deploy script and `deploy.yml`; `release.yml` switches from the webhook to it. *Done: #118.*
5. **P0-5** - cleanup and docs. *Done: #119, plus #121 (the `openapi.json` round-trip fix).* Then merge the
   release PR #117: the first real release through the new flow. *Done 2026-09-20: `v0.4.0`.*
6. **P1-1 and P1-4** as independent follow-ups (P1-2 and P1-3 are dropped). *P1-1: #126, confirmed by the next
   release. P1-4: #125.*
7. **Shared workflows (P2)** - before the other repos adopt the flow, so that they start from the callers.
   *Done: `skill-platform-workflows` `v1.0.0`, #133.* Then skillbot (*adopted 2026-09-21, skillbot#10*), then
   skillsite.

**Dependency:** Leon extends the App installation and creates the org variable/secret, the `production`
environment and the Dokploy API key - these cannot be done from a PR.

## Rules for implementing agents

- Follow [`CLAUDE.md`](../../CLAUDE.md): English only, symbol references instead of line numbers, never
  hand-edit `openapi.json`, `just check` green before every commit, conventional commits.
- Pin every action by full SHA with a version comment, as the existing workflows do.
- Never print or echo secrets; pass the Dokploy API key through a curl config file, not the command line.
- Do not add rollback, digest pinning or attestation logic - they are non-goals, not omissions.
- Repo settings, rulesets, the App installation and Dokploy configuration are changed by Leon, not by an
  agent; describe the exact setting in the PR instead.
- Tick the acceptance checkboxes in this file in the PR that fulfils them and flip the status line when P0 is done.

[workflows]: https://github.com/Nachhilfe-Leon-Weimann/skill-platform-workflows
