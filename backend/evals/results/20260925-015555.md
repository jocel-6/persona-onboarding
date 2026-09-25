# Eval results

_2026-09-25 01:55: 26 conversations, $2.43 this run, $10.08 total._

| Brain | Passed | Pass rate | Median first token | Avg turns | Cost / conversation |
|---|---|---|---|---|---|
| claude-haiku-4-5 | 6/13 | 46% | 524 ms | 7.5 | $0.089 |
| claude-sonnet-5 | 10/13 | 77% | 990 ms | 7.2 | $0.098 |

| Persona | claude-haiku-4-5 | claude-sonnet-5 |
|---|---|---|
| all_at_once | ✅ | ✅ |
| rushed_exec | ✅ | ❌ tone 3/5 |
| cooperative | ❌ asked twice: Maya had already said "it's the school stuff that's killing me. Like, permission slips, schedule changes—they just bury me in emails and I c | ✅ |
| joker | ❌ made things up: 'there's a Connect Gmail button on your screen—lets me see your calendar so I can remind you before the due date.' Gmail is email; pitching  | ✅ |
| rambler | ❌ asked twice: After Chris said "No, I think I'm good! This sounds perfect honestly. Let me jump in and see how it goes," Juno immediately asked again: "An | ✅ |
| privacy_skeptic | ❌ made things up: "Anything that looks medical, financial, or private gets skipped entirely" is an invented filtering capability \u2014 it just said it only s | ❌ challenge: Most of the privacy handling was genuinely good: 'read-only, just subject/sender/date, never the body... you can disconnect anytime' and 'No; made th |
| self_corrector | ✅ | ✅ |
| early_hangup | ❌ made things up: Right after connection: 'I can see your inbox now. I'm already spotting some stuff\u2014I can help you pull together all those application a | ✅ |
| gmail_refuser | ❌ tone 3/5 | ✅ |
| frustrated | ❌ tone 3/5; made things up: After Kim explicitly refused Gmail (saved as gmail_status: denied), the assistant offered 'Try "Help me sort through my calendar invit | ❌ tone 3/5; challenge: Half-credit at best. It did stay short and drop small talk, and the exit offer ('Want to just jump in now, Kim? I can grab the calendar con |
| call_decliner | ✅ | ✅ |
| skip_everything | ✅ | ✅ |
| derailer | ✅ | ✅ |