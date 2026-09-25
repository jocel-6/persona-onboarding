# Persona onboarding

An adaptive voice + text onboarding agent. It learns four things (what to call the agent, what to call you, a connected Gmail, and what you need help with) through a conversation that feels like the first five minutes of using Persona, not a form.

> Status: **Phases 0–4 built**: text brain, real voice calls, real Gmail sign-in (needs Google credentials, see [docs/google-setup.md](docs/google-setup.md); demo data otherwise), and the recap. The eval harness (Phase 5) is next. See `Persona_Onboarding_Project_Plan.pdf` and `Persona_Onboarding_Technical_Design.pdf`.

## Run it locally

Requires Python 3.12+ and Node 20+.

```bash
cp .env.example .env            # add ANTHROPIC_API_KEY (+ voice keys for real calls, see below)
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
Voice is picked with data (`backend/scripts/voices.py`): `list` shows real voices per provider; `render provider:voice …` has each read 8 test lines (greeting, question, empathy, excitement, dates/times, unusual names, a long sentence, "Got it.") and measures time to first audio; `listen.html` is a blind listening page for 3–5 friends; `score` merges their ratings into the table below. Round 1 so far: Cartesia Parker, Skylar, Corey and Cathy rendered (time to first audio 145–267 ms); ElevenLabs pending. *(Scorecard goes here after the blind listening round.)*

## Gmail and the value moment (Phase 3)

The user signs in with Google in a popup, mid-call if they like, while the conversation keeps going. The agent then mentions one real thing ("I see you've got the dentist Thursday") tied to what they need help with, and offers one concrete action. It only offers; nothing happens without a yes.

- **Scopes, as small as possible:** sign-in, `calendar.events.readonly`, and `gmail.metadata`. The Gmail scope reads headers (subject, sender) and **cannot open email bodies at all**, so "we never read your email" is enforced by Google, not just promised.
- **What's read:** once, right after connecting: the next ~10 events (3 weeks out) and ~20 recent inbox subject lines. Anything that looks medical, financial, or otherwise private (lab results, bank statements, verification codes, …) is filtered out in code before the model sees it (`backend/app/google.py`).
- **Where tokens live:** a server-side table only, never in session state, the browser, or the model. **Disconnect** (top bar or recap) revokes the token with Google and deletes it; resetting the session does the same.
- **Only the server can say "connected":** the OAuth callback records the result; the browser can't claim a connection. Google's pages can cut the popup's link back to the app, so the app polls for the result instead of relying on the popup.
- **Failure cases:** closed popup or denied access (no guilt, offered once more later), unticked permissions (treated as not connected, said plainly), account not on the test list (explained, with demo data or skipping offered), empty calendar (value moment falls back to what they said), API errors (retried once, then skipped silently).
- **Testing mode:** Google only lets listed test users sign in until the app is verified (which takes weeks). Setup and test users: [docs/google-setup.md](docs/google-setup.md).
- **Demo data:** "Can't sign in? Use demo data" connects a clearly labeled sample calendar and inbox, and the agent says it's demo data when it uses it. Off unless the user picks it.

## Recap (Phase 4)

After a call ends, and at graduation, a card shows what the agent got: its own name, the user's name, what they need help with, Gmail status, and what it'll do first. Missing items say so plainly ("Not yet. I'll ask."). Every item has **Edit**: fixes go through the same validation as everything else (no model call), show up in the chat, and the agent hears about them on its next turn. The "You're in" screen reuses the same rows plus tappable starter suggestions.

### What's simulated (and why)
- **The phone call** runs in the browser over WebRTC rather than a real phone number: no telephony vendor or number setup for reviewers, and the Gmail card can appear on screen mid-call.
- **Google sign-in** falls back to a clearly labeled stand-in until `GOOGLE_CLIENT_ID`/`SECRET` are set.

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
