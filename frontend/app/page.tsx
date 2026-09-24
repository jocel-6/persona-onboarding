"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  createSession,
  deleteSession,
  getSession,
  streamPost,
  type Config,
  type EventType,
  type SessionState,
  type StreamEvent,
  type UiEvent,
} from "@/lib/api";
import { startVoiceCall, type VoiceCall } from "@/lib/voice";

const SESSION_KEY = "persona.session";
const RING_TIMEOUT_MS = 20_000;
const DEFAULT_CHIPS = ["Nova", "Juno", "Milo"];
// When the agent ends the call (graduation, "can we text instead?"), let it finish its sentence.
const END_AFTER_SPEECH_GRACE_MS = 1500;

type CallView = "none" | "ringing" | "live";

export default function Onboarding() {
  const [session, setSession] = useState<SessionState | null>(null);
  const [config, setConfig] = useState<Config>({ gmail_stub: false });
  const [live, setLive] = useState<string | null>(null);
  const [pendingUser, setPendingUser] = useState<string | null>(null);
  const [waiting, setWaiting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [chips, setChips] = useState<string[] | null>(null);
  const [callOffer, setCallOffer] = useState(false);
  const [callback, setCallback] = useState(false);
  const [gmailCard, setGmailCard] = useState(false);
  const [gmailPopup, setGmailPopup] = useState(false);
  const [callView, setCallView] = useState<CallView>("none");
  const [doneDismissed, setDoneDismissed] = useState(false);
  const [debug, setDebug] = useState(false);
  const [latency, setLatency] = useState<{ ttft_ms: number | null; total_ms: number } | null>(null);

  // Real voice call state (Phase 2). Without voice configured, calls fall back to typing.
  const voiceRef = useRef<VoiceCall | null>(null);
  const [voiceLive, setVoiceLive] = useState(false);
  const [botSpeaking, setBotSpeaking] = useState(false);
  const [userSpeaking, setUserSpeaking] = useState(false);
  const [muted, setMuted] = useState(false);
  const [callNote, setCallNote] = useState<string | null>(null);
  const botSpeakingRef = useRef(false);
  const endAfterSpeechRef = useRef(false);
  const voiceTextRef = useRef("");
  // Set below once finishVoice exists; lets handleUi/applyState end a voice call gracefully.
  const requestEndAfterSpeechRef = useRef<(() => void) | null>(null);

  const queue = useRef<Promise<void>>(Promise.resolve());
  // Lets handleUi queue an event (e.g. ring after "yes, call me") without a dependency cycle.
  const enqueueRef = useRef<((t: EventType) => void) | null>(null);
  const sessionId = session?.session_id;

  // ---- server events -------------------------------------------------

  const handleUi = useCallback((ui: UiEvent) => {
    switch (ui.type) {
      case "name_suggestions":
        setChips(ui.names);
        break;
      case "show_call_offer":
        setChips(null);
        setCallOffer(true);
        break;
      case "hide_call_offer":
        setCallOffer(false);
        break;
      case "start_call":
        // The model heard "yes, call me": ring after this turn finishes.
        enqueueRef.current?.("call_accepted");
        break;
      case "ringing":
        setCallOffer(false);
        setCallback(false);
        setCallView("ringing");
        break;
      case "end_call":
        if (voiceRef.current) requestEndAfterSpeechRef.current?.();
        else setCallView("none");
        break;
      case "show_callback":
        setCallback(true);
        break;
      case "show_gmail_card":
        setGmailCard(true);
        break;
      case "hide_gmail_card":
        setGmailCard(false);
        break;
    }
  }, []);

  const applyState = useCallback((s: SessionState) => {
    setSession(s);
    if (s.call_status !== "in_progress") {
      if (voiceRef.current) requestEndAfterSpeechRef.current?.();
      else setCallView((v) => (v === "live" ? "none" : v));
    }
    if (s.agent_name) setChips(null);
  }, []);

  // ---- voice ---------------------------------------------------------

  /** Close the voice connection without telling the server it was a hangup. */
  const finishVoice = useCallback(async () => {
    const v = voiceRef.current;
    voiceRef.current = null;
    endAfterSpeechRef.current = false;
    setVoiceLive(false);
    setBotSpeaking(false);
    setUserSpeaking(false);
    setCallView("none");
    if (v) await v.hangUp();
  }, []);

  useEffect(() => {
    requestEndAfterSpeechRef.current = () => {
      if (endAfterSpeechRef.current) return;
      endAfterSpeechRef.current = true;
      // If the goodbye hasn't started playing yet, give it a moment; otherwise
      // onBotSpeaking(false) finishes the call when it's done.
      setTimeout(() => {
        if (endAfterSpeechRef.current && !botSpeakingRef.current) void finishVoice();
      }, END_AFTER_SPEECH_GRACE_MS);
    };
  }, [finishVoice]);

  const handleVoiceEvent = useCallback(
    (e: StreamEvent) => {
      if (e.type === "delta") {
        voiceTextRef.current += e.text;
        setLive(voiceTextRef.current);
      } else if (e.type === "ui") handleUi(e.ui);
      else if (e.type === "done") setLatency(e.latency);
      else if (e.type === "state") {
        voiceTextRef.current = "";
        applyState(e.state);
        setLive(null);
        setPendingUser(null);
      }
    },
    [applyState, handleUi],
  );


  const run = useCallback(
    (path: string, body: unknown) => {
      queue.current = queue.current.then(async () => {
        setError(null);
        let text = "";
        try {
          await streamPost(path, body, (e: StreamEvent) => {
            if (e.type === "delta") {
              setWaiting(false);
              text += e.text;
              setLive(text);
            } else if (e.type === "ui") handleUi(e.ui);
            else if (e.type === "done") setLatency(e.latency);
            else if (e.type === "state") {
              applyState(e.state);
              setLive(null);
              setPendingUser(null);
            }
          });
        } catch {
          setError("Couldn't reach Persona. Check that the backend is running, then try again.");
        } finally {
          setWaiting(false);
          setLive(null);
          setPendingUser(null);
        }
      });
      return queue.current;
    },
    [applyState, handleUi],
  );

  const sendEvent = useCallback(
    (type: EventType, data: Record<string, unknown> = {}) => {
      if (!sessionId) return;
      return run(`/api/sessions/${sessionId}/events`, { type, data });
    },
    [run, sessionId],
  );
  useEffect(() => {
    enqueueRef.current = (t) => void sendEvent(t);
  }, [sendEvent]);

  const sendMessage = useCallback(
    (text: string) => {
      const t = text.trim();
      if (!t || !sessionId) return;
      setPendingUser(t);
      setWaiting(true);
      setChips(null);
      return run(`/api/sessions/${sessionId}/messages`, { text: t });
    },
    [run, sessionId],
  );

  // ---- boot / resume -------------------------------------------------

  const boot = useCallback(async (fresh = false) => {
    try {
      let id: string | null = null;
      try {
        id = fresh ? null : localStorage.getItem(SESSION_KEY);
      } catch {}
      if (id) {
        const got = await getSession(id);
        if (got) {
          const s = got.state;
          setConfig(got.config);
          applyState(s);
          setCallOffer(s.call_status === "offered");
          setCallback(s.call_status === "hung_up" || s.call_status === "missed" || s.call_status === "in_progress");
          setGmailCard(s.gmail_card_shown && s.gmail_status !== "connected");
          if (!s.agent_name && s.user_turns === 0) setChips(DEFAULT_CHIPS);
          if (s.user_turns > 0 && !s.graduated) {
            await run(`/api/sessions/${s.session_id}/events`, { type: "resumed", data: {} });
          }
          return;
        }
      }
      const created = await createSession();
      try {
        localStorage.setItem(SESSION_KEY, created.state.session_id);
      } catch {}
      setConfig(created.config);
      applyState(created.state);
      created.ui.forEach(handleUi);
    } catch {
      setError("Couldn't reach Persona. Is the backend running on port 8000?");
    }
  }, [applyState, handleUi, run]);

  // Dev mode runs mount effects twice; boot once so we don't create two sessions.
  const booted = useRef(false);
  useEffect(() => {
    if (booted.current) return;
    booted.current = true;
    void boot();
  }, [boot]);

  const reset = async () => {
    await finishVoice();
    if (sessionId) await deleteSession(sessionId);
    setError(null);
    setCallOffer(false);
    setCallback(false);
    setGmailCard(false);
    setCallView("none");
    setDoneDismissed(false);
    setLatency(null);
    await boot(true);
  };

  // ---- call lifecycle ------------------------------------------------

  useEffect(() => {
    if (callView !== "ringing") return;
    const t = setTimeout(() => {
      setCallView("none");
      void sendEvent("call_missed");
    }, RING_TIMEOUT_MS);
    return () => clearTimeout(t);
  }, [callView, sendEvent]);

  const answer = async () => {
    setCallView("live");
    setCallback(false);
    setCallNote(null);
    setMuted(false);
    if (!config.voice || !sessionId) {
      void sendEvent("call_connected"); // typed call
      return;
    }
    try {
      voiceTextRef.current = "";
      voiceRef.current = await startVoiceCall(sessionId, {
        onEvent: handleVoiceEvent,
        onUserTranscript: (text) => setPendingUser(text),
        onBotSpeaking: (speaking) => {
          botSpeakingRef.current = speaking;
          setBotSpeaking(speaking);
          if (!speaking && endAfterSpeechRef.current) void finishVoice();
        },
        onUserSpeaking: setUserSpeaking,
        onDropped: () => {
          if (!voiceRef.current) return;
          voiceRef.current = null;
          setVoiceLive(false);
          setCallView("none");
          void sendEvent("hangup");
        },
      });
      setVoiceLive(true);
    } catch {
      // Mic blocked or voice server unreachable: keep the call going by typing.
      voiceRef.current = null;
      setCallNote("Couldn't use your microphone, so let's type on this call instead.");
      void sendEvent("call_connected");
    }
  };
  const declineRing = () => {
    setCallView("none");
    void sendEvent("call_declined");
  };
  const hangUp = async () => {
    await finishVoice();
    setCallView("none");
    void sendEvent("hangup");
  };

  // ---- render --------------------------------------------------------

  const agentName = session?.agent_name ?? "Persona";
  const transcript = session?.transcript ?? [];
  const graduated = !!session?.graduated;

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="dot" aria-hidden />
          <span>{agentName}</span>
          {session?.agent_name_defaulted && <span className="muted small">(default name)</span>}
        </div>
        <div className="topbar-actions">
          <button className="ghost small" onClick={() => setDebug((d) => !d)} aria-pressed={debug}>
            {debug ? "Hide" : "Show"} state
          </button>
          <button className="ghost small" onClick={reset}>
            Start over
          </button>
        </div>
      </header>

      <main className="layout">
        <section className="chat" aria-live="polite">
          <Thread
            transcript={transcript}
            pendingUser={pendingUser}
            live={callView === "live" ? null : live}
            waiting={waiting && callView !== "live"}
          />

          {error && (
            <div className="banner error" role="alert">
              {error}
            </div>
          )}

          <div className="actions">
            {chips && !session?.agent_name && (
              <div className="chips">
                {chips.map((n) => (
                  <button key={n} className="chip" onClick={() => sendMessage(n)}>
                    {n}
                  </button>
                ))}
                <button className="chip subtle" onClick={() => sendEvent("name_skipped")}>
                  Skip for now
                </button>
              </div>
            )}

            {callOffer && callView === "none" && (
              <div className="card">
                <div>
                  <strong>Quick 2-minute call?</strong>
                  <p className="muted small">{agentName} calls you right here in the browser. Or keep texting, both work.</p>
                </div>
                <div className="row">
                  <button className="primary" onClick={() => sendEvent("call_accepted")}>
                    Call me
                  </button>
                  <button className="ghost" onClick={() => { setCallOffer(false); void sendEvent("call_declined"); }}>
                    Keep texting
                  </button>
                </div>
              </div>
            )}

            {callback && callView === "none" && !graduated && (
              <div className="row">
                <button className="secondary" onClick={() => { setCallback(false); void sendEvent("callback"); }}>
                  📞 Call me back
                </button>
              </div>
            )}

            {session?.wrapping_up && !graduated && callView === "none" && (
              <div className="row">
                <button className="primary" onClick={() => sendEvent("graduate")}>
                  I&apos;m ready, let&apos;s go
                </button>
              </div>
            )}

            {gmailCard && callView === "none" && (
              <GmailCard
                stub={config.gmail_stub}
                onConnect={() => { setGmailPopup(true); void sendEvent("gmail_popup_opened"); }}
                onDismiss={() => sendEvent("gmail_closed")}
              />
            )}
          </div>

          <Composer
            placeholder={graduated ? `Message ${agentName}` : "Type a message"}
            onSend={sendMessage}
            disabled={!session}
          />
        </section>

        {debug && session && <DebugPanel s={session} latency={latency} />}
      </main>

      {callView !== "none" && (
        <CallScreen
          key={callView}
          view={callView}
          agentName={agentName}
          transcript={transcript}
          live={live}
          pendingUser={pendingUser}
          onAnswer={answer}
          onDecline={declineRing}
          onHangUp={hangUp}
          onSay={sendMessage}
          voice={voiceLive}
          botSpeaking={botSpeaking}
          userSpeaking={userSpeaking}
          muted={muted}
          onMute={() => { const m = !muted; setMuted(m); voiceRef.current?.setMuted(m); }}
          note={callNote ?? (config.voice ? null : "Voice isn't set up on this server, so type to talk.")}
          onReady={session?.wrapping_up && !graduated ? () => sendEvent("graduate") : undefined}
          gmail={
            gmailCard ? (
              <GmailCard
                stub={config.gmail_stub}
                compact
                onConnect={() => { setGmailPopup(true); void sendEvent("gmail_popup_opened"); }}
                onDismiss={() => sendEvent("gmail_closed")}
              />
            ) : null
          }
        />
      )}

      {gmailPopup && (
        <GoogleStubPopup
          onAllow={(email, name) => { setGmailPopup(false); void sendEvent("gmail_connected", { email, name }); }}
          onCancel={() => { setGmailPopup(false); void sendEvent("gmail_closed"); }}
        />
      )}

      {graduated && !doneDismissed && callView === "none" && session && (
        <DonePanel
          s={session}
          onClose={() => setDoneDismissed(true)}
          onStart={(text) => { setDoneDismissed(true); void sendMessage(text); }}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------

function Thread({
  transcript,
  pendingUser,
  live,
  waiting,
}: {
  transcript: SessionState["transcript"];
  pendingUser: string | null;
  live: string | null;
  waiting: boolean;
}) {
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => {
    end.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [transcript.length, pendingUser, live, waiting]);

  return (
    <div className="thread">
      {transcript.map((t, i) =>
        t.role === "event" ? (
          <div key={i} className="event">
            {t.text}
          </div>
        ) : (
          <div key={i} className={`bubble ${t.role}`}>
            {t.channel === "voice" && <span className="tag">on call</span>}
            {t.text}
          </div>
        ),
      )}
      {pendingUser && <div className="bubble user">{pendingUser}</div>}
      {live && <div className="bubble agent">{live}</div>}
      {waiting && !live && (
        <div className="bubble agent typing" aria-label="typing">
          <span />
          <span />
          <span />
        </div>
      )}
      <div ref={end} />
    </div>
  );
}

function Composer({ onSend, placeholder, disabled }: { onSend: (t: string) => void; placeholder: string; disabled?: boolean }) {
  const [text, setText] = useState("");
  const submit = () => {
    if (!text.trim()) return;
    onSend(text);
    setText("");
  };
  return (
    <form className="composer" onSubmit={(e) => { e.preventDefault(); submit(); }}>
      <input
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder={placeholder}
        disabled={disabled}
        aria-label="Message"
        autoFocus
      />
      <button className="primary" type="submit" disabled={disabled || !text.trim()}>
        Send
      </button>
    </form>
  );
}

function GmailCard({
  stub,
  compact,
  onConnect,
  onDismiss,
}: {
  stub: boolean;
  compact?: boolean;
  onConnect: () => void;
  onDismiss: () => void;
}) {
  return (
    <div className={`card gmail ${compact ? "compact" : ""}`}>
      <div>
        <strong>Connect Gmail</strong>
        <p className="muted small">
          Read-only. I&apos;ll look at upcoming calendar events and recent email subject lines. I never send anything or
          change your calendar, and you can disconnect anytime.
        </p>
      </div>
      <div className="row">
        <button className="primary" onClick={onConnect} disabled={!stub} title={stub ? "" : "Google sign-in arrives in Phase 3"}>
          Connect Gmail
        </button>
        <button className="ghost" onClick={onDismiss}>
          Not now
        </button>
      </div>
    </div>
  );
}

function GoogleStubPopup({ onAllow, onCancel }: { onAllow: (email: string, name: string) => void; onCancel: () => void }) {
  const [email, setEmail] = useState("you@gmail.com");
  const [name, setName] = useState("Margaret Chen");
  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="Google sign-in (dev stub)">
      <div className="modal">
        <p className="stub-label">Dev stub · real Google sign-in comes in Phase 3</p>
        <h2>Sign in with Google</h2>
        <label className="small muted" htmlFor="stub-email">
          Account
        </label>
        <input id="stub-email" value={email} onChange={(e) => setEmail(e.target.value)} />
        <label className="small muted" htmlFor="stub-name">
          Name on the Google account
        </label>
        <input id="stub-name" value={name} onChange={(e) => setName(e.target.value)} />
        <p className="small muted">Persona wants read-only access to your calendar and email subject lines.</p>
        <div className="row end">
          <button className="ghost" onClick={onCancel}>
            Cancel
          </button>
          <button className="primary" onClick={() => onAllow(email, name)}>
            Allow
          </button>
        </div>
      </div>
    </div>
  );
}

function CallScreen({
  view,
  agentName,
  transcript,
  live,
  pendingUser,
  onAnswer,
  onDecline,
  onHangUp,
  onSay,
  voice,
  botSpeaking,
  userSpeaking,
  muted,
  onMute,
  note,
  onReady,
  gmail,
}: {
  view: CallView;
  agentName: string;
  transcript: SessionState["transcript"];
  live: string | null;
  pendingUser: string | null;
  onAnswer: () => void;
  onDecline: () => void;
  onHangUp: () => void;
  onSay: (t: string) => void;
  voice: boolean;
  botSpeaking: boolean;
  userSpeaking: boolean;
  muted: boolean;
  onMute: () => void;
  note: string | null;
  onReady?: () => void;
  gmail: React.ReactNode;
}) {
  const [seconds, setSeconds] = useState(0);
  useEffect(() => {
    if (view !== "live") return;
    const t = setInterval(() => setSeconds((s) => s + 1), 1000);
    return () => clearInterval(t);
  }, [view]);

  const lastAgentVoice = [...transcript].reverse().find((t) => t.role === "agent" && t.channel === "voice")?.text;
  const caption = live ?? lastAgentVoice ?? "";
  const mmss = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;

  return (
    <div className="call" role="dialog" aria-label={`Call with ${agentName}`}>
      <div className="call-inner">
        <div
          className={`avatar ${view === "ringing" ? "ringing" : (voice ? botSpeaking : live) ? "speaking" : ""} ${
            voice && userSpeaking ? "listening" : ""
          }`}
        >
          {agentName.slice(0, 1).toUpperCase()}
        </div>
        <h2>{agentName}</h2>
        <p className="muted">
          {view === "ringing"
            ? "Incoming call…"
            : voice
              ? `${mmss} · ${muted ? "muted" : userSpeaking ? "listening…" : botSpeaking ? "speaking" : "your turn"}`
              : mmss}
        </p>

        {view === "ringing" ? (
          <div className="row center">
            <button className="decline" onClick={onDecline}>
              Decline
            </button>
            <button className="accept" onClick={onAnswer}>
              Answer
            </button>
          </div>
        ) : (
          <>
            <div className="caption" aria-live="polite">
              {pendingUser && <p className="you">You: {pendingUser}</p>}
              {caption && <p>{caption}</p>}
            </div>
            {gmail}
            {voice ? (
              <button className="secondary wide" onClick={onMute} aria-pressed={muted}>
                {muted ? "Unmute" : "Mute"}
              </button>
            ) : (
              <>
                {note && <p className="stub-label">{note}</p>}
                <Composer placeholder="Say something…" onSend={onSay} />
              </>
            )}
            {onReady && (
              <button className="primary wide" onClick={onReady}>
                I&apos;m ready, let&apos;s go
              </button>
            )}
            <button className="decline wide" onClick={onHangUp}>
              End call
            </button>
          </>
        )}
      </div>
    </div>
  );
}

function DonePanel({ s, onClose, onStart }: { s: SessionState; onClose: () => void; onStart: (text: string) => void }) {
  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="You're in">
      <div className="modal done">
        <h2>You&apos;re in.</h2>
        <p className="muted">{s.agent_name} is ready. Here&apos;s what it knows so far:</p>
        <dl>
          <dt>Your name</dt>
          <dd>{s.user_name ?? <span className="muted">Not yet. It&apos;ll ask later.</span>}</dd>
          <dt>Needs help with</dt>
          <dd>{s.help_topic ?? <span className="muted">Not yet</span>}</dd>
          <dt>Gmail</dt>
          <dd>{s.gmail_status === "connected" ? s.gmail : <span className="muted">Not connected. You can connect it anytime.</span>}</dd>
        </dl>
        {s.starter_suggestions.length > 0 ? (
          <>
            <p className="small muted">Try one of these first:</p>
            <div className="starters">
              {s.starter_suggestions.map((t) => (
                <button key={t} className="starter" onClick={() => onStart(t)}>
                  {t}
                </button>
              ))}
            </div>
          </>
        ) : (
          s.help_topic && (
            <p>
              <strong>First up:</strong> {s.help_topic}
            </p>
          )
        )}
        <div className="row end">
          <button className={s.starter_suggestions.length ? "ghost" : "primary"} onClick={onClose}>
            {s.starter_suggestions.length ? "I'll explore on my own" : "Let's go"}
          </button>
        </div>
      </div>
    </div>
  );
}

function DebugPanel({ s, latency }: { s: SessionState; latency: { ttft_ms: number | null; total_ms: number } | null }) {
  const rows: [string, string][] = [
    ["agent_name", s.agent_name ?? "—"],
    ["user_name", s.user_name ?? "—"],
    ["help_topic", s.help_topic ?? "—"],
    ["gmail", `${s.gmail ?? "—"} (${s.gmail_status})`],
    ["channel", s.channel],
    ["call_status", s.call_status],
    ["sentiment", s.sentiment],
    ["short_answer_streak", String(s.short_answer_streak)],
    ["turns_since_progress", String(s.turns_since_progress)],
    ["graduation_offered", String(s.graduation_offered)],
    ["graduated", String(s.graduated)],
    ["corrected", s.corrected.join(", ") || "—"],
    ["last turn", latency ? `first token ${latency.ttft_ms ?? "—"}ms · total ${latency.total_ms}ms` : "—"],
  ];
  return (
    <aside className="debug" aria-label="Session state">
      <h3>Under the hood</h3>
      <dl>
        {rows.map(([k, v]) => (
          <div key={k}>
            <dt>{k}</dt>
            <dd>{v}</dd>
          </div>
        ))}
      </dl>
    </aside>
  );
}
