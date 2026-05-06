# Agent Workflow Guide

This document explains how to run the structured agent flow using role-based skills.
Claude does not provide a built-in workflow engine — you define the sequence and
invoke each role explicitly.

---

## Design Principle: Stay Idle, Save Context

Agents that produce artifacts other agents depend on **remain idle after completing
their primary task**. This eliminates context-window startup costs between roles:

- The **architect-reviewer** stays idle so the planner can ask clarifying questions
  during planning without re-reading the entire architecture review from scratch.
- The **architect-planner** stays idle so the developer can ask plan questions during
  implementation and so the planner can perform the plan-compliance check when the
  developer is done.
- The **sprint-planner** stays idle for the same reason — it owns compliance for
  sprint plans it produced.

Handoffs between phases are signals ("finished"), not context restarts.

---

## Workflow Diagram

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│                           WORKFLOW OVERVIEW                                  │
└─────────────────────────────────────────────────────────────────────────────┘

  ┌──────────────────┐
  │  User Request    │
  └────────┬─────────┘
           │
           ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  PHASE 1: ARCHITECTURE                                                       │
│                                                                              │
│  ┌─────────────────────┐                                                     │
│  │  architect-reviewer │ ──▶ ARCHITECTURE_REVIEW.md                          │
│  └──────────┬──────────┘                                                     │
│             │ [IDLE — available for planner Q&A]                             │
└─────────────┼────────────────────────────────────────────────────────────────┘
              │ ▲ planner may ask questions
              │ │
              ▼ │
┌──────────────────────────────────────────────────────────────────────────────┐
│  PHASE 2: PLANNING                                                           │
│                                                                              │
│  Single feature?                   Sprint / postmortem findings?             │
│                                                                              │
│  ┌─────────────────────┐           ┌─────────────────────┐                  │
│  │  architect-planner  │           │   sprint-planner    │                  │
│  │  plan/<feature>.md  │           │  plan/sprint_X.md   │                  │
│  └──────────┬──────────┘           └──────────┬──────────┘                  │
│             │                                 │                              │
│             └──────────────┬──────────────────┘                              │
│                            ▼                                                 │
│                  ┌─────────────────┐                                         │
│                  │  plan-reviewer  │  (pre-implementation gate)              │
│                  └────────┬────────┘                                         │
│                           │                                                  │
│  [Planner IDLE — Q&A + plan-compliance check when dev says "finished"]      │
└───────────────────────────┼──────────────────────────────────────────────────┘
                            │
                            ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  PHASE 3: IMPLEMENTATION                                                     │
│                                                                              │
│  ┌─────────────────┐                                                         │
│  │    developer    │ ──▶ commits + PR ──▶ "finished"                         │
│  └─────────────────┘                                                         │
│    (asks planner when stuck; escalates to planner if plan is broken)         │
└──────────────────────────────────────────────────────────────────────────────┘
              │ planner reviews plan compliance
              ▼ ✅ plan-compliant
┌──────────────────────────────────────────────────────────────────────────────┐
│  PHASE 4: CODE REVIEW                                                        │
│                                                                              │
│  ┌─────────────────┐         ┌─────────────────┐                             │
│  │    developer    │ ◀──────  │  code-reviewer  │                             │
│  └─────────────────┘  fixes  └─────────────────┘                             │
│                                      │ ✅ APPROVED                            │
│                                      ▼                                        │
│                               Merge to main                                  │
└──────────────────────────────────────────────────────────────────────────────┘
         │
         ▼ (optional, after API changes)
┌──────────────────────────────────────────────────────────────────────────────┐
│  UTILITY: DOCUMENTATION                                                      │
│  ┌─────────────────────┐                                                     │
│  │   api-documenter    │ ──▶ OpenAPI improvements                            │
│  └─────────────────────┘                                                     │
└──────────────────────────────────────────────────────────────────────────────┘


ARTIFACTS FLOW:
───────────────
ARCHITECTURE_REVIEW.md ◀──────────────────────────────────────────────┐
        │                                                             │
        ▼                                                             │
plan/<feature>.md  ─┐                                                 │
plan/sprint_X.md  ──┴──▶ implementation ──▶ PR ──▶ merge ──▶ (may update)┘


PLAN COMPLIANCE OWNERSHIP:
───────────────────────────
architect-planner  owns compliance for  plan/<feature>.md
sprint-planner     owns compliance for  plan/sprint_*.md


PROGRESS TRACKING:
──────────────────
All agents update plan/progress.txt when starting or completing work.
This file tracks handoffs between phases and records blocking questions.
```

---

## Progress Tracking

The file `plan/progress.txt` tracks workflow state and handoffs between agents:

- **Current State**: Which agent is active and on what feature
- **Handoff Log**: Chronological record of agent completions and blockers
- **Blocking Questions**: Unresolved issues that prevent progress

Each agent should:

1. Update "Current State" when starting work
2. Add a "Handoff Log" entry when completing or blocking
3. Record any blocking questions in the log entry

---

## Phase 1 — Architecture & Direction

### 1. Architect Reviewer

#### Purpose

- Maintain `ARCHITECTURE_REVIEW.md`
- Use `git diff` to get user input
- Identify problems and risks
- Rank issues by risk vs reward
- Answer Q&A and incorporate user suggestions
- Commit updates

#### Idle Behaviour

After completing `ARCHITECTURE_REVIEW.md`, the architect-reviewer **stays idle**.
The architect-planner or sprint-planner may ask clarifying questions at any point
during planning without restarting the reviewer from scratch.

**Role**: `architect-reviewer`

---

## Phase 2 — Planning

There are two planners. Choose based on scope:

| Scope | Use |
|-------|-----|
| One feature, one PR | `architect-planner` |
| Multiple findings, multiple tickets, multiple PRs | `sprint-planner` |

---

### 2a. Architect Planner (single feature)

#### Purpose

- User selects a feature to plan
- Read `ARCHITECTURE_REVIEW.md` as constraints and context
- Query the idle architect-reviewer for clarifications when needed
- Convert architecture findings into an executable plan
- Create or update `plan/<feature-name>.md`
- Commit the plan

#### Idle Behaviour

After the plan is reviewed and committed, the architect-planner **stays idle**.
It serves two functions during implementation:

1. **Q&A**: Answer developer questions about plan intent without requiring a new
   invocation.
2. **Plan-compliance check**: When the developer says **"finished"**, the planner
   reviews whether the implementation matches the plan. Semantic check only — does
   the code do what the plan described? Documents the result in `plan/<feature>.md`
   under `## Plan Compliance — YYYY-MM-DD` as `PASS` or `FAIL [reason]`.

**Role**: `architect-planner`

---

### 2b. Sprint Planner (multi-ticket sprint)

#### Purpose

- Input: a set of findings (postmortem, backlog, architecture review section)
- Cross-checks each finding against the actual source tree before writing tickets
- Groups findings into tickets — combines where files overlap, splits where effort differs
- Orders tickets by dependency and risk into a recommended PR sequence
- Writes full ticket specs with root cause, wiring verification, test scenarios,
  and acceptance criteria checkboxes
- Produces a sprint-level regression guard test and wiring checklist table
- Creates `plan/sprint_<name>.md` and commits it

#### Idle Behaviour

After the sprint plan is committed, the sprint-planner **stays idle**.
It serves the same two functions as the architect-planner, but for sprint plans:

1. **Q&A**: Answer developer questions about ticket intent, PR grouping rationale,
   or wiring decisions.
2. **Plan-compliance check**: When the developer says **"finished"** for a ticket or PR,
   the sprint-planner checks:
   - Each acceptance criterion in the ticket spec
   - Wiring: call site exists (grep), no TODO in happy path, constants align with config
   - Regression guard test is present and not marked `xfail`
   - Documents `## Plan Compliance — YYYY-MM-DD — <ticket ID>` as `PASS` or `FAIL [reason]`

The sprint-planner does **not** check code quality — that is the code-reviewer's job.

**Role**: `sprint-planner`

---

### 3. Plan Reviewer (pre-implementation gate)

#### Purpose

- Review the plan like a code review
- Catch missing steps, ordering issues, FastAPI/Python risks before implementation
- Fix and tighten the plan
- Commit corrections

Applies to plans produced by both architect-planner and sprint-planner.

**Role**: `plan-reviewer`

At this point the plan is implementation-ready and the producing planner enters idle mode.

---

## Phase 3 — Implementation (Iterative)

### 4. Developer

#### Purpose

- Read `ARCHITECTURE_REVIEW.md` and the relevant plan file
- Create a feature branch
- Implement in incremental commits
- Add tests using the existing framework
- Keep diffs minimal and aligned with existing code
- If diverging from the plan, document it under a `Deviations` subsection
- Ask the idle planner questions when plan intent is unclear — do not guess
- Say **"finished"** when all plan steps are implemented and the PR is ready

Before opening a PR, the developer completes the **Wiring Checklist** in the sprint plan
(if working from a sprint plan). The checklist pre-populated by the sprint-planner must
be answered with grep evidence, not assumptions.

#### Escalation — Hand Back to Planner

Stop work and escalate to the producing planner when:

- A plan step conflicts with the existing implementation in a way that makes it
  unworkable (plan assumes a function or field that does not exist)
- Following the plan requires touching significantly more code than described
- The plan produces flaky or incorrect behaviour that cannot be fixed without
  redesigning the approach

When escalating:

1. Stop without committing broken or half-finished code
2. Add `## Escalation — <reason>` to the plan file
3. Update `plan/progress.txt` with `status: escalated-to-planner`
4. Hand off to the planner to revise before resuming

**Rule**: escalate early. Do not work around a broken plan.

**Role**: `developer`

---

## Plan-Compliance Check (between Phase 3 and Phase 4)

When the developer says **"finished"**, the idle planner that produced the plan
performs the compliance check:

| Plan type | Compliance checked by |
|-----------|----------------------|
| `plan/<feature>.md` | `architect-planner` |
| `plan/sprint_*.md` | `sprint-planner` |

The sprint-planner's check is stricter — it additionally verifies:
- Every new method/callback has a confirmed call site (grep evidence)
- No `# TODO` remains in the happy path of any ticket
- Path/constant values match the config module, not a hardcoded string
- The sprint-level regression guard test is present and not marked `xfail`

If `FAIL`, the developer addresses the gap before the PR moves to code review.

---

## Phase 4 — Code Review (Iterative)

### 5. Code Reviewer

#### Purpose

Focuses on **code quality** — not plan compliance (planner's job) and not
architecture (architect-reviewer's job).

Responsibilities:

- Review the open PR for correctness, security, and maintainability
- Verify tests exist for changed behaviour and pass **for the right reasons** —
  not just `pytest` green, but assertions match the described behaviour and would
  catch a regression if the code were reverted
- Verify at least one test asserts observable output (non-empty list, specific
  value) — not just exception handling or log messages
- Check wiring: every new method is called from a production call site; no `# TODO`
  in the happy path; callbacks are passed, not just defined; path constants align
  across modules
- For each issue, classify as:
  - **REVIEWER REMARK** — plan was ambiguous; developer implemented reasonably;
    fix the plan, then ask developer to fix the code
  - **DEVELOPER REMARK** — plan was clear; developer deviated or introduced a bug
- Request changes or approve

Does **not** re-check plan compliance — that was already done by the planner.

**Role**: `code-reviewer`

---

## Iteration Loop

Phases 3 and 4 repeat (with the planner staying idle throughout) until:

- Plan compliance: PASS
- Review comments resolved
- Plan deviations (if any) documented
- Tests pass for the right reasons
- PR approved and merged

---

## Utility Roles

### API Documenter

- Improve FastAPI OpenAPI output and developer docs
- Add explicit `responses={...}` for non-implicit errors
- Document authentication dependencies
- Add request/response examples via Pydantic `Field`

**Role**: `api-documenter`

Use during or after implementation when API documentation needs attention.

---

## How You Run the Flow

You explicitly invoke roles. Examples:

- "Run the architect reviewer and update `ARCHITECTURE_REVIEW.md`."
- "Plan feature X using the architect planner."
- "Plan the next sprint from the postmortem findings using the sprint planner."
- "Review the plan like a code review and fix issues."
- "Implement the plan — ask the planner if anything is unclear, say finished when done."
- "Run the code reviewer on the PR."

Claude does not guess the next step — you choose the role.

---

## Optional Readiness Gate

Before implementation:

```text
[ ] Plan produced (architect-planner or sprint-planner)
[ ] Plan reviewed (plan-reviewer)
[ ] Open questions resolved
[ ] Acceptance criteria defined and testable
[ ] Wiring checklist pre-populated (sprint plans)
[ ] Regression guard test specified (sprint plans)
[ ] Producing planner is idle and available for developer Q&A
```
