# Project Instructions

## Goal
Build simple, readable, maintainable code.
Prefer the smallest change that fully solves the problem.
Do not introduce complexity, abstractions, or dependencies unless they clearly pay for themselves.

## Workflow
Before making changes:
1. Read the relevant files first.
2. Explain the current behavior briefly.
3. State the simplest implementation plan.
4. Then make changes.

## Coding standards
- Match the existing style and conventions of the repo.
- Prefer clear names over clever names.
- Prefer straightforward control flow over dense one-liners.
- Keep functions focused and reasonably small.
- Avoid premature abstraction.
- Avoid placeholder code, fake implementations, and TODO-heavy patches.
- Do not silently change unrelated code.
- Assume the user is a beginner.
- Explain changes in plain English
- Prefer the simplest working solution.
- Avoid unnecessary dependencies.
- Before coding, inspect and propose a plan.
- After coding, verify the result.

## Dependencies
- Prefer built-in or already-installed tools first.
- Do not add new dependencies unless they meaningfully reduce complexity.
- If adding a dependency, explain why it is better than a built-in or existing option.

## Testing and validation
After changes:
- Run the narrowest useful tests first, then broader checks if needed.
- If the repo has linting, formatting, or type-checking, run them on touched code.
- If something cannot be run locally, say exactly what could not be verified.

## File changes
- Keep diffs tight and task-focused.
- Preserve comments unless they are wrong or obsolete.
- Update docs, examples, or config if your code change makes them inaccurate.

## Architecture preferences
- Prefer composition over inheritance.
- Prefer explicit data flow over hidden magic.
- Prefer stable, boring solutions over flashy ones.
- Optimize for maintainability first, performance second unless performance is the task.

## Communication
When finishing a task:
- Summarize what changed.
- Note any tradeoffs.
- Note any remaining risks or follow-up work.
