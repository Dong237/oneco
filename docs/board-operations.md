# Board operations

You are the Board. Your normal workspace is the Cockpit plus three persistent conversations: CPO, CEO, and CTO. Owners run in hidden tmux panes and appear only after a project passes the launch gate.

## Daily playbook

1. Start once:

   ```bash
   oneco start --root /path/to/company
   ```

2. In Cockpit, assign a market Signal with its source. A Board idea, link, interview note, or reviewed Daily Product Teardown artifact can be a Signal. OneCo never runs Daily Product Teardown automatically.
3. Tell CPO to investigate the assigned `SIG-NNNN`. CPO uses appropriate external research skills, preserves contradictory evidence, and submits a versioned Product Bet.
4. In Cockpit, choose one action on the exact revision:
   - **Select**: CEO may promote it.
   - **More evidence**: name the missing evidence, not how CPO must obtain it.
   - **Reject**: keep the record; create no project.
5. Tell CEO to promote the selected Bet and shape the smallest useful result with CTO. CEO protects product intent; CTO protects technical safety. Both endorse the same launch digest.
6. After launch, let the Owner execute. Use **Watch** only when you need the live log. Ask CEO about product choices and CTO about technical choices.
7. When the Owner records a verified release candidate, CEO and CTO independently endorse its exact digest. Approve production in Cockpit only after both are ready.
8. Close Cockpit whenever you like; supervision continues. Stop the company background supervisor explicitly:

   ```bash
   oneco stop --root /path/to/company
   ```

## Useful commands

```bash
oneco status --root /path/to/company
oneco talk CPO --root /path/to/company
oneco talk CEO --root /path/to/company
oneco talk CTO --root /path/to/company
oneco signal list --root /path/to/company --json
oneco bet list --root /path/to/company --json
oneco doctor --root /path/to/company
oneco runtime rebuild --root /path/to/company
```

Routine product work should happen conversationally through the authorized role tools. Direct `oneco project register` is disabled in v1; CEO promotion is the only normal project-creation path.

## Escalation rules

- A routine checkpoint does not stop the Owner. Compatible work continues.
- A real gate—unsafe change, irreversible action, architecture conflict, acceptance failure, or product-intent change—stops only affected work and routes to CTO or CEO.
- CEO has final product and sequence authority only inside CTO's evidence-backed safety envelope.
- Material CEO/CTO conflict comes to you. No agent silently resolves it.
- Verification is not production approval.

## Narrow launch waiver

Normal launch requires matching CEO and CTO endorsements. If exactly one peer is unavailable and every mandatory artifact is complete, tell the available executive in a fresh message:

```text
Just launch PROJ-001.
```

The waiver is tied to that human turn, lasts briefly, is consumed once, and waives only the absent peer endorsement. It cannot waive the Brief, Spec, constitution, acceptance criteria, non-goals, Git boundary, or both endorsements at once.

## Recovery

| Situation | Expected behavior | Board action |
| --- | --- | --- |
| CPO/CEO/CTO window closes | Reopens the same role session and exact Trae thread | Click the role in Cockpit or run `oneco talk ROLE` |
| Owner pane dies | Claimed work is requeued; exact Owner thread resumes | Inspect only if failure repeats |
| Old role process returns | Epoch fence rejects its writes and messages | None |
| SQLite is lost | Git-backed Signals, Bets, decisions, projects, reviews, and releases remain | Run `oneco runtime rebuild`, then restart |
| Chat context is lost | Role reconstructs state from snapshot, inbox, and artifacts | Ask it to re-ground, not to guess |
| External research skill is missing | Health shows an explicit missing capability | Install the named skill or revise the evidence request |
| CEO and CTO disagree materially | Owner continues only compatible work | Decide the product-versus-safety tradeoff explicitly |

## Parallel research and role recovery

Ask a persistent role to fork a bounded delegate when useful. A CPO research delegate reports only to CPO; CEO/CTO Secretaries report only to their parent. They cannot inherit the parent identity or use privileged tools.

Use a recovery candidate only when an exact role thread cannot resume. The candidate asks you for a short-lived phrase such as `PROMOTE CPO 7K4N`. Enter it in that exact candidate conversation; successful succession increments the epoch and fences the old process.

## Pressure-test contract

| # | Actor / scenario | Allowed and durable change | Forbidden | Runtime, Cockpit, retry/recovery |
| --- | --- | --- | --- | --- |
| 1 | Board idea or teardown artifact | Board records either as a sourced Signal | CPO scheduling teardown | CPO gets one assignment; both appear identically as Signals |
| 2 | CPO drafts/revises | Bet files and immutable submitted manifest | Select, reject, promote, endorse, launch, approve, instruct | Unauthorized MCP calls fail closed |
| 3 | Board asks for evidence | Digest-bound `more_evidence` with named gap | Prescribing a required research method | CPO receives idempotent work and sees only the new gap |
| 4 | Board rejects | Decision history remains | Project creation | Cockpit shows rejected Bet; rebuild preserves it |
| 5 | Board selects | Exact revision/digest decision | Promoting another digest | CEO receives promotion work; copied handoff matches byte-for-byte |
| 6 | Selected Bet changes | New revision requires new selection | Stale promotion | Digest/revision check fails before project creation |
| 7 | CEO/CTO disagree | Evidence-backed review and Board escalation | CPO deciding; global stop | Only affected work blocks; compatible Owner work continues |
| 8 | Launch artifact changes | New digest | Reusing endorsements | Both endorsements read stale in Cockpit and runtime |
| 9 | Owner has a question | Product to CEO; technical to CTO | CPO interception | Scoped messages, explicit acknowledgement, retry-safe order |
| 10 | CPO adds post-selection evidence | Advice to CEO/CTO | Project or queue mutation | Project remains unchanged |
| 11 | Role windows close | Exact-thread resume | Implicit last-thread selection | Cockpit shows lost/live state; no durable truth changes |
| 12 | Replaced CPO returns | None | Write or message as CPO | Epoch fence rejects the stale session |
| 13 | CPO delegate reports | Message to CPO | Becoming CPO or messaging delivery roles | Parent-only route survives retries |
| 14 | Duplicate/out-of-order traffic | One idempotent message; explicit ack | Duplicate work | Per-thread sequence remains intelligible; retry is safe |
| 15 | SQLite loss | Rebuild indexes/queues from Git | Treating SQLite as memory | Cockpit repopulates from artifacts |
| 16 | Chat loss | Re-read bounded snapshot and files | Inventing prior decisions | Exact role thread is helpful but not required for truth |
| 17 | Multiple Bets/products | Independent records and Owner queues | Agent-owned portfolio allocation | Cockpit displays all; Board chooses attention |
| 18 | Validation status varies | Preserve passed/mixed/failed/invalid/TBD as evidence | Automatic selection/rejection | Any state may reach Board with confidence boundary |
| 19 | External skill unavailable | Explicit capability error | Silent substitute | Health names the missing contract; artifacts remain intact |
| 20 | Increment verified | Release candidate and endorsements | Automatic production | Board decision queues exactly one release action; result is separate |
