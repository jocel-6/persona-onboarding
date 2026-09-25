# Roadmap: making Persona as capable as possible

Ideas beyond the take-home spec, chosen to make the consumer experience better and to show what Persona could be. Each has a status that's updated as it's built.

**Status key:** ⬜ planned · 🟡 in progress · ✅ built (tested with the fake-model suite) · 🔑 built, needs an account/key to switch on

## A. Make the call feel human-fast

| # | Idea | What the user notices | Status |
|---|---|---|---|
| 1 | **Speculative replies.** Start generating from the live transcript before the user finishes; discard if they keep talking. | Replies ~0.3–0.5 s sooner | ✅ |
| 2 | **Instant acknowledgements.** Pre-rendered "mm-hm / got it / oh nice" clips in the agent's voice play the moment the user stops, while the real reply is written. | The gap after you speak disappears | ✅ |
| 3 | **Tone-matched voice.** Cartesia emotion/speed controls follow the mood the app already detects: softer and slower when frustrated, brighter when excited. | It sounds like it's reading the room | ✅ |
| 4 | **Pick its voice when you name it.** Three short samples in the naming step. | A second "this is mine" moment | ✅ |
| 5 | **Hear tone, not just words.** Audio emotion analysis (Hume) feeds the director's note. | Notices stress even behind polite words | 🔑 |

> #1 runs on Deepgram Flux (`STT_ENGINE=flux`): conversational speech-to-text with its own turn detection and early end-of-turn predictions. It's opt-in until verified on live calls; the default stays Nova-3 + Smart Turn.

## B. Make "Persona noticed" brilliant

| # | Idea | What the user notices | Status |
|---|---|---|---|
| 6 | **Deep-thinking pass in the background.** A stronger model reasons over the whole week while the conversation continues and finds connections rules can't ("the field trip Friday lands on your planning day; want me to ask Sam to cover pickup?"). | Insights that feel eerily smart | ✅ |
| 7 | **Fixes that solve the problem.** Free-slot finding for conflicts, focus blocks, rescheduling suggestions. Always proposed, always confirmed. | It fixes, not just flags | ✅ |
| 8 | **Draft replies (never send).** "Jordan's waiting on you; here's a draft" → saved to Gmail drafts only when the user taps Save draft. | The strongest "it's already working" moment | ✅ |
| 9 | **Tomorrow at a glance.** A card at the end: tomorrow as Persona sees it, plus the one thing to watch. | What daily life with Persona feels like | ✅ |

## C. Make it look and feel premium

| # | Idea | What the user notices | Status |
|---|---|---|---|
| 10 | **A living profile that builds itself.** A card that fills in with smooth animations as the agent learns: name, need, the shape of their week, first findings. | You watch it get to know you | ✅ |
| 11 | **Animated voice orb.** An Apple-style orb on the call screen reacting to both voices in real time, with live word-by-word captions. | A polished product, not a demo | ✅ |
| 12 | **Mobile-first and installable** (PWA: add to home screen, standalone, safe areas). | Matches a phone-first product | ✅ |

## D. Make it real-world

| # | Idea | What the user notices | Status |
|---|---|---|---|
| 13 | **Real phone calls and texts** (Twilio): Persona calls your number; the recap arrives as an SMS. | The spec's "phone call," literally | ⬜ |
| 14 | **"What Persona knows about you."** Everything it learned, editable and deletable, in one place. | Trust and transparency, on-brand for "nothing without your yes" | ✅ |

## E. Engineering depth

| # | Idea | Why | Status |
|---|---|---|---|
| 15 | **Voice evals at scale.** All personas through real audio calls; transcription accuracy and latency distributions. | Proves the voice path, not just text | ✅ |
| 16 | **Metrics dashboard.** Latency p50/p90, cost per conversation, onboarding funnel and drop-off. | The plan's "learn from drop-off" item | ✅ |
| 17 | **Quality gate in CI.** A small eval subset on demand/for PRs that blocks regressions. | Quality that can't silently slip | 🔑 |

> #17 needs the Anthropic key as a GitHub secret (repo → Settings → Secrets and variables → Actions → `ANTHROPIC_API_KEY`). It runs only by hand or on PRs labeled `eval` (~$0.40/run, capped at $2).

> #15 is built and dry-runs by default (`scripts/voice_eval.py`); `--yes` runs it (~$0.15 for all six calls). #5 switches on with `HUME_API_KEY`.

## Build order

Highest impact for the least risk first: 10 → 11 → 2 → 1 → 3 → 4 → 6 → 7 → 8 → 9 → 14 → 12 → 16 → 17 → 15 → 5 → 13.

## Cost notes

- Free at runtime: 1 (a bit more model usage from discarded drafts), 2, 3, 4, 7, 10, 11, 12, 14, 16.
- Cents per user: 6 (~$0.02–0.05 per connection), 8 and 9 (~$0.01 each).
- Only when run on purpose: 15 (~$2–4 per run), 17 (~$0.30 per run).
- Needs a new account: 5 (Hume), 13 (Twilio, ~$1/month number plus per-minute/SMS).
