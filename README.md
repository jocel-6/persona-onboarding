# Persona onboarding

An adaptive voice + text onboarding agent. It learns four things (what to call the agent, what to call you, a connected Gmail, and what you need help with) through a conversation that feels like the first five minutes of using Persona, not a form.

> Status: **Phase 0–1 done** (text brain, simulated call screen, Gmail stub). Voice (Phase 2), real Google sign-in (Phase 3), recap (Phase 4) and the eval harness (Phase 5) are next. See `Persona_Onboarding_Project_Plan.pdf` and `Persona_Onboarding_Technical_Design.pdf`.

## Run it locally

Requires Python 3.12+ and Node 20+.

```bash
cp .env.example .env            # add ANTHROPIC_API_KEY
cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && cd ..
cd frontend && npm install && cd ..
./dev.sh                        # backend :8000, frontend :3000
```

Open http://localhost:3000. "Show state" in the top bar shows the slot table, signals, and per-turn latency. "Start over" resets the session.

Tests: `cd backend && .venv/bin/python -m pytest` (these use a fake model, so they don't need an API key).

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

### What's simulated (and why)
- **The phone call** runs in the browser. In Phase 1 you type into the call screen and the turns are labeled `voice`, so voice-mode behavior (short spoken replies, hangup handling) can be tested before real audio is wired up.
- **Google sign-in** is a clearly labeled dev stub (`GMAIL_STUB=1`) until Phase 3.

### Storage
SQLite (`backend/data/sessions.db`), one JSON state document per session. It survives server restarts and page refreshes ("Welcome back…"). The raw model history never leaves the server.

## Edge cases handled so far

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
