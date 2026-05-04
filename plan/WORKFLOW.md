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

Handoffs between phases are signals ("I am done, ask planner"), not context restarts.

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
│  ┌─────────────────────┐         ┌─────────────────┐                         │
│  │  architect-planner  │ ──▶     │  plan-reviewer  │ ──▶ plan/<feature>.md   │
│  └─────────────────────┘         └─────────────────┘     (reviewed)          │
│             │ [IDLE — available for developer Q&A + plan-compliance check]   │
└─────────────┼────────────────────────────────────────────────────────────────┘
              │ ▲ developer may ask questions        │ plan-compliance check
              │ │                                    ▼ (when dev says "finished")
              ▼ │
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
plan/<feature>.md ──▶ implementation ──▶ PR ──▶ merge ──▶ (may update)┘


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

#### Architect Reviewer Purpose

- Maintain `ARCHITECTURE_REVIEW.md`
- Use `git diff` to get user input
- Identify problems and risks
- Rank issues by risk vs reward
- Answer Q&A and incorporate user suggestions
- Commit updates

#### Architect Reviewer Idle Behaviour

After completing `ARCHITECTURE_REVIEW.md`, the architect-reviewer **stays idle**.
The architect-planner may ask clarifying questions at any point during planning
without restarting the architect-reviewer from scratch.

**Role**: `architect-reviewer`

---

## Phase 2 — Planning

### 2. Architect Planner

#### Architect Planner Purpose

- User selects a feature to plan
- Read `ARCHITECTURE_REVIEW.md` as constraints and context
- Query the idle architect-reviewer for clarifications when needed
- Convert architecture findings into an executable plan
- Create or update `plan/<feature-name>.md`
- Commit the plan

#### Architect Planner Idle Behaviour

After the plan is reviewed and committed, the architect-planner **stays idle**.
It serves two functions during implementation:

1. **Q&A**: Answer developer questions about plan intent without requiring a new
   planner invocation (and without the developer reading the full plan cold).
2. **Plan-compliance check**: When the developer says **"finished"**, the planner
   reviews whether the implementation matches the plan. This is a semantic check
   only — does the code do what the plan described? Not a code-quality review.
   The planner documents the result in `plan/<feature>.md` under a
   `## Plan Compliance` section and marks it `PASS` or `FAIL [reason]`.

**Role**: `architect-planner`

### 3. Plan Reviewer (Pre-implementation gate)

#### Plan Reviewer Purpose

- Review the plan like a code review
- Use `git diff`
- Catch missing steps, ordering issues, FastAPI/Python risks
- Fix and tighten the plan
- Commit corrections

**Role**: `plan-reviewer`

At this point, the plan is implementation-ready and the planner enters idle mode.

---

## Phase 3 — Implementation (Iterative)

### 4. Developer / Implementer

#### Developer Purpose

- Read `ARCHITECTURE_REVIEW.md` and `plan/<feature>.md`
- Create a feature branch
- Implement in incremental commits
- Add tests using the existing framework
- Keep diffs minimal and aligned with existing code
- If diverging from the plan, document it under a `Deviations` subsection
- Ask the idle planner questions when plan intent is unclear — do not guess
- Say **"finished"** when all plan steps are implemented and the PR is ready

#### Developer Escalation — Hand Back to Planner

The developer may **stop work and hand back to the architect-planner** when:

- A plan step conflicts with the existing implementation in a way that makes it
  unworkable (e.g. the plan assumes a function or field that does not exist)
- Following the plan would require touching significantly more code than described,
  indicating a design gap
- The plan produces flaky or incorrect behaviour that cannot be fixed without
  redesigning the approach

When escalating, the developer must:

1. Stop without committing broken or half-finished code
2. Add a `## Escalation — [reason]` section to `plan/<feature>.md` documenting
   exactly what was tried, what broke, and why the plan needs revision
3. Update `plan/progress.txt` with status `escalated-to-planner` and a clear
   description of the blocker
4. Hand off to the architect-planner to revise the plan before resuming

**Rule**: escalate early. Do not work around a broken plan with hacks — surface
the problem so the plan can be fixed properly.

**Role**: `developer`

---

## Planner Plan-Compliance Check (between Phase 3 and Phase 4)

When the developer says **"finished"**, the idle planner performs a plan-compliance
check:

- Read the final diff / commits against each step in `plan/<feature>.md`
- Verify each step was implemented as described (semantic check only — no code quality)
- Document the result in `plan/<feature>.md` under `## Plan Compliance`:
  - `PASS` — all steps implemented as planned (deviations are documented)
  - `FAIL [reason]` — a step was skipped or implemented contrary to the plan

If `FAIL`, the developer must address the gap before the PR moves to code review.

---

## Phase 4 — Code Review (Iterative)

### 5. Code Reviewer

#### Code Reviewer Purpose

The code reviewer focuses on **code quality** — not plan compliance (that was the
planner's job) and not architecture (that is the architect-reviewer's job).

Responsibilities:

- Review the open PR for correctness, security, and maintainability
- Verify that tests exist for the changed behaviour and that they pass **for the
  right reasons** — not just that `pytest` is green, but that the test assertions
  match the described behaviour
- Flag code issues: naming, duplication, unsafe patterns, missing edge cases
- For each issue found, classify it:
  - **REVIEWER REMARK** — the plan was ambiguous; developer followed it reasonably;
    fix the plan, then ask developer to fix the code
  - **DEVELOPER REMARK** — plan was clear; developer deviated or introduced a bug;
    developer fixes it
- Request changes or approve

The code reviewer does **not** re-check plan compliance — that was already done by
the planner. If a plan-compliance gap surfaces during code review, flag it as a
`REVIEWER REMARK` and hand back to the planner.

**Role**: `code-reviewer`

---

## Iteration Loop

Phases 3 and 4 repeat (with the planner staying idle throughout) until:

- Plan compliance: PASS
- Review comments resolved
- Plan deviations (if any) documented
- Tests are passing for the right reasons
- PR is approved and merged

---

## Utility Roles

### API Documenter

#### API Documenter Purpose

- Improve FastAPI OpenAPI output and developer docs
- Add explicit `responses={...}` for non-implicit errors
- Document authentication dependencies
- Add request/response examples via Pydantic `Field`

**Role**: `api-documenter`

Use this role during or after implementation when API documentation needs attention.

---

## How You Run the Flow

You explicitly invoke roles. Examples:

- "Run the architect reviewer and update `ARCHITECTURE_REVIEW.md`."
- "Plan feature X using the architect planner."
- "Review the plan like a code review and fix issues."
- "Implement the plan — ask the planner if anything is unclear, say finished when done."
- "Run the code reviewer on the PR."

Claude does not guess the next step — you choose the role.

---

## Optional Readiness Gate

Before implementation:

```text
[ ] Plan reviewed and committed
[ ] Open questions resolved
[ ] Acceptance criteria defined
[ ] Test strategy defined
[ ] Planner is idle and available for developer Q&A
```

This prevents starting work on incomplete plans.
