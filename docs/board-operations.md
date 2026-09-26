# Board operations

The human is the Board. Your normal interface is one Cockpit and two conversations—not tmux and not a command console.

## Start the company

Double-click `OneCo — COMPANY.app` in `~/Applications`, or run:

```bash
oneco start --root /path/to/company
```

You see:

- a native Cockpit with CEO/CTO presence, every project, current task, Owner state, queued work, health, and items requiring Board attention;
- a canonical CEO in one native Ghostty window;
- a canonical CTO in another native Ghostty window.

The Cockpit is for awareness and navigation, not project forms. Click an executive to focus or reopen its exact conversation. Click Watch on a project to open a read-only Ghostty view of its hidden Owner worker.

## Shape and start a product

Talk normally. A typical Board flow is:

```text
Board → CEO: I want to build X for Y. The first outcome should be Z. Challenge the scope and create it when concrete.
CEO → Board: My recommended first vertical result is ... The one decision I need is ...
Board → CTO: Inspect PROJ-001. Give me the smallest sound technical path and write the quality boundary.
CTO → Board: Use ... Avoid ... The launch packet is technically ready / needs this decision.
Board → CEO or CTO: Launch it once you both endorse this exact packet.
```

CEO/CTO use OneCo tools inside their conversations to create the workspace, read one another's messages, edit the initial Brief/Spec/constitution, endorse, launch, and wake the Owner. You do not run those commands yourself. A product cannot launch until its Brief, Spec or seed, constitution, acceptance criteria, non-goals, and Git boundary are present.

Normal launch requires both executive endorsements. If one executive is unavailable and the artifacts are complete, tell the other in a fresh message: `Just launch PROJ-001.` The override applies once, is tied to that exact human turn, and waives only the absent peer endorsement.

## Supervise without micromanaging

Owners execute continuously in atomic turns. They use Spec Kit to produce or refine `spec.md`, `plan.md`, and `tasks.md`, complete the smallest accepted task, verify it, checkpoint it, and claim the next compatible item.

Ask CEO about user intent, scope, sequence, momentum, and simplicity. Ask CTO about actual architecture, tests, correctness, safety, and unnecessary machinery. If either finds drift, they call a bounded Owner instruction themselves. While the Owner is busy, it stays queued until the atomic boundary and then wakes automatically.

The five races are not rankings between heterogeneous products:

| Owner behavior | Useful pressure | Bad proxy never rewarded |
| --- | --- | --- |
| Closure | Smallest runnable vertical loop | Number of features |
| Commitment | Finish the accepted Spec promise | Online time |
| Simplicity | Fewer justified layers and dependencies | Code volume |
| Truth | Expose failures and unknowns early | Polished status theater |
| Reuse | Share demonstrated practices quickly | Report length or Token use |

Reliable Owners earn broader autonomous execution spans. Drift or hidden uncertainty earns narrower instructions and more frequent checkpoints. Repeated protocol failure may replace an Owner session, but the product is never paused because coding capacity is scarce.

## Parallel executive conversations

Tell CEO or CTO: “Fork a secretary to explore pricing without changing the current direction.” They call the branch tool and a titled Ghostty window opens with the exact parent Trae context. The Secretary may research and send advice only to its parent; it cannot endorse, launch, approve, instruct an Owner, or become a second canonical executive.

The operator fallback is:

```bash
oneco branch CEO --purpose "pricing alternative" --root /path/to/company
```

If an executive thread cannot resume at all, create a recovery-candidate branch. The candidate requests a short-lived phrase and asks you to type it exactly in that same chat. Only then does OneCo promote it and fence the old executive. Do not use recovery candidates for ordinary brainstorming.

## Common scenarios

| Situation | System behavior | Board action |
| --- | --- | --- |
| Owner adds unapproved behavior | CEO queues `NARROW` or `ALIGN` with evidence | Decide only if product intent itself must change |
| Owner creates premature infrastructure | CTO queues `SIMPLIFY` | None unless the architecture principle changes |
| Tests or safety fail | CTO blocks only the affected merge/deploy and names the unblock condition | Decide only material risk acceptance |
| New instruction arrives mid-task | SQLite queues it; current atomic turn is not interrupted | None |
| Owner pane dies mid-task | Claimed work is requeued; exact Trae thread resumes | Inspect only if repeated |
| CEO/CTO window closes | Reopen resumes the same UUID and context | Click the executive in Cockpit |
| CEO and CTO conflict materially | Owner continues only compatible work | Resolve direction explicitly |
| Brief/Spec changes after endorsement | Digest invalidates both endorsements | Ask both to re-review |
| External product action | Durable approval request goes to CEO | Intervene only for strategic commitment |
| External technical/data action | Durable approval request goes to CTO | Intervene for sensitive/high-risk access |
| Runtime is damaged | Versioned artifacts remain intact | Run `oneco runtime rebuild`, then restart |

## Anti-AI-taste standard

Executives lead with one decision, evidence, and the next needed action. They do not produce ceremonial strategy decks, generic frameworks, long option menus, invented confidence, or status reports that do not change a decision. CEO inspects real behavior when it matters; CTO runs the product, code, and tests when the claim depends on them. Context is rich before delegation, control is light after it, and corrections are short and concrete.
