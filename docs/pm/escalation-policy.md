# Escalation policy

## Purpose

Agent Relay uses ordinary agents for bounded, repeatable execution and spends
high-intelligence Codex reasoning on decisions that have high downstream
leverage. Escalation is a policy decision, not a model-confidence claim and
not a replacement for deterministic verification.

The policy answers:

1. Should the current stage continue with the ordinary worker?
2. Should Agent Relay consult a configured high-intelligence lane?
3. Should the task wait or block because the evidence or authority is
   insufficient?

The policy does not silently change a task's write scope, credentials, retry
permissions, or verification commands.

## Roles

| Role | Normal responsibility | Proof boundary |
| --- | --- | --- |
| Bulk worker | Mechanical implementation, extraction, formatting, bounded repair, and other repeatable work | Candidate result, declared checks, scope and workspace gates |
| High planner | Resolve ambiguity, choose an approach, decompose difficult work, or diagnose repeated failure | Bounded consultation receipt; it does not authorize edits by itself |
| High verifier | Independently inspect a candidate result, evidence, risks, and regressions | Read-only findings and an explicit pass/fail verdict |
| Human/operator | Approve irreversible or externally consequential decisions | Separate approval evidence |

The high lanes are configurable. The repository's Codex adapter can use
`gpt-5.6-sol`, `gpt-5.6-terra`, or another entitled model at high reasoning
effort. Model names are policy configuration, not routing logic.

## Stages and default escalation table

Rules are evaluated against the stage and observed signals. The table below is
the default policy intent; deployments may replace it with a JSON policy file.

| Priority | Stage | Trigger | Decision | Required evidence or next action |
| ---: | --- | --- | --- | --- |
| 1000 | any | Irreversible, credential-bearing, production, or externally consequential side effect without explicit authority | `block` | Record the missing authority; do not invoke a worker or fallback model |
| 900 | plan | Architecture, security, migration, product decision, ambiguous objective, or high consequence | `consult` | High-planner receipt containing assumptions, alternatives, risks, decomposition, and acceptance criteria |
| 850 | plan | Scope exceeds configured bounded-task limit or acceptance criteria are missing | `consult` or `block` | Planner may clarify/decompose; block if the contract remains incomplete |
| 800 | execute | Worker disagreement, contradictory task inputs, or an unsafe scope signal | `consult` | High-diagnosis receipt; do not broaden the worker contract |
| 750 | recovery | Timeout, adapter failure, or the same failed attempt repeats | `consult` | Failure evidence, attempted remedies, and a bounded recovery recommendation |
| 700 | review | Verification fails, evidence is missing, scope changed, or a high-risk signal is present | `require_review` | Independent high-verifier receipt before accepting the candidate |
| 650 | release | Release, deploy, publish, delete, or production transition | `require_review` | High-verifier findings plus human approval where the side effect is irreversible |
| 100 | any | No rule matches and the task has bounded scope plus passing deterministic checks | `continue` | Normal worker receipt and parent-owned deterministic verification |

`block` is fail-closed. `consult` means obtain high-intelligence advice before
continuing. `require_review` means a high-verifier pass is a prerequisite for
acceptance. `continue` never means “trusted”; it means the existing worker and
deterministic proof path is sufficient for the current gate.

## Configurable policy contract

The policy is a versioned JSON object. The explicit `--policy` path or
`AR_ESCALATION_POLICY` environment variable takes precedence over the project
default. Rules are sorted by descending `priority`; ties preserve file order.
The first matching rule wins. A malformed policy is rejected rather than
silently replaced with permissive defaults.

```json
{
  "version": 1,
  "enabled": true,
  "default_action": "continue",
  "profiles": {
    "high_planner": {
      "lane": "codex-review",
      "model": "gpt-5.6-sol",
      "reasoning_effort": "high"
    },
    "high_verifier": {
      "lane": "codex-review",
      "model": "gpt-5.6-terra",
      "reasoning_effort": "high"
    }
  },
  "rules": [
    {
      "id": "review-failed-proof",
      "priority": 700,
      "stages": ["review"],
      "any": {
        "verification_failed": true,
        "missing_evidence": true,
        "scope_changed": true
      },
      "action": "require_review",
      "profile": "high_verifier",
      "evidence_required": ["independent_findings", "verification_receipt"]
    }
  ]
}
```

Supported signals are deliberately operational rather than invented model
confidence: `task_kind`, `risk_flags`, `scope_files`, `ambiguity`,
`consequence`, `attempts`, `worker_failed`, `verification_failed`,
`missing_evidence`, `scope_changed`, and `stage`. Operators can add rules but
cannot make a verifier's receipt count as deterministic proof or bypass the
workspace and authorization gates.

## Runtime flow

```text
task contract
     |
     v
policy(stage, observed signals) ---- block ---> durable blocked/waiting state
     |
     +---- continue --------------------------> bulk worker
     |
     +---- consult / require_review ----------> configured high Codex lane
                                                     |
                                                     v
                                      bounded advice/findings receipt
                                                     |
                                      worker repair or acceptance gate
```

The policy engine is pure and produces a machine-readable decision. The
`escalate` command is useful for previews and automation. The `consult` path
is the only path that invokes the high lane. If that lane is unavailable, the
result is an explicit failed or blocked consultation; Agent Relay must not
silently fall back to the bulk worker and call the gate satisfied.

## What counts as evidence

Every escalation decision records the policy version, matched rule, stage,
signals, selected profile/lane/model, and required evidence. A consultation
receipt records the command, model, reasoning effort, duration, status, and
bounded response/findings. Acceptance still requires the ordinary artifact,
scope, workspace, and deterministic verification evidence. A high-model
opinion is additional evidence, never a substitute for execution proof.

## Initial measurement

Record each decision with `escalated`, `reason`, model/profile, duration, and
outcome. Review the policy after a real cohort using:

- escalation rate by stage and rule;
- consultations that found an actionable defect;
- recovery success after escalation;
- false-positive escalations where deterministic proof was already sufficient;
- high-model tokens and latency per verified job;
- blocked jobs caused by unavailable high-lane capability.

The policy should be tuned from these outcomes, not from the worker's free-form
self-confidence.
