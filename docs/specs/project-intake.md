# Spec: Project intake (issues and PRs land on the board with module, assignee and the iteration they closed in)

> Status: In progress - P0-0, P0-1 (#128) and P0-1a (#130) done and live in skillforge since 2026-09-20; P0-2
> half done (skillbot has the caller since 2026-09-21, skillbot#11; skillsite open) and P0-3 (retire the
> built-in auto-add) open. P2 *Shared workflow* done: the workflow
> lives in [`skill-platform-workflows`][workflows] since 2026-09-20, the repos keep a caller (#133).
> Tracking: [#127](https://github.com/Nachhilfe-Leon-Weimann/skillforge/issues/127)
> Platform arc (skillforge first, then skillsite and skillbot), same shape as
> [`release-flow.md`](release-flow.md): written here because skillforge is the first adopter; the **platform
> contract** below is what the other repos copy.
> This spec is also the decision record (no separate ADR).

## Problem statement

Planning happens on one org-wide board, the
[skill-platform project](https://github.com/orgs/Nachhilfe-Leon-Weimann/projects/2). Five things are still done
by hand - or forgotten - on every issue and every PR, in every repo:

- **Putting it on the board.** GitHub's built-in *Auto-add to project* workflow covers one repository per
  workflow, and the number of such workflows depends on the plan of the project's **owner**: one on Free. The
  project belongs to the org, the org is on Free, and that one workflow is already in use. The maintainer's personal
  Pro plan does not count.
- **Assigning it.** An item without an assignee is invisible in every "assigned to me" view. With one developer
  the answer is almost always "whoever opened it".
- **Setting its Module.** The board is sliced by the single-select field *Module* (`forge`, `bot`, `site`,
  `core`). The value follows from the repo, yet it is picked by hand - and an item without it drops out of
  every per-module view.
- **Recording when an item was finished.** The board runs in two-week iterations. An issue or a PR that closes
  should sit in the iteration it closed in, whatever it was planned for - otherwise an iteration's view does
  not show what was actually delivered in it.
- **Giving an issue its type.** Task, Bug, Feature, Epic: GitHub cannot make the issue type mandatory, so an
  issue opened in a hurry stays without one, and nothing points it out.

An item that is forgotten is simply missing from the board, and nothing notices.

## Goals

1. **Every issue and every PR lands on the board** without a manual step, in all three platform repos.
2. **An item nobody is assigned to gets its author as assignee.** An assignee set on purpose is never touched.
3. **Every item has its Module.** The repo's module is set on intake; a Module set on purpose is never touched.
   Which module a repo stands for is configuration, not code.
4. **A closed issue or PR sits in the iteration it closed in.**
5. **An issue without a type does not go unnoticed:** closing it asks for one.
6. **One flow, three repos, one file.** Same file name, same job names, same org variable and secret - the
   platform contract of `release-flow.md` applies - and **the file itself is identical in every repo**, so it
   can move into a shared platform repo unchanged.
7. **No new credential.** The org App that already opens the release PRs does this too.
8. **It never gets in the way.** The workflow is not a required check, and an item it cannot handle (an author
   who cannot be assigned, a bot) leaves a green run, not a red one.

## Non-goals

- **Setting the other project fields** (Status, Priority, Effort). The project's built-in *Item added to
  project* workflow already sets the status; the rest is planning, not intake. Module and the iteration of a
  closed item are the exceptions because they are not judgements: one follows from the repo, the other from the
  calendar.
- **Iteration planning.** Which iteration an *open* item is planned for stays a manual decision.
- **Setting labels, reviewers, milestones or the issue type.** Which type an issue has is a judgement; the
  workflow only points out that it is missing (decision L).
- **Assigning bot-authored PRs.** A bot cannot be an assignee; the release PR and Dependabot PRs stay unassigned.
- **A GitHub plan upgrade, or a personal access token.** A PAT is tied to the maintainer's account and expires.
- **A third-party action for the assignment.** It is one REST call.
- **The shared platform repo itself.** [`skill-platform-workflows`][workflows] came out of the P2 of
  `release-flow.md` and holds `triage.yml` since 2026-09-20. This spec did not build that repo; it made sure the
  file was ready for it: nothing in it is repo-specific (decision J).
- **skillcore and the `student-*` repos.** skillcore is private: on the Free plan org variables and secrets do
  not reach private repos, and the App is installed on selected repos only. It may adopt this later with
  repo-level copies of the variable and the secret.

## Decided defaults

| Topic | Decision | Rationale |
|---|---|---|
| **A - Mechanism** | One workflow per repo, **`triage.yml`**, with three independent jobs: **`board`** (add to the project, set the Module, move a closed item into the current iteration), **`assign-author`** and **`issue-type`** (decision L). | Independent on purpose: a broken App token must not stop the assignment or the reminder, and the other way round. Everything that needs the App token and the project item lives in one job, so the token is minted once per event. |
| **B - Adding** | [`actions/add-to-project`](https://github.com/actions/add-to-project) (GitHub's own action, pinned by SHA), pointed at the project URL. | Maintained by GitHub, one input besides the token. Adding an item that is already on the board is a no-op, so it can run next to the built-in workflow. |
| **C - Token for the board** | An installation token of the org App **`skill-platform-release`**, minted with `actions/create-github-app-token` and narrowed to `permission-organization-projects: write` plus read on issues and pull requests (the action resolves the item it adds). Credentials are the existing org variable `RELEASE_APP_CLIENT_ID` and org secret `RELEASE_APP_PRIVATE_KEY`. | `GITHUB_TOKEN` cannot reach an org project at all. The App, its key and both names already exist in every platform repo. This widens the App's role - see *Trade-offs accepted*; decision E in `release-flow.md` is amended accordingly. |
| **D - Token for the assignment** | The workflow's own `GITHUB_TOKEN` with `issues: write` and `pull-requests: write` on that job only; top-level `permissions: {}`. | Assigning is a repo-level write; it needs neither the App nor a secret. |
| **E - Triggers** | `issues` and `pull_request_target`, each with `opened`, `reopened` and `closed`. | `pull_request_target` runs the workflow from `main` and has the secret even for a PR from a fork (the repos are public). An issue transferred into a repo arrives as `opened`. `reopened` is the cheap second chance for an item whose first run failed. `closed` drives decisions K and L; `assign-author` skips it. |
| **F - What is added** | Everything, including bot-authored PRs (release PR, Dependabot). | That is what the board holds today: release PR #117 and five Dependabot PRs are on it. Merged items move to *Done* and are auto-archived by the project's built-in workflows, so they do not pile up. |
| **G - Who is assigned** | The author, if and only if the item has **no assignee at the moment the job runs** and the author is a user (not a bot) who can be assigned in that repo. Anything else: do nothing, stay green. | "Nobody assigned" is read from the API at run time, not from the event payload, so an assignee picked while creating the item or seconds after it always wins. Assignability is checked first (`GET /repos/{owner}/{repo}/assignees/{login}`), so an outside contributor does not produce a failed run. |
| **H - Built-in auto-add** | Switched **off** once `triage.yml` is proven in the repo it covers. *Auto-add sub-issues to project* stays on. | One mechanism per concern; a second, plan-limited one that covers a single repo is a trap for the next person who wonders why one repo behaves differently. *Auto-add sub-issues* is a different concern and not plan-limited: it follows the parent issue, not the repo. It covers what `triage.yml` cannot see - an existing issue that is attached to an epic later (no `opened` event), and sub-issues in repos without the workflow - and adding twice is a no-op. Its one gap: an item that reaches the board only this way gets no Module. |
| **I - Not a gate** | `triage.yml` is never added to the required checks of the `main` ruleset. | A GitHub API hiccup must not block a merge. A red run is visible in the Actions tab and costs one click on the board. |
| **J - Module** | The `board` job sets the project's single-select field **Module** to the repo's module, **only when the field is empty**. The module is the **repository variable `PROJECT_MODULE`** (skillforge: `forge`), given by option *name*; project, field and option ids are looked up at run time from the item the action returns. An unset variable or a name the project does not know fails the run. | Same rule as for the assignee: a value chosen by hand wins, e.g. a forge issue that really belongs to `core`. A name survives a recreated field; an opaque option id does not. A repository variable instead of a line in the file, because the file is about to move into the shared platform repo: a caller's workflow-level `env` is **not** passed on to a called workflow, while GitHub's own advice for values shared across workflows is the `vars` context. With the variable the file has no repo-specific line at all (goal 6). The price is configuration that no diff shows - which is why a missing or wrong value must be a red run, not an item without a Module. |
| **K - Iteration** | When an **issue or a PR is closed** - whatever the reason: merged or not, completed or not planned - the `board` job sets the project's **Iteration** field to the current iteration: the one whose `startDate <= today < startDate + duration`, with *today* taken in `Europe/Berlin`. It **replaces** an existing value. If no iteration covers today (a gap, or none planned), the run leaves the field alone, prints a notice and stays green. | Unlike Module and assignee this is not a default but a fact: the iteration an item closed in. Something planned for iteration 12 that closes in 13 belongs to 13. Every close reason counts - the board moves the item to *Done* just the same, and a rule with exceptions would need someone to remember them. Berlin time, because the iterations are planned in it: a PR merged at 00:30 belongs to the day the maintainer saw on the clock. A gap between iterations is a legitimate state, not an error. (The first version only listened to closed PRs; the first live test, #129, showed that issues were the missing half.) |
| **L - Missing issue type** | When an **issue** is closed and has no issue type at that moment, the `issue-type` job posts one comment asking for it. The comment carries a hidden marker, so closing the same issue again does not ask twice. It uses `GITHUB_TOKEN` (`issues: write`), reads the type at run time and never sets a type itself. | The type cannot be made mandatory and cannot be derived. Closing is the last moment somebody looks at the issue anyway, and a comment reaches author and assignee as a notification without blocking anything. On close rather than on open, because an issue is often filed in a hurry and typed later - asking at once would be noise. |

## Platform contract

Identical in every adopting repo:

- **Workflow:** the work is `triage.yml` in [`skill-platform-workflows`][workflows]: jobs `board`, `assign-author`
  and `issue-type`, top-level `permissions: {}`, every action pinned by full SHA with a version comment
  (Dependabot keeps them current). Its workflow-level `env` holds `PROJECT_URL`
  (`https://github.com/orgs/Nachhilfe-Leon-Weimann/projects/2`) and `MODULE: ${{ vars.PROJECT_MODULE }}`.
- **Caller:** `.github/workflows/triage.yml` in every repo - **identical in every repo**: the triggers of
  decision E, top-level `permissions: {}`, one job that calls the shared workflow `@v1` with `secrets: inherit`
  and grants `issues: write` and `pull-requests: write` (a called workflow only gets the permissions its caller
  grants; decision D needs these two). The README of the shared repo has the file to copy.
- **Repository variable `PROJECT_MODULE`:** the repo's option of the project field *Module* - `forge`, `site`,
  `bot`. The only thing that differs between the repos.
- **Project fields the workflow relies on, by name:** *Module* (single select) and *Iteration* (iteration).
- **Org-level (existing):** variable `RELEASE_APP_CLIENT_ID`, secret `RELEASE_APP_PRIVATE_KEY`.
- **App `skill-platform-release`:** installed on the repo, with the organization permission *Projects: read and
  write* accepted on the org installation.
- **`pull_request_target` hygiene:** the workflow never checks out code and never interpolates event data into
  a script. Values from the event (number, author login) reach `run:` steps through `env:` only. The shared
  repo guards this with its tests ([`test_workflows.py`][workflows-tests]); each repo
  guards that its `triage.yml` stays a caller - no steps of its own - with a test next to its other config tests
  (skillforge: [`test_triage_workflow.py`](../../tests/test_triage_workflow.py)).

## Verified behavior

Checked on 2026-09-20 against the live org (read-only API calls).

| Question | Result |
|---|---|
| Which plan counts for the built-in auto-add limit? | The org is on **Free** (`GET /orgs/...`: `plan.name = free`). Project #2 has exactly one *Auto-add to project* workflow, enabled - the Free allowance. |
| Does the App have the project permission? | **Yes, since the evening of 2026-09-20** - and it took two steps. Declaring `organization_projects: write` on the App (`GET /apps/skill-platform-release`) changed nothing for the org: the installation kept listing only `contents`, `issues`, `pull_requests` (write) and `metadata` until the new permission was **accepted on the installation** (org settings -> GitHub Apps -> `skill-platform-release` -> review request). `GET /orgs/.../installations` is the check that counts. |
| Do the credentials reach the repos? | `RELEASE_APP_CLIENT_ID` and `RELEASE_APP_PRIVATE_KEY` are org-level with visibility `all`. On Free that means all **public** repos: skillforge, skillsite, skillbot. |
| What is on the board today? | 80 items from skillforge, skillsite, skillbot and skillcore, among them Dependabot PRs and the release PR. Hence decision F. |
| Does the Module logic work against the real project? | **Yes.** The lookup query and the mutation of `triage.yml` were run verbatim (with a user token) against the item of #127: the field was empty, `forge` resolved to its option id, an unknown name resolved to nothing (the red branch), the mutation set the field, and the read-back returned `forge` (the "already set" branch). Project, field and option ids all come out of the one lookup by item id. |
| Does the iteration logic work against the real project? | **Yes.** The lookup and the mutation were run verbatim against the item of PR #128. The field has 14-day iterations; the filter picks *Iteration 13* for 2026-09-13 to 09-26 and *Iteration 14* from 09-27, is right across the end of daylight saving time (10-24 -> 15, 10-25 -> 16), and returns nothing before the first and after the last planned iteration (the notice branch). `configuration.iterations` lists the running and the upcoming iterations; completed ones are a separate list and are not needed. |
| Do the assignment calls behave as decision G assumes? | **Yes.** `GET .../issues/{number}` answers for a PR number too; `GET .../assignees/{login}` exits 0 for the maintainer and fails with `HTTP 404` for an outside account - the text the script matches on. |
| Can the workflow be tried before it is on `main`? | **No.** `issues` and `pull_request_target` run the workflow file of the default branch, so opening the PR that adds `triage.yml` does not trigger it. Its merge might: `closed` fires after the merge, when `main` already has the file. Otherwise the first issue or PR opened after the merge is the live test. |
| Does it work live? | **Yes, since #128 landed on 2026-09-20.** Merging #128 was the first run (`closed` fires after the merge, when `main` has the file): *Module is already set to "forge"*, *Moved into "Iteration 13"*. The test issue #129 was on the board seconds after it was opened: *Module set to "forge"* and assigned to its author. Both runs used the **repo-scoped App token narrowed to `organization-projects: write`** - it can add to the org project and write its fields. Closing #129 started no run and left its iteration empty: `issues` did not listen to `closed` yet (now decision K). |
| How does the API show a missing issue type? | `GET /repos/{owner}/{repo}/issues/{number}` carries `"type": null`; with a type it is an object with `name`. The reminder's calls were run against #129: comment posted, found again through its marker, deleted. |
| In the shared platform repo: is `vars.PROJECT_MODULE` inside the *called* workflow the **calling** repo's variable? | **Yes** (live, the merge of #133). The run belongs to the caller: `vars`, `github.event` and the App token's repository are the caller's. No `module` input is needed. |
| Action versions | `actions/add-to-project` v2.0.0 (`5afcf98fcd03f1c2f92c3c83f58ae24323cc57fd`), `actions/create-github-app-token` v3.2.0 (`bcd2ba49218906704ab6c1aa796996da409d3eb1`, the pin `release.yml` already uses). |

**Not verified yet:**

- Whether a `pull_request_target` run **triggered by Dependabot** receives the org Actions secret. GitHub treats
  Dependabot runs like fork runs for `pull_request` (no Actions secrets, only Dependabot secrets); its docs are
  not explicit for `pull_request_target`. See *Open questions*.

## Requirements

### Must-have (P0)

**P0-0 - Accept the App permission (the maintainer, settings only).**
- *Technique:* accept *Projects: read and write* on the org installation of `skill-platform-release`.
- *Acceptance criteria:*
  - [x] `GET /orgs/Nachhilfe-Leon-Weimann/installations` lists `organization_projects: write` for the App.
        *(accepted 2026-09-20)*

**P0-1 - `triage.yml` in skillforge.**
- *Technique:* the workflow described by decisions A-G, J and K. `board`: mint the App token, run the action,
  then set the Module: fail if `PROJECT_MODULE` is unset, one GraphQL lookup by the returned item id (current
  value, project id, field id, options), stop if a value is set, fail if the name matches no option, else
  `updateProjectV2ItemFieldValue`. On `closed` a further step looks up the Iteration field's
  `configuration.iterations`, picks the one that covers today and writes it the same way. Re-running the
  action on `closed` is how the step gets the item id - and it puts a PR on the board that never was.
  `assign-author`: skip when the author's `type` is `Bot`; otherwise read the item through
  `GET /repos/{owner}/{repo}/issues/{number}` (PRs are issues for this endpoint), stop if `assignees` is not
  empty, stop if the author is not assignable, else `POST .../issues/{number}/assignees`. `gh api` with
  `GH_TOKEN: ${{ github.token }}` is enough; no checkout, no script file.
- *Technique (the maintainer or an agent with repo admin):* `gh variable set PROJECT_MODULE --body forge`.
- *Technique:* [`test_triage_workflow.py`](../../tests/test_triage_workflow.py) guards the hygiene rules of the
  platform contract as part of `just check`: no checkout, no `${{ }}` inside a `run:` block, top-level
  `permissions: {}`, every action pinned, the narrowed App token, and `MODULE` coming from `vars.PROJECT_MODULE`
  (no module name in the file).
- *Technique:* docs in the same PR - the `.github/` line of the layout block in [`CLAUDE.md`](../../CLAUDE.md)
  and a pointer from the *Platform contract* of `release-flow.md` to this spec.
- *Acceptance criteria:*
  - [x] `actionlint` (with `shellcheck`) passes on `triage.yml`; the hygiene tests are part of `just check`.
  - [x] The token step requests `organization-projects: write`, `issues: read` and `pull-requests: read` and
        nothing else; the workflow has top-level `permissions: {}` and no checkout step. *(enforced by the tests)*
  - [x] The Module, iteration and assignment calls do what decisions G, J and K say. *(run by hand against the
        live project, see Verified behavior; #127 got its `forge` and #128 its iteration that way)*
  - [x] The repository variable `PROJECT_MODULE` is `forge`. *(set 2026-09-20)*

  Live (the workflow only runs from `main`):
  - [x] A new issue without an assignee is on the board, Module `forge`, assigned to its author within a minute.
        *(#129: on the board and assigned 9 seconds after it was opened)*
  - [x] A new PR from a branch of the repo: same. *(#130, the PR of P0-1a, was opened without an assignee and not
        put on the board by hand: on the board, Module `forge`, author assigned)*
  - [ ] An item created **with** an assignee keeps exactly that assignee; an item whose Module was changed by
        hand keeps it after a close and reopen.
  - [ ] The release PR (author `skill-platform-release[bot]`) is on the board with its Module, unassigned, and
        both jobs are green.
  - [x] An item that is already on the board leaves a green run and no duplicate. *(#128 had been added by
        hand; its merge run found the item, and the PR still has exactly one)*
  - [x] A merged PR sits in the current iteration afterwards. *(#128: "Moved into Iteration 13")* A PR closed
        without merging has not been tried.

**P0-1a - Closed issues: iteration and type reminder.**
- *Technique:* `issues` also listens to `closed` (decision E), so the iteration step of `board` covers issues
  (decision K). New job `issue-type` (decision L): read `type` through `GET .../issues/{number}`, stop if it is
  set, stop if a comment with the marker `<!-- triage:missing-issue-type -->` exists, else post the comment.
- *Acceptance criteria:*
  - [x] `actionlint` passes; the hygiene tests cover the new `run:` block without a change.
  - [x] The reminder's calls do what decision L says. *(run by hand against #129, see Verified behavior)*

  Live:
  - [x] A closed issue sits in the current iteration afterwards. *(#129, reopened and closed after #130 landed:
        "Moved into Iteration 13"; #130 itself moved there on its merge.)* An iteration that was already planned
        is replaced. *(confirmed live by the maintainer, 2026-09-20 - and wanted: the moment of closing decides; whoever
        means an older iteration corrects it after closing)*
  - [x] An issue closed without a type gets a comment. *(#129, closed with its type removed: one comment from
        `github-actions`)* A second close does not ask again, and a typed issue gets none. *(both confirmed live
        by the maintainer, 2026-09-20)*

**P0-2 - Roll out to skillsite and skillbot.**
- *Technique:* set the repository variable `PROJECT_MODULE` (`site`, `bot`) and make sure the App is installed
  on the repo, then copy the caller `triage.yml` and its test from skillforge verbatim. One PR per repo, tracked
  by an issue in that repo. (Written before the shared platform repo existed, when the copy would have been the
  whole workflow.)
- *Acceptance criteria:*
  - [ ] The first two live criteria of P0-1 hold in skillsite (Module `site`) and in skillbot (Module `bot`).
        *(skillbot: the caller is on `main` since skillbot#11, 2026-09-21, with `PROJECT_MODULE` = `bot`; its
        merge was the first run - "Module is already set to "bot"", "Moved into "Iteration 13"". A new issue and
        a new PR there are the remaining live test. skillsite: open.)*
  - [ ] `diff` between the three `triage.yml` files is empty. *(skillforge and skillbot: empty, and the caller's
        test is identical too; skillsite: open)*

**P0-3 - Retire the built-in auto-add (the maintainer, settings only).**
- *Technique:* in the project's workflow settings, switch off *Auto-add to project* (decision H). Only after
  P0-1 has been green for the repo that workflow covers.
- *Acceptance criteria:*
  - [x] The project lists *Auto-add to project* as disabled. *(switched off 2026-09-20; *Auto-add sub-issues to
        project* stays on, decision H)*
  - [ ] A new issue in each of the three repos still lands on the board. *(skillforge: yes. skillsite and
        skillbot only once P0-2 is done - the built-in workflow covered one repo, and whichever it was has no
        automatic intake until then.)*

### Nice-to-have (P1)

**P1-1 - One-off backfill.** Open issues and PRs that never made it onto the board are added once with
`gh project item-add 2 --owner Nachhilfe-Leon-Weimann --url <url>` over `gh issue list` / `gh pr list` of the three
repos. A command in the PR description, not a workflow. *Criterion:* no open issue or PR of the three repos is
missing from the board.

### Future considerations (P2)

- **Shared workflow.** *Done 2026-09-20 (#133):* `triage.yml` moved into the central public platform repo
  [`skill-platform-workflows`][workflows] together with `deploy.yml` (`release-flow.md`, P2). It has `workflow_call`
  as its only trigger, and the repos keep a caller with `secrets: inherit`. Because of decision J nothing else
  changed: the jobs moved as they were and `PROJECT_MODULE` stays where it is. The hygiene tests moved with the
  file; this repo's test now guards the caller.
  - [x] Live: the first run from `main` is green and names the Module - which proves that
        `vars.PROJECT_MODULE` inside the called workflow is the calling repo's variable. *(the merge of #133,
        the first run through the caller: "Module is already set to "forge"", "Moved into "Iteration 13"" -
        the shared repo has no such variable, and an empty one is a red run)*
- **skillcore:** adopt with repo-level variable and secret, once the App is installed there.

## Trade-offs accepted

- **The release App is no longer single-purpose.** `release-flow.md` decided "one App per role; it does releases
  only". A second App would mean a second private key, a second pair of org credentials and a second
  installation to keep in sync, for a workflow that needs one org permission. The role is widened to *platform
  repo automation* instead, and every workflow narrows its token to the permissions it needs
  (`permission-*` inputs), so the intake token cannot write contents and the release token cannot write projects.
- **The App's private key is now used by a workflow that outsiders can trigger** (anyone can open an issue on a
  public repo). `issues` and `pull_request_target` runs execute the workflow file from `main`, the workflow
  checks out nothing and passes no event data into a shell. The key is read by one pinned first-party action.
  This is the reason for the hygiene rule in the platform contract - it is not optional.
- **Three copies of one caller.** The work is one file in the shared platform repo; what each repo copies is
  the caller, a dozen lines without logic. P0-2's empty `diff` keeps the copies honest.
- **Configuration outside the repo.** `PROJECT_MODULE` lives in the repo settings: not versioned, not reviewed,
  invisible in a diff. Accepted for a file without repo-specific lines; the red run on a missing or unknown
  value is the safety net.
- **The iteration of a closed item is overwritten.** Whoever parks a closed issue or PR in another iteration on
  purpose has to do it after closing. An item that is closed, reopened and closed again ends up in the
  iteration of the last close.
- **The type reminder is a comment, not a gate.** It can be ignored, and it adds one bot comment to the issue.
  Accepted: GitHub offers no way to require a type, and a notification at the moment of closing is the least
  intrusive thing that still gets seen.
- **A failed intake stops nothing.** Not being a required check (decision I) means a red `triage` run is only
  seen by someone who looks. The cost of a miss is one manual click; the cost of a gate would be blocked merges.

## Open questions

- **Dependabot PRs and the secret.** If a Dependabot-triggered `pull_request_target` run does not get
  `RELEASE_APP_PRIVATE_KEY`, the `board` job fails on every Dependabot PR. Two ways out: mirror the key as an
  org **Dependabot** secret, or skip the `board` job for `dependabot[bot]` and drop Dependabot PRs from
  decision F. Decide on the first Dependabot PR after P0-1; prefer skipping - a second copy of the key is worse
  than a bump PR that is not on the board.

## Timeline / phasing

1. **P0-0** - The maintainer accepts the permission. Blocks everything else. *Done 2026-09-20.*
2. **P0-1** - one PR in skillforge. The workflow only runs once it is on `main`, so the live criteria are ticked
   with the first issue and PR after the merge.
3. **P0-2** - one PR each in skillsite and skillbot, independent of each other.
4. **P0-3** - settings, once P0-1 is proven. **P1-1** any time after P0-2.

## Rules for implementing agents

- Follow [`AGENTS.md`](../../AGENTS.md): English only, symbol references instead of line numbers, conventional
  commits, `just check` green before every commit, a slice as one PR or a small stack (_PR size_).
- Pin every action by full SHA with a version comment, as the existing workflows do.
- Keep this repo's `triage.yml` a caller. The workflow itself is changed in `skill-platform-workflows` - and
  there: never add a checkout step, and never use `${{ github.event.* }}` inside a `run:` block.
- App permissions, the org installation and the project's workflow settings are changed by the maintainer,
  not by an agent; describe the exact setting in the PR instead.
- Tick the acceptance checkboxes in this file in the PR that fulfils them and flip the status line when P0 is done.

[workflows]: https://github.com/Nachhilfe-Leon-Weimann/skill-platform-workflows
[workflows-tests]: https://github.com/Nachhilfe-Leon-Weimann/skill-platform-workflows/blob/main/tests/test_workflows.py
