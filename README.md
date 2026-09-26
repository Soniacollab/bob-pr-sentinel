# 🛡️ Dev Sentinel

### Catch the regression before it leaves your machine.

A small code change can quietly remove a defensive check, break an edge case, or change behaviour that existing tests don't cover.

**Dev Sentinel investigates risky changes, builds a targeted test plan, reproduces suspected regressions, and blocks the `git push` when the regression is confirmed.**

**Predict → Prove → Fix**

> Don't just detect what *might* be wrong.
> **Prove it before you push.**

---

## ⚡ See it in action

```text
$ git push

🛡️ Dev Sentinel
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

   Analysing changes...
   5 Python file(s) changed.

   Exploring affected symbols...
   Analysing behavioural impact...
   Building targeted test plan...
   Reproducing suspected regression...

✗  REGRESSION CONFIRMED

   user_score = None
   → TypeError reproduced

⚠ PUSH BLOCKED

   Review the incident with:
   dev-sentinel fix
```

The developer stays in control.

```text
$ dev-sentinel fix

   Confirmed regression:
   float(None) raises TypeError

   Proposed fix:
   restore defensive handling for missing input

   Apply this fix? [y/N] y

✓  Focused tests passed
✓  Full test suite passed
✓  Fix applied

   Review, stage and commit the changes before pushing again.
```

**No automatic commit. No automatic push. No silent code modification.**

---

## 🎯 The problem

Traditional pre-push checks are good at answering:

> **"Do the tests pass?"**

But what happens when the test covering a new edge case **doesn't exist yet**?

Consider a seemingly harmless change:

```diff
- if value is None or value == "":
-     cleaned_data["user_score"] = 0.0
+ cleaned_data["user_score"] = float(value)
```

The diff is tiny.

The application still works for normal input.

But:

```python
{"user_score": None}
```

now produces:

```text
TypeError
```

The regression was introduced by removing a guard — and there may be no existing test for that input.

**Dev Sentinel is designed for this gap.**

---

## 🧠 How Dev Sentinel works

```text
             CODE CHANGE
                  │
                  ▼
          ┌───────────────┐
          │    Explorer   │
          │ What changed? │
          └───────┬───────┘
                  │
                  ▼
          ┌───────────────┐
          │     Impact    │
          │ What can it   │
          │ affect?       │
          └───────┬───────┘
                  │
                  ▼
          ┌───────────────┐
          │     Tester    │
          │ What should   │
          │ we prove?     │
          └───────┬───────┘
                  │
                  ▼
          ┌───────────────┐
          │    Runner     │
          │ Does it       │
          │ actually fail?│
          └───────┬───────┘
                  │
             ┌────┴────┐
             │         │
           PASS       FAIL
             │         │
             ▼         ▼
           CLEAN    CONFIRMED
                       │
                       ▼
                 PUSH BLOCKED
                       │
                       ▼
                User-authorized
                     Fixer
```

### The important distinction

**Impact predicts. Tests prove.**

An impact analysis alone never blocks the push.

Dev Sentinel blocks only when the suspected regression is actually reproduced.

---

## 🔬 The investigation pipeline

| Stage        | What it does                                                      | Writes source files? |
| ------------ | ----------------------------------------------------------------- | -------------------: |
| **Explorer** | Finds changed symbols, call chains, references and existing tests |                    ❌ |
| **Impact**   | Identifies changed behaviour, affected inputs and components      |                    ❌ |
| **Tester**   | Builds a targeted regression-test plan                            |                    ❌ |
| **Runner**   | Executes the selected pytest target                               |                    ❌ |
| **Fixer**    | Applies a verified production fix after authorization             |                    ✅ |

The investigation stages are read-only.

The **Orchestrator never calls the Fixer**.

This separation is intentional: detecting a problem and modifying code are different trust boundaries.

---

## 🛡️ Fail-closed by design

A safety tool should not say:

> "I couldn't check, so I guess it's fine."

If Git diff computation or the investigation pipeline cannot complete reliably, Dev Sentinel blocks the push with:

```text
⚠ PUSH BLOCKED

Dev Sentinel could not complete its analysis.
```

This avoids turning an analysis failure into a false `clean` result.

---

## 🔧 Fixes require permission

Dev Sentinel does not silently rewrite production code.

The Fixer can only be reached through:

```bash
dev-sentinel fix
```

The developer must explicitly authorize the change:

```text
Apply this fix? [y/N]
```

Only `y` continues.

Invalid answers are rejected and the command asks again.

`n`, an empty response, `Ctrl+C`, or `Ctrl+D` cancel the operation.

After applying a fix:

1. The focused regression test must pass.
2. The full test suite must pass.
3. If verification fails, the original source is restored.
4. The developer reviews the resulting diff.
5. The developer stages and commits the change.
6. The developer pushes again.

---

## 🚫 What Dev Sentinel never does

During normal analysis, Dev Sentinel does **not**:

* run `git add`
* run `git commit`
* run `git push`
* run `git reset`
* rewrite Git history
* modify tracked source files

The Fixer is the exception: it may modify production source code **only after explicit authorization**, and it never commits or pushes automatically.

The developer remains in control of the Git workflow.

---

## 🧪 Example application

The repository includes a deliberately small registration-processing application:

```text
app/
├── preprocessing.py
└── api.py
```

`process_registration()` sends incoming data through `clean_user_input()`.

The test suite covers normal input and edge cases such as:

```python
{"user_score": None}
{"user_score": ""}
```

This gives Dev Sentinel a concrete regression scenario to investigate and reproduce.

The example is intentionally small: the interesting part is not the application itself, but the **safety workflow around the change**.

---

## 📦 Installation

Requirements:

* Python 3.11+
* Git

Create a virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate
```

Install Dev Sentinel:

```bash
pip install -e .
```

Install the pre-push hook:

```bash
dev-sentinel install
```

By default, the current repository is targeted.

You can specify another repository:

```bash
dev-sentinel install --target /path/to/repository
```

If an existing non-Dev-Sentinel pre-push hook is detected, installation stops instead of overwriting it.

To explicitly replace it:

```bash
dev-sentinel install --force
```

Remove the hook:

```bash
dev-sentinel uninstall
```

---

## ▶️ Usage

Run the investigation manually against the current working-tree diff:

```bash
dev-sentinel run
```

Review a confirmed incident and optionally authorize a fix:

```bash
dev-sentinel fix
```

The incident is tied to the Git `HEAD` that produced it. If `HEAD` has changed since the incident was recorded, the fix command refuses to apply the stale incident.

---

## 📊 Result states

| Status                 | Result                                                             |
| ---------------------- | ------------------------------------------------------------------ |
| `clean`                | No confirmed regression → push allowed                             |
| `regression_confirmed` | Regression reproduced → push blocked                               |
| `analysis_failed`      | Safety analysis failed → push blocked                              |
| `fixed`                | Authorized fix verified → developer still needs to commit and push |

---

## 🗺️ Roadmap

Dev Sentinel is intentionally starting with a narrow, evidence-driven scope rather than pretending to detect every possible regression.

Future iterations could expand the system in two directions:

* **More regression patterns** — broader coverage of validation removals, API contract changes, type-related regressions, error-handling changes and other behavioural patterns.
* **More languages** — extend the same architecture beyond Python to ecosystems such as JavaScript/TypeScript, Java, Go and others, with language-specific analysis and testing.

The long-term goal is not simply to detect more suspicious diffs.

It is to make the **Predict → Prove → Fix** workflow increasingly useful across different codebases and languages.

---

## 🔐 Repository hook

This repository contains a versioned development hook:

```text
.githooks/pre-push
```

It can be enabled with:

```bash
python scripts/install_hooks.py
```

This configures:

```text
core.hooksPath=.githooks
```

for this repository only.

It does **not** modify global Git configuration.

The versioned hook invokes the project's virtual-environment entrypoint:

```text
.venv/bin/dev-sentinel-hook
```

For another repository, `dev-sentinel install` installs the hook directly into that repository's `.git/hooks/`.

---

## 🧩 Development

Run the test suite:

```bash
pytest
```

The project currently contains deterministic, evidence-driven agent implementations using Python's standard library:

* `ast`
* `re`
* `pathlib`

The agent contracts are intentionally separated from the orchestrator so they can later be replaced or augmented by Bob subagents without changing the core safety boundaries.

---

## 📁 Project structure

```text
sentinel/
├── cli.py              # install | uninstall | run | fix
├── git_hook.py         # Git pre-push adapter
├── orchestrator.py     # investigation state machine
├── runner.py           # pytest execution
├── models.py           # shared data models
└── agents/
    ├── explorer.py     # change exploration
    ├── impact.py       # impact analysis
    ├── tester.py       # targeted test planning
    └── fixer.py        # authorized production fix

app/
├── preprocessing.py    # example application logic
└── api.py              # example registration flow

tests/                  # automated tests

.githooks/
└── pre-push             # versioned hook for this repository

scripts/
└── install_hooks.py     # repository-local hook setup
```

---

## 💡 Design principles

### Evidence over speculation

A suspicious diff is not automatically a regression.

```text
Potential impact
       ↓
Targeted test
       ↓
Reproduction
       ↓
Confirmed regression
```

### Least privilege

Each component has a narrow responsibility.

Analysis is read-only.

Code modification is isolated behind an explicit authorization step.

### Fail closed

When the safety analysis cannot reliably complete, the push is blocked.

### Developer remains in control

Dev Sentinel can investigate.

It can prove.

It can propose and, with permission, apply a fix.

But **the developer decides what gets committed and pushed.**

---

## 🛡️ Dev Sentinel

**Don't just ask whether the code looks safe.**

**Prove it before you push.**
