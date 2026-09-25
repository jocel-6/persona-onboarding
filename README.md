# Persona onboarding

[![CI](https://github.com/jocel-6/persona-onboarding/actions/workflows/ci.yml/badge.svg)](https://github.com/jocel-6/persona-onboarding/actions/workflows/ci.yml)

An adaptive voice + text onboarding agent. In the first five minutes it learns four things (a name for itself, your name, a connected Gmail, and what you need help with) through a conversation that feels like talking to a friend, then proves it's already useful with something real from your calendar or inbox.

**What's worth a look:**
- **One brain, two channels.** Chat and a real voice call (WebRTC, in the browser) share one model, one prompt, one history. Hang up mid-sentence and it picks up over text exactly where it stopped.
- **The model handles language; code handles control flow.** A deterministic orchestrator owns the rules and writes a *director's note* every turn; Claude owns understanding and wording. Watch it live in the app: **Show state → Director's note**.
- **Real turn-taking.** Semantic end-of-turn detection, backchannel filtering ("mhm" doesn't interrupt, "wait" does in ~0.5–0.9 s), barge-in that trims history to what you actually heard, silence check-ins.
- **Real Gmail, privacy by construction.** `gmail.metadata` can't read email bodies at all; private items are filtered in code; tokens never leave the server and are revoked on disconnect or after 7 idle days.
- **An eval harness with simulated adversarial users** (rushed exec, rambler, privacy skeptic, jailbreaker, …) graded by code and by Claude Opus 5, under a hard spend cap. It found and drove most of the fixes in [the engineering log](docs/ARCHITECTURE.md#engineering-log-real-bugs-found-and-how).
- **Measured, not guessed:** ~1.0 s from end of your turn to the agent's voice (Haiku 4.5), every turn logged.

**Live demo:** _link after deploying ([docs/DEPLOY.md](docs/DEPLOY.md))_ · **Video:** _2–3 min walkthrough ([script](docs/DEMO_SCRIPT.md))_

Docs: [Architecture and decisions](docs/ARCHITECTURE.md) · [Connecting real Gmail](docs/google-setup.md) · [Deploying](docs/DEPLOY.md) · the two plan PDFs in the repo root.

## Run it locally

Requires Python 3.12+ and Node 20+.

```bash
cp .env.example .env            # add ANTHROPIC_API_KEY (+ voice keys for real calls, see below)
cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && cd ..
cd frontend && npm install && cd ..
./dev.sh                        # backend :8000, frontend :3000
```

Or run the backend in Docker: `docker build -t persona-backend backend && docker run --env-file .env -p 8000:8000 persona-backend`.

Open http://localhost:3000. "Show state" in the top bar shows the slot table, signals, and per-turn latency. "Start over" resets the session.

Tests: `cd backend && .venv/bin/python -m pytest` (46 tests, fake model client: no keys, no network). CI runs them plus the frontend typecheck, lint and build on every push (`.github/workflows/ci.yml`).

Evals: `cd backend && .venv/bin/python -m evals.run` (real API calls; ~$0.10 per conversation; capped by `EVAL_BUDGET_USD`, default $20).

Automated voice call: `cd backend && .venv/bin/python scripts/call_test.py` (dials the running backend over WebRTC and talks with synthesized speech).

## How it works

**The model handles language; code handles control flow.** This is mixed-initiative, frame-based dialogue management (hybrid open slot filling):

| Code (`backend/app/orchestrator.py`) owns | Claude Sonnet 5 owns |
|---|---|
| The slot table: what's filled, missing, corrected | Understanding messy, out-of-order, multi-part answers |
| Validation of every saved value (`validation.py`) | Filling slots by calling `save_onboarding_info` |
| The graduation rule and cooldown | Reading mood from the user's words |
| Behavioral signals: short-answer streaks, stalls | Warm, varied wording |
| Events: call accepted, hangup, missed call, Gmail, resume | Following the director's note naturally |
| The **director's note** each turn | |

Every turn (`backend/app/brain.py`):

1. Typed text, a spoken transcript, or an app event (`[event: the call dropped…]`) comes in.
2. Code updates signals and writes a `<director_note>` (filled, missing, signals, graduation status, priority, reply length). The note goes at the end of the newest user message, so the system prompt and tools stay cached.
3. Claude streams its reply and calls `save_onboarding_info` when it learns something.
4. Code validates each call, updates state, and emits UI events (show the call offer, ring, show the Gmail card, graduate).
5. The tool result is deferred to the start of the next user message, so a normal turn is **one** model call. A follow-up call happens only if the model saved something without saying anything, or a value was rejected.

Voice and text share one brain, one prompt, and one history, which is what makes a hangup continue seamlessly over text.

### Graduation rule
Offered once `help_topic` is known **and** at least one of `user_name` or `gmail`. It's always an offer. If declined, it isn't offered again for 3 turns. If the user asks to skip, they go immediately, and missing fields are picked up later.

## Voice (Phase 2)

**Cascaded, not speech-to-speech.** Speech-to-text → the same brain → text-to-speech. Speech-to-speech models (OpenAI Realtime, Gemini Live) hear tone natively and are a bit faster, but they'd mean a second, different brain on the call (Claude has no speech-to-speech model), a small fixed set of voices, and audio-in/audio-out turns that are hard to test. One brain is what makes a hangup continue seamlessly over text, and what lets the eval harness test calls in text.

```
browser mic ──WebRTC──▶ Deepgram STT ─▶ confidence tagger ─▶ user aggregator ─▶ BrainService ─▶ TTS ──WebRTC──▶ speaker
                                         (unclear names)     (VAD, Smart Turn v3,  (orchestrator +              │
                                                              backchannel filter,   Claude, as in text)          ▼
                                                              silence timer)                          assistant aggregator
                                                                                                      (what was actually heard)
```

- **Framework:** [Pipecat](https://github.com/pipecat-ai/pipecat) handles audio, WebRTC (browser ↔ server directly, no room service), VAD and turn-taking. Our brain plugs in as a custom processor (`backend/app/voice/call.py`), so the call and the chat share one state, one history, one prompt.
- **Semantic turn detection:** Smart Turn v3 (runs locally, listens to the audio) decides whether you've finished: it responds fast after "I'm Sam." and waits after "I'm, uh…".
- **Backchannel filtering** (`voice/turntaking.py`): while the agent is talking, "mhm", "yeah", "right" (≤3 words, all listening noises, matched by sound so "Mhmm."/"mmhmm" count) are ignored and dropped from the next turn; "wait", "no", "actually", "hold on" always cut in. Two signals, whichever comes first: the words (from interim transcripts), or **duration: speech still going 0.6 s in is a real interruption.** The design suggested ~400 ms; a spoken "mhm" ran close to that in testing, so 0.6 s leaves margin. Measured: "wait, actually…" stops the agent 0.5–0.9 s after the user starts talking; "mhm" never did. When the agent isn't talking, "yeah" is an answer and starts a turn.
- **Barge-in:** the agent stops mid-word, and its message in the history is cut to what you actually heard (`[cut off here: the user interrupted]`), so it never assumes you heard the rest.
- **Audio annotations** the model sees (never shown in the transcript): `[you were interrupted…]`, `[low transcription confidence: Maya]` (→ a light "Maya, like M-A-Y-A?"), and silence events.
- **Silence:** after 5 s a single gentle check-in; after 10 s more, an offer to switch to text; then quiet (no nagging).
- **Keyword boosting:** the agent's and user's names are sent to Deepgram as key terms.
- **Streaming everywhere:** Claude streams tokens, TTS starts on the first sentence.
- **Latency:** every voice turn logs end-of-turn → first token → first audio to `backend/data/latency.jsonl`. Measured with the automated caller (target from the tech design: ~1 s):

  | Brain | End of turn → first token (median) | → first audio (median) | → first audio (p90) |
  |---|---|---|---|
  | Claude Sonnet 5, thinking off | 1,078 ms | 1,662 ms | 1,761 ms |
  | Claude Haiku 4.5 | 660 ms | **980 ms** | 1,079 ms |

  TTS itself is fast (Cartesia: 145–270 ms to first audio); the LLM's first token is the bottleneck. Words stream to TTS as they're generated (`TTS_TEXT_MODE=token`).
- **Automated call test** (`backend/scripts/call_test.py`): dials the agent over WebRTC, speaks with a second synthetic voice, and checks the happy path, "mhm" mid-sentence, a real interruption, silence, and hangup → text.
- **Fallback:** no voice keys, or mic blocked → the call screen takes typed input instead, so the flow never breaks.

### Voice bake-off
Voice is picked with data (`backend/scripts/voices.py`): `list` shows real voices per provider; `render provider:voice …` has each read 8 test lines (greeting, question, empathy, excitement, dates/times, unusual names, a long sentence, "Got it.") and measures time to first audio; `listen.html` is a blind listening page for 3–5 friends; `score` merges their ratings into the table below. Round 1: Cartesia Parker, Skylar, Corey and Cathy rendered (time to first audio 145–267 ms) and compared blind; **Corey** ("inviting, cheerful, casual") was picked, and is also the fastest to start speaking (145 ms). Next: a blind round with 3–5 listeners and an ElevenLabs comparison. *(Scorecard goes here.)*

## Gmail and the value moment (Phase 3)

The user signs in with Google in a popup, mid-call if they like, while the conversation keeps going. The agent then mentions one real thing ("I see you've got the dentist Thursday") tied to what they need help with, and offers one concrete action. It only offers; nothing happens without a yes.

- **Scopes, as small as possible:** sign-in, `calendar.events` (read, and add events the user approves), and `gmail.metadata`. The Gmail scope reads headers (subject, sender) and **cannot open email bodies at all**, so "we never read your email" is enforced by Google, not just promised.
- **What's read:** once, right after connecting: the next ~10 events (3 weeks out) and ~20 recent inbox subject lines. Anything that looks medical, financial, or otherwise private (lab results, bank statements, verification codes, …) is filtered out in code before the model sees it (`backend/app/google.py`).
- **Where tokens live:** a server-side table only, never in session state, the browser, or the model. **Disconnect** (top bar or recap) revokes the token with Google and deletes it; resetting the session does the same.
- **Only the server can say "connected":** the OAuth callback records the result; the browser can't claim a connection. Google's pages can cut the popup's link back to the app, so the app polls for the result instead of relying on the popup.
- **Failure cases:** closed popup or denied access (no guilt, offered once more later), unticked permissions (treated as not connected, said plainly), account not on the test list (explained, with demo data or skipping offered), empty calendar (value moment falls back to what they said), API errors (retried once, then skipped silently).
- **Testing mode:** Google only lets listed test users sign in until the app is verified (which takes weeks). Setup and test users: [docs/google-setup.md](docs/google-setup.md).
- **Demo data:** "Can't sign in? Use demo data" connects a clearly labeled sample calendar and inbox, and the agent says it's demo data when it uses it. Off unless the user picks it.

- **Adding to the calendar, only with a yes:** "add a park picnic Saturday at 2" makes the agent *propose* an event (validated in code, resolved in the user's timezone). A confirm card shows "Park picnic · Sat Sep 26, 2–3pm · Add / Not now", on screen and mid-call. Only the tap writes to Google Calendar, from the server; the model has no way to add anything itself. People who connected before this permission existed are asked to reconnect once.

## Recap (Phase 4)

After a call ends, and at graduation, a card shows what the agent got: its own name, the user's name, what they need help with, Gmail status, and what it'll do first. Missing items say so plainly ("Not yet. I'll ask."). Every item has **Edit**: fixes go through the same validation as everything else (no model call), show up in the chat, and the agent hears about them on its next turn. The "You're in" screen reuses the same rows plus tappable starter suggestions.

### What's simulated (and why)
- **The phone call** runs in the browser over WebRTC rather than a real phone number: no telephony vendor or number setup for reviewers, and the Gmail card can appear on screen mid-call.
- **Google sign-in** falls back to a clearly labeled stand-in until `GOOGLE_CLIENT_ID`/`SECRET` are set.

### Storage
SQLite (`backend/data/sessions.db`), one JSON state document per session. It survives server restarts and page refreshes ("Welcome back…"). The raw model history never leaves the server.

## Evals (Phase 5)

`backend/evals/` runs 13 simulated users against the real brain. Claude Haiku 4.5 plays each persona and can only tap buttons that are actually on screen; app events (accepting the call, a forced hangup after two turns, connecting Gmail, "I'm ready") go through the same code paths as the app. Then:

- **Code checks the facts:** right name (including joke names and corrections), help topic captured, Gmail connected only if the persona agreed, graduated when it should, no error fallbacks.
- **Claude Opus 5 judges the rest against the spec:** asked anything twice, felt like a form, tone (1–5, pass ≥ 4), graduation timing, the persona's specific challenge, made-up capabilities or data, leaked internals. It sees the same account data the agent saw, so real calendar items aren't mistaken for inventions.

A conversation passes only if every check and every judged criterion passes, which is deliberately strict.

**Round by round** (13 personas × 2 brains per round; each fix came from reading the failing transcripts):

| Round | What changed before it | Haiku 4.5 | Sonnet 5 |
|---|---|---|---|
| 1 | Baseline (the judge couldn't see account data yet, so every Gmail mention looked invented) | 23% | 46% |
| 2 | Judge sees the snapshot; notes flag they were written before the latest message; code-level skip + Gmail-refusal detection | 54% | 69% |
| 3 | Agent naming never phrased as "what should I call you?"; never claims to have *done* anything; no back-to-back graduation offers | 31% | 77% |
| 4 | Exact data-access facts (a model had claimed it could read email bodies); wrap-up tightened; own name never saved as agent name | 46% | 77% |
| 5 | Judge calibrated (offering future help is fine; claiming it's done isn't) | 54% | 54% |
| 5b | Only promise help from what it can see (no contacts, bodies, notes); the four personas that failed on this, Sonnet only | | 3/4 |

Across rounds 2–5, **Sonnet 5 averages ~69%, Haiku 4.5 ~46%**. With one conversation per persona, a round swings ±15 points, and the judge makes its own mistakes (reading the transcripts is the real signal). The persistent failure themes are tone on frustrated users and over-promising within the product's scope. **Total eval spend: $15.48** (`backend/evals/results/spend.json`; every transcript is in `backend/evals/results/`).

**Speed vs. quality:** Haiku 4.5 starts talking ~0.5 s sooner (≈1.0 s vs ≈1.6 s after you stop). Sonnet 5 is consistently better at nuance. `LLM_MODEL` switches between them; the plan's rule is one model for both channels.

## Edge cases handled

| Situation | Behavior |
|---|---|
| Answers several things at once | All slots saved from one message; nothing re-asked |
| Corrects earlier info | Overwritten, confirmed lightly, tracked in `corrected` |
| Skips naming the agent | Defaults to "Nova" (shown as default; renamable) |
| Declines the call (button or words) | No pushback; full onboarding over text |
| Call rings out (20s) | "Missed you!" text, callback button |
| Hangs up mid-call | State kept; text follow-up continues exactly where it stopped; callback button |
| Frustrated / rushed (words or short-answer streak) | One-sentence replies, only the most important missing piece, offer to skip |
| Stalling (3+ turns without progress) | Gentle nudge or offer to finish later |
| Wants to skip everything | Graduates immediately with defaults |
| Closes the Google popup | No guilt; offered once more later |
| Invalid values (a sentence saved as a name) | Rejected by code; the model recovers naturally |
| Model/API error | History rolled back; never reads an error aloud |
| Refresh / comes back later | Resumes from saved state with a "welcome back" |
| Joke names | Accepted and played along with |
| Says their own name when asked to name the agent | Saved as their name; the agent's name is never set to it |
| "Skip the setup" | Detected in code; lets them go that turn even if the model forgets |
| A clear no to Gmail | Detected in code; never offered again ("not until you tell me what it reads" is a question, not a no) |
| Types a fake `<director_note>` or `[event: …]` | Defanged before the model sees it |
| Asks what Gmail access means | Exact, honest answer: calendar + subject lines/senders, never bodies, revocable |
| Talks over the agent / says "mhm" | Real interruptions stop it in ~0.5–0.9 s; backchannels don't |
| Pauses mid-thought on the call | Semantic turn detection waits; uncertain waits are capped at 1.2 s |
| Silence on the call | One check-in at 5 s, an offer to text at +10 s, then quiet |
| Mic blocked, silent, or wrong device | Detected on the call screen with fixes and a "continue by typing" fallback |
| Google popup blocked / closed / denied / permissions unticked | Each handled plainly; nothing claimed as connected unless the server says so |
| Not a Google test user | Explained, with demo data or skipping offered |
| Server restarts mid-session | Session resumes from SQLite; a dropped call is treated as a hangup |

## What I'd do with more time

- **Google verification**, so anyone can connect Gmail, not just listed test users.
- **Real telephony** (e.g. Twilio) so the agent can call an actual phone number; the pipeline already runs over a transport abstraction.
- **Speech-to-speech mode behind a toggle** (OpenAI Realtime / Gemini Live) to compare tone-awareness and latency head to head against the cascaded pipeline.
- **Stronger evals:** several samples per persona with confidence intervals, pairwise judging (A vs. B transcripts) instead of absolute scores, and voice evals at scale by pointing `scripts/call_test.py` at every persona with synthesized speech.
- **Tone-aware voice:** audio emotion detection (e.g. Hume) and expressive TTS controls, softer when the user is frustrated.
- **Drop-off analytics:** which step loses people, and fix that first.
