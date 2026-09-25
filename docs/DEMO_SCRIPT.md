# Demo video script (2–3 minutes)

Record the screen with sound (macOS: Cmd+Shift+5 → record entire screen, Options → your mic). Use headphones so the agent's voice doesn't echo into the mic. Open **Show state** before you start: the live director's note on the right is what makes this look like engineering, not just a chatbot.

## 0:00–0:15 · The one-liner
Say: "This is Persona's onboarding: a voice and text agent that learns four things about you in a real conversation. The model handles language; code handles control flow. That panel on the right is what the code tells the model on every turn."

## 0:15–1:15 · Happy path
1. Tap a suggested name. It offers a call. Tap **Call me**, then **Answer**.
2. Talk naturally, with a pause mid-thought: "I'm… um… Maya, and honestly school emails are burying me."
   Point out: it waited through the pause, and **Show state** filled in name and help topic from one sentence.
3. Say "mhm" while it's talking (it keeps going), then cut in with "wait, actually…" (it stops).
4. When the Gmail card appears, connect (real account or demo data). It mentions one real item and offers, rather than acts.
5. Say yes to jumping in: starter ideas, "any questions?", then the recap card.

## 1:15–1:45 · Hangup recovery
1. Start over, get onto a call, and hit **End call** mid-sentence.
2. Point out: "Looks like we got cut off!" arrives by text, and it continues from exactly where it stopped, without re-asking anything.
3. Tap **Call me back**: it picks up where it left off.

## 1:45–2:20 · A difficult user
1. Start over. Type: "ugh this is taking forever, I have a meeting in five."
2. Point out the director's note: mood flips to **rushed**, the reply length drops to one sentence, and graduation is offered right away.
3. Type "can I just skip the setup?": it lets you go immediately (a code-level guarantee, not a prompt hope).

## 2:20–2:45 · Under the hood (optional, for technical viewers)
Flash the README: the eval table (13 simulated adversarial users, graded by code and by Claude Opus 5), the latency table (~1 s from end of turn to voice), and the engineering log of real bugs found.

Close with: "Everything's in the repo: the architecture doc, the decisions, the evals, and every transcript."
