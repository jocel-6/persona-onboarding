"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  API_URL,
  addInsightFix,
  draftReply,
  pickVoice,
  voiceSampleUrl,
  createSession,
  deleteSession,
  editField,
  getSession,
  streamPost,
  type Config,
  type EditableField,
  type EventType,
  type SessionState,
  type StreamEvent,
  type TurnLatency,
  type TurnUsage,
  type CalendarEvent,
  type Insight,
  type Tomorrow,
  type VoiceChoice,
  type UiEvent,
} from "@/lib/api";
import { startVoiceCall, type VoiceCall } from "@/lib/voice";

const SESSION_KEY = "persona.session";
const RING_TIMEOUT_MS = 20_000;
const DEFAULT_CHIPS = ["Nova", "Juno", "Milo"];
const GOOGLE_POLL_MS = 1500;
const GOOGLE_TIMEOUT_MS = 3 * 60_000;
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
  const [googleWaiting, setGoogleWaiting] = useState(false);
  const googleCleanup = useRef<(() => void) | null>(null);
  const cancelGoogleRef = useRef<(() => void) | null>(null);
  const [callView, setCallView] = useState<CallView>("none");
  const [doneDismissed, setDoneDismissed] = useState(false);
  const [recapDismissed, setRecapDismissed] = useState(false);
  const [insightsDismissed, setInsightsDismissed] = useState(false);
  const [voicePickerDone, setVoicePickerDone] = useState(false);
  const [knowsOpen, setKnowsOpen] = useState(false);
  const [debug, setDebug] = useState(false);
  const [latency, setLatency] = useState<TurnLatency | null>(null);
  const [usage, setUsage] = useState<TurnUsage | null>(null);

  // Real voice call state (Phase 2). Without voice configured, calls fall back to typing.
  const voiceRef = useRef<VoiceCall | null>(null);
  const [voiceLive, setVoiceLive] = useState(false);
  const [botSpeaking, setBotSpeaking] = useState(false);
  const [userSpeaking, setUserSpeaking] = useState(false);
  const [muted, setMuted] = useState(false);
  const [callNote, setCallNote] = useState<string | null>(null);
  const [micLevel, setMicLevel] = useState(0);
  const [botLevel, setBotLevel] = useState(0);
  const [spokenCaption, setSpokenCaption] = useState("");
  const [micName, setMicName] = useState<string | null>(null);
  const [micProblem, setMicProblem] = useState<string | null>(null);
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
      else if (e.type === "done") {
        setLatency(e.latency);
        setUsage(e.usage ?? null);
      } else if (e.type === "state") {
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
            else if (e.type === "done") {
              setLatency(e.latency);
              setUsage(e.usage ?? null);
            } else if (e.type === "state") {
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
    setRecapDismissed(false);
    setInsightsDismissed(false);
    setVoicePickerDone(false);
    setLatency(null);
    await boot(true);
  };

  // ---- Gmail ----------------------------------------------------------

  /**
   * Real mode: Google sign-in in a popup. Google's pages can cut the popup's link back
   * to us, so we don't rely on postMessage or window.closed: the server records how
   * sign-in ended and we poll for it (postMessage is just a faster path when it works).
   */
  const connectGmail = useCallback(() => {
    if (!sessionId) return;
    if (config.gmail_mode !== "google") {
      setGmailPopup(true);
      void sendEvent("gmail_popup_opened");
      return;
    }
    // Must open synchronously on the click, or popup blockers step in.
    const w = window.open(
      `${API_URL}/api/google/start?session_id=${sessionId}`,
      "persona-google",
      "width=500,height=680",
    );
    if (!w) {
      setError("Your browser blocked the Google sign-in window. Allow pop-ups for this site, then try again.");
      return;
    }
    void sendEvent("gmail_popup_opened");
    setGoogleWaiting(true);
    let done = false;
    const finish = (result: string) => {
      if (done) return;
      done = true;
      googleCleanup.current?.();
      setGoogleWaiting(false);
      try {
        w.close();
      } catch {}
      if (result === "ok") void sendEvent("gmail_connected");
      else if (result === "denied") void sendEvent("gmail_denied");
      else void sendEvent("gmail_closed", { reason: result });
    };
    const onMessage = (e: MessageEvent) => {
      if (e.origin !== new URL(API_URL).origin || e.data?.source !== "persona-google") return;
      finish(e.data.ok ? "ok" : String(e.data.reason || "error"));
    };
    window.addEventListener("message", onMessage);
    const poll = setInterval(async () => {
      try {
        const got = await getSession(sessionId);
        const r = got?.state.google_result;
        if (r) finish(r);
      } catch {}
    }, GOOGLE_POLL_MS);
    const timeout = setTimeout(() => finish("closed"), GOOGLE_TIMEOUT_MS);
    googleCleanup.current = () => {
      window.removeEventListener("message", onMessage);
      clearInterval(poll);
      clearTimeout(timeout);
      googleCleanup.current = null;
    };
    // Cancel = the user gave up on the popup.
    cancelGoogleRef.current = () => finish("closed");
  }, [config.gmail_mode, sendEvent, sessionId]);
  useEffect(() => () => googleCleanup.current?.(), []);

  const chooseDemoData = useCallback(() => {
    googleCleanup.current?.();
    setGoogleWaiting(false);
    void sendEvent("gmail_connected", { demo: true });
  }, [sendEvent]);

  const gmailCardProps = {
    mode: config.gmail_mode ?? (config.gmail_stub ? "stub" : "google"),
    demo: !!config.demo_data,
    waiting: googleWaiting,
    onConnect: connectGmail,
    onCancel: () => cancelGoogleRef.current?.(),
    onDemo: chooseDemoData,
    onDismiss: () => sendEvent("gmail_closed"),
  } as const;

  // #6: deep findings arrive a few seconds after connecting; pick them up even between turns.
  const connectedAt = session?.gmail_status === "connected" ? session.session_id : null;
  const hasDeep = !!session?.insights?.some((i) => i.kind === "deep");
  useEffect(() => {
    if (!connectedAt || hasDeep) return;
    let tries = 0;
    const t = setInterval(async () => {
      tries += 1;
      try {
        const got = await getSession(connectedAt);
        if (got?.state.insights?.some((i) => i.kind === "deep")) {
          setSession((cur) => (cur ? { ...cur, insights: got.state.insights } : cur));
          clearInterval(t);
        }
      } catch {}
      if (tries >= 12) clearInterval(t);
    }, 2500);
    return () => clearInterval(t);
  }, [connectedAt, hasDeep]);

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
    setRecapDismissed(false);
    setCallView("live");
    setCallback(false);
    setCallNote(null);
    setMuted(false);
    setMicProblem(null);
    setMicLevel(0);
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
          if (speaking) setSpokenCaption("");
          if (!speaking && endAfterSpeechRef.current) void finishVoice();
        },
        onUserSpeaking: setUserSpeaking,
        onBotLevel: setBotLevel,
        onBotWord: (w) => setSpokenCaption((c) => (c ? `${c} ${w}` : w)),
        onMicLevel: setMicLevel,
        onMicName: setMicName,
        onMicProblem: setMicProblem,
        onDropped: () => {
          if (!voiceRef.current) return;
          voiceRef.current = null;
          setVoiceLive(false);
          setCallView("none");
          void sendEvent("hangup");
        },
      }, config.ice_servers);
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
          {session?.gmail_status === "connected" && (
            <span className="pill">
              {session.gmail_demo ? "Demo data" : "Gmail connected"}
              <button className="link" onClick={() => sendEvent("gmail_disconnected")}>
                Disconnect
              </button>
            </span>
          )}
          {session && (
            <button className="ghost small" onClick={() => setKnowsOpen(true)}>
              What I know
            </button>
          )}
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
          {session && <ProfileStrip s={session} />}
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

            {session?.agent_name && callView === "none" && !graduated && (config.voice_choices?.length ?? 0) > 0 &&
              !session.voice_id && !voicePickerDone && (
                <VoicePicker
                  agentName={session.agent_name}
                  choices={config.voice_choices ?? []}
                  onPick={(v) => void pickVoice(session.session_id, v.id).then(applyState).catch(() => {})}
                  onDone={() => setVoicePickerDone(true)}
                />
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

            {session && session.gmail_status === "connected" && session.insights?.length > 0 && !insightsDismissed &&
              callView === "none" && (
                <InsightsCard
                  insights={session.insights}
                  demo={session.gmail_demo}
                  onFix={(i) =>
                    void (i.action?.type === "draft_reply"
                      ? draftReply(session.session_id, i.id)
                      : addInsightFix(session.session_id, i.id)
                    ).then(applyState).catch((e) => setError(String(e.message)))
                  }
                  onAsk={(i) => sendMessage(`Tell me more about this: ${i.headline}`)}
                  onDismiss={() => setInsightsDismissed(true)}
                />
              )}

            {session?.graduated && session.tomorrow && session.gmail_status === "connected" && doneDismissed &&
              callView === "none" && <TomorrowCard t={session.tomorrow} />}

            {session?.pending_draft && callView === "none" && (
              <DraftCard
                key={session.pending_draft.body}
                draft={session.pending_draft}
                demo={session.gmail_demo}
                onSave={(body) => sendEvent("draft_confirmed", { body })}
                onCancel={() => sendEvent("draft_cancelled")}
              />
            )}

            {session?.pending_event && callView === "none" && (
              <EventConfirmCard
                event={session.pending_event}
                demo={session.gmail_demo}
                onAdd={() => sendEvent("event_confirmed")}
                onCancel={() => sendEvent("event_cancelled")}
              />
            )}

            {session && showRecap(session) && !graduated && !recapDismissed && callView === "none" && (
              <RecapCard
                s={session}
                onSaved={applyState}
                onConnectGmail={() => setGmailCard(true)}
                onDisconnectGmail={() => sendEvent("gmail_disconnected")}
                onDismiss={() => setRecapDismissed(true)}
              />
            )}

            {gmailCard && callView === "none" && (
              <GmailCard {...gmailCardProps} />
            )}
          </div>

          <Composer
            placeholder={graduated ? `Message ${agentName}` : "Type a message"}
            onSend={sendMessage}
            disabled={!session}
          />
        </section>

        {session && <ProfilePanel s={session} />}
        {debug && session && <DebugPanel s={session} latency={latency} usage={usage} />}
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
          botLevel={botLevel}
          spokenCaption={spokenCaption}
          micLevel={micLevel}
          micName={micName}
          micProblem={micProblem}
          onTypeInstead={async () => {
            // Keep the call going, just typed: drop the audio connection but not the call.
            await finishVoice();
            setCallView("live");
            setMicProblem(null);
            setCallNote("Typing instead of talking. The call is still on.");
          }}
          botSpeaking={botSpeaking}
          userSpeaking={userSpeaking}
          muted={muted}
          onMute={() => { const m = !muted; setMuted(m); voiceRef.current?.setMuted(m); }}
          note={callNote ?? (config.voice ? null : "Voice isn't set up on this server, so type to talk.")}
          onReady={session?.wrapping_up && !graduated ? () => sendEvent("graduate") : undefined}
          gmail={
            <>
              {session && session.gmail_status === "connected" && session.insights?.length > 0 && !session.pending_event && (
                <InsightsCard
                  compact
                  insights={session.insights}
                  demo={session.gmail_demo}
                  onFix={(i) =>
                    void (i.action?.type === "draft_reply"
                      ? draftReply(session.session_id, i.id)
                      : addInsightFix(session.session_id, i.id)
                    ).then(applyState).catch(() => {})
                  }
                  onAsk={(i) => sendMessage(`Tell me more about this: ${i.headline}`)}
                />
              )}
              {session?.pending_event && (
                <EventConfirmCard
                  event={session.pending_event}
                  demo={session.gmail_demo}
                  onAdd={() => sendEvent("event_confirmed")}
                  onCancel={() => sendEvent("event_cancelled")}
                />
              )}
              {gmailCard ? <GmailCard {...gmailCardProps} compact /> : null}
            </>
          }
        />
      )}

      {gmailPopup && (
        <GoogleStubPopup
          onAllow={(email, name) => { setGmailPopup(false); void sendEvent("gmail_connected", { email, name }); }}
          onCancel={() => { setGmailPopup(false); void sendEvent("gmail_closed"); }}
        />
      )}

      {knowsOpen && session && (
        <KnowsPanel
          s={session}
          voiceName={config.voice_choices?.find((v) => v.id === session.voice_id)?.name ?? null}
          onSaved={applyState}
          onConnectGmail={() => { setKnowsOpen(false); setGmailCard(true); }}
          onDisconnectGmail={() => sendEvent("gmail_disconnected")}
          onDeleteAll={async () => {
            if (!window.confirm("Delete everything Persona knows about you and start over? This can't be undone.")) return;
            setKnowsOpen(false);
            await reset();
          }}
          onClose={() => setKnowsOpen(false)}
        />
      )}

      {graduated && !doneDismissed && callView === "none" && session && (
        <DonePanel
          s={session}
          onSaved={applyState}
          onConnectGmail={() => { setDoneDismissed(true); setGmailCard(true); }}
          onDisconnectGmail={() => sendEvent("gmail_disconnected")}
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
  mode,
  demo,
  waiting,
  compact,
  onConnect,
  onCancel,
  onDemo,
  onDismiss,
}: {
  mode: "google" | "stub";
  demo: boolean;
  waiting: boolean;
  compact?: boolean;
  onConnect: () => void;
  onCancel: () => void;
  onDemo: () => void;
  onDismiss: () => void;
}) {
  return (
    <div className={`card gmail ${compact ? "compact" : ""}`}>
      <div>
        <strong>Connect Gmail</strong>
        <p className="muted small">
          Read-only: upcoming calendar events and email subject lines, never the emails themselves. Nothing gets
          sent or changed, and you can disconnect anytime.
        </p>
        {mode === "google" && !waiting && (
          <p className="muted small">
            This is a test app, so Google will show a warning: tap <strong>Advanced</strong>, then{" "}
            <strong>Go to Persona</strong>.
          </p>
        )}
      </div>
      {waiting ? (
        <div className="row">
          <span className="muted small">Waiting for Google sign-in in the other window…</span>
          <button className="ghost" onClick={onCancel}>
            Cancel
          </button>
        </div>
      ) : (
        <div className="row">
          <button className="primary" onClick={onConnect}>
            Connect Gmail
          </button>
          <button className="ghost" onClick={onDismiss}>
            Not now
          </button>
        </div>
      )}
      {demo && mode === "google" && (
        <button className="link small" onClick={onDemo}>
          Can&apos;t sign in? Use demo data instead
        </button>
      )}
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
  botLevel,
  spokenCaption,
  micLevel,
  micName,
  micProblem,
  onTypeInstead,
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
  botLevel: number;
  spokenCaption: string;
  micLevel: number;
  micName: string | null;
  micProblem: string | null;
  onTypeInstead: () => void;
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
  // On a real call, show words as they're spoken (TTS word timestamps); otherwise the streamed text.
  const caption = (voice && spokenCaption) || live || lastAgentVoice || "";
  const mmss = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;

  return (
    <div className="call" role="dialog" aria-label={`Call with ${agentName}`}>
      <div className="call-inner">
        <VoiceOrb
          mode={view === "ringing" ? "ringing" : voice && userSpeaking ? "listening" : (voice ? botSpeaking : !!live) ? "speaking" : "idle"}
          level={voice ? (userSpeaking ? micLevel : botLevel) : live ? 0.35 : 0}
          initial={agentName.slice(0, 1).toUpperCase()}
        />
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
              <>
                <div className="mic" aria-label="Microphone level">
                  <div className="mic-bar">
                    <div className="mic-fill" style={{ width: `${Math.min(100, Math.round(micLevel * 250))}%` }} />
                  </div>
                  <span className="small muted">{micName ?? "microphone"}</span>
                </div>
                {micProblem && (
                  <div className="banner error mic-help" role="alert">
                    <strong>{micProblem}</strong>
                    <ol>
                      <li>Click the icon left of the address bar → Microphone → Allow, then reload.</li>
                      <li>Mac: System Settings → Privacy &amp; Security → Microphone → turn on Chrome.</li>
                      <li>Wrong mic? chrome://settings/content/microphone → pick the right one.</li>
                    </ol>
                    <button className="secondary" onClick={onTypeInstead}>
                      Continue by typing
                    </button>
                  </div>
                )}
                <button className="secondary wide" onClick={onMute} aria-pressed={muted}>
                  {muted ? "Unmute" : "Mute"}
                </button>
              </>
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

/** #14: everything Persona knows and has done, editable and deletable, in one place. */
function KnowsPanel({
  s,
  voiceName,
  onDeleteAll,
  onClose,
  ...recap
}: RecapProps & { voiceName: string | null; onDeleteAll: () => void; onClose: () => void }) {
  const w = s.week_summary;
  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="What Persona knows about you">
      <div className="modal done knows">
        <h2>What {s.agent_name ?? "Persona"} knows about you</h2>
        <p className="small muted">Everything it has learned or done. Edit anything, or delete it all.</p>

        <h3>What you told it</h3>
        <RecapRows s={s} {...recap} />
        {voiceName && <p className="small muted">Voice: {voiceName}</p>}

        <h3>What it can see</h3>
        {s.gmail_status === "connected" ? (
          <p className="small">
            {s.gmail_demo ? "The demo account" : s.gmail}: read once when you connected
            {w ? `: ${w.events_this_week} upcoming events and ${w.emails_scanned} email subject lines` : ""}. Never
            email bodies; medical, financial and private items are skipped.
          </p>
        ) : (
          <p className="small muted">Nothing. Gmail isn&apos;t connected.</p>
        )}

        <h3>What it did for you</h3>
        {s.added_events.length || s.saved_drafts.length ? (
          <ul className="small knows-list">
            {s.added_events.map((e, n) => (
              <li key={`e${n}`}>{e.op === "move" ? "Moved" : "Added"} {e.title} ({e.when})</li>
            ))}
            {s.saved_drafts.map((d, n) => (
              <li key={`d${n}`}>Saved a draft reply to {d.to_name} (not sent)</li>
            ))}
          </ul>
        ) : (
          <p className="small muted">Nothing yet. It only acts when you tap to approve.</p>
        )}

        <h3>Your data</h3>
        <p className="small muted">
          Kept on Persona&apos;s server and deleted after 7 idle days (Google access is revoked first).
        </p>
        <div className="row">
          <a className="button ghost small" href={`${API_URL}/api/sessions/${s.session_id}/export`}>
            Download my data
          </a>
          <button className="ghost small danger" onClick={onDeleteAll}>
            Delete everything
          </button>
        </div>
        <div className="row end">
          <button className="primary" onClick={onClose}>
            Done
          </button>
        </div>
      </div>
    </div>
  );
}

/** #9: tomorrow as Persona sees it, plus the one thing to watch. */
function TomorrowCard({ t }: { t: Tomorrow }) {
  return (
    <div className="card tomorrow">
      <div className="insights-head">
        <strong>{t.label}</strong>
        {t.demo && <span className="small muted">demo</span>}
      </div>
      {t.watch && (
        <p className="tomorrow-watch">
          <span className="tag-pill conflict">Watch</span> {t.watch}
        </p>
      )}
      {t.items.length ? (
        <ul className="tomorrow-list">
          {t.items.map((it, n) => (
            <li key={n}>
              <span className="muted">{it.time}</span>
              <span>{it.title}</span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="small muted">Nothing on the calendar. A clear day.</p>
      )}
      {t.free && <p className="small muted" style={{ margin: 0 }}>Longest free stretch: {t.free}</p>}
    </div>
  );
}

/** #4: hear your Persona say its new name in each voice, and pick one. */
function VoicePicker({
  agentName,
  choices,
  onPick,
  onDone,
}: {
  agentName: string;
  choices: VoiceChoice[];
  onPick: (v: VoiceChoice) => void;
  onDone: () => void;
}) {
  const [playing, setPlaying] = useState<string | null>(null);
  const [picked, setPicked] = useState<string | null>(null);
  const audio = useRef<HTMLAudioElement | null>(null);

  const tryVoice = (v: VoiceChoice) => {
    audio.current?.pause();
    const a = new Audio(voiceSampleUrl(v.id, agentName));
    audio.current = a;
    setPlaying(v.id);
    a.onended = () => setPlaying((p) => (p === v.id ? null : p));
    void a.play().catch(() => setPlaying(null));
    setPicked(v.id);
    onPick(v);
  };

  return (
    <div className="card voice-picker">
      <div>
        <strong>Pick {agentName}&apos;s voice</strong>
        <p className="small muted" style={{ margin: "2px 0 0" }}>Tap to hear it. You can change it anytime.</p>
      </div>
      <div className="voice-options">
        {choices.map((v) => (
          <button
            key={v.id}
            className={`voice-option ${picked === v.id ? "picked" : ""} ${playing === v.id ? "playing" : ""}`}
            onClick={() => tryVoice(v)}
            aria-pressed={picked === v.id}
          >
            <span className="voice-wave" aria-hidden>
              <i /><i /><i />
            </span>
            <span>
              <strong>{v.name}</strong>
              {v.vibe && <span className="small muted"> · {v.vibe}</span>}
            </span>
          </button>
        ))}
      </div>
      <div className="row end">
        <button className={picked ? "primary small" : "link small"} onClick={onDone}>
          {picked ? "Sounds good" : "Skip"}
        </button>
      </div>
    </div>
  );
}

/** Apple-style orb that breathes with whoever is talking. */
function VoiceOrb({ mode, level, initial }: { mode: "ringing" | "listening" | "speaking" | "idle"; level: number; initial: string }) {
  const scale = 1 + Math.min(1, level * 3) * 0.22;
  return (
    <div className={`orb ${mode}`} aria-hidden>
      <div className="orb-glow" style={{ transform: `scale(${1 + Math.min(1, level * 3) * 0.45})` }} />
      <div className="orb-core" style={{ transform: `scale(${scale})` }}>
        <span>{initial}</span>
      </div>
    </div>
  );
}

const VIBE: Record<string, string> = {
  rushed: "In a hurry",
  frustrated: "A bit frustrated",
  enthusiastic: "Excited",
  chatty: "Chatty",
};

/** The living profile: it fills in as the agent gets to know you. */
function profileRows(s: SessionState): { key: string; label: string; value: string; accent?: boolean }[] {
  const rows: { key: string; label: string; value: string; accent?: boolean }[] = [];
  if (s.agent_name) rows.push({ key: "agent", label: "Your Persona", value: s.agent_name });
  if (s.user_name) rows.push({ key: "you", label: "You", value: s.user_name });
  if (s.help_topic) rows.push({ key: "need", label: "Helping with", value: s.help_topic });
  if (VIBE[s.sentiment]) rows.push({ key: "vibe", label: "Right now", value: VIBE[s.sentiment] });
  const w = s.week_summary;
  if (w && s.gmail_status === "connected") {
    rows.push({
      key: "week",
      label: w.demo ? "Your week (demo)" : "Your week",
      value: `${w.events_this_week} events${w.busiest_day ? ` · busiest ${w.busiest_day.toLowerCase()} (${w.busiest_count})` : ""}`,
    });
  }
  if (s.insights?.length) {
    rows.push({ key: "noticed", label: "Noticed", value: s.insights[0].headline, accent: true });
  }
  if (s.added_events?.length) {
    rows.push({ key: "added", label: "Added for you", value: s.added_events.map((e) => e.title).join(", ") });
  }
  return rows;
}

function ProfilePanel({ s }: { s: SessionState }) {
  const rows = profileRows(s);
  return (
    <aside className="profile" aria-label="What Persona knows so far">
      <div className="profile-head">
        <span className="profile-dot" />
        <strong>{s.agent_name ? `${s.agent_name} is getting to know you` : "Getting to know you"}</strong>
      </div>
      {rows.length === 0 && <p className="small muted">This fills in as we talk.</p>}
      <dl>
        {rows.map((r) => (
          <div key={`${r.key}:${r.value}`} className={`profile-row ${r.accent ? "accent" : ""}`}>
            <dt>{r.label}</dt>
            <dd>{r.value}</dd>
          </div>
        ))}
      </dl>
    </aside>
  );
}

/** Small screens: the same profile as chips across the top. */
function ProfileStrip({ s }: { s: SessionState }) {
  const rows = profileRows(s).filter((r) => r.key !== "noticed");
  if (!rows.length) return null;
  return (
    <div className="profile-strip" aria-hidden>
      {rows.map((r) => (
        <span key={`${r.key}:${r.value}`} className="profile-chip">
          <span className="muted">{r.label}</span> {r.value}
        </span>
      ))}
    </div>
  );
}

const INSIGHT_LABEL: Record<Insight["kind"], string> = {
  deep: "Insight",
  conflict: "Conflict",
  tight: "No breathing room",
  packed: "Busy day",
  deadline: "Deadline",
  prep: "Prep",
  reply: "Waiting on you",
};

/** Persona noticed: problems found in their calendar and inbox, with one-tap fixes. */
function InsightsCard({
  insights,
  demo,
  compact,
  onFix,
  onAsk,
  onDismiss,
}: {
  insights: Insight[];
  demo: boolean;
  compact?: boolean;
  onFix: (i: Insight) => void;
  onAsk: (i: Insight) => void;
  onDismiss?: () => void;
}) {
  const shown = insights.slice(0, compact ? 2 : 3);
  return (
    <div className={`card insights ${compact ? "compact" : ""}`}>
      <div className="insights-head">
        <strong>Persona noticed</strong>
        <span className="small muted">{demo ? "in the demo account" : "in your calendar and inbox"}</span>
      </div>
      <ul>
        {shown.map((i) => (
          <li key={i.id}>
            <span className={`tag-pill ${i.kind}`}>{INSIGHT_LABEL[i.kind] ?? i.kind}</span>
            <p className="insight-headline">{i.headline}</p>
            {!compact && <p className="small muted insight-detail">{i.detail}</p>}
            <div className="row">
              {i.action && (
                <FixButton insight={i} onFix={onFix} />
              )}
              <button className="ghost small" onClick={() => onAsk(i)}>
                Tell me more
              </button>
            </div>
          </li>
        ))}
      </ul>
      {onDismiss && (
        <div className="row end">
          <button className="link small" onClick={onDismiss}>
            Hide
          </button>
        </div>
      )}
    </div>
  );
}

function FixButton({ insight, onFix }: { insight: Insight; onFix: (i: Insight) => void }) {
  const [busy, setBusy] = useState(false);
  const type = insight.action?.type;
  const label =
    type === "draft_reply" ? "Draft a reply" : type === "move_event" ? "Move it" : insight.kind === "packed" ? "Block it" : "Add to calendar";
  return (
    <button
      className="primary small"
      disabled={busy}
      onClick={() => {
        setBusy(true);
        onFix(insight);
        setTimeout(() => setBusy(false), type === "draft_reply" ? 4000 : 800);
      }}
    >
      {busy && type === "draft_reply" ? "Writing…" : label}
    </button>
  );
}

/** #8: an editable reply. Saved to Gmail drafts only on the tap; Persona never sends. */
function DraftCard({
  draft,
  demo,
  onSave,
  onCancel,
}: {
  draft: { to_name: string; subject: string; body: string };
  demo: boolean;
  onSave: (body: string) => void;
  onCancel: () => void;
}) {
  const [body, setBody] = useState(draft.body);
  return (
    <div className="card draft-card">
      <div>
        <p className="small muted" style={{ margin: 0 }}>
          Reply to {draft.to_name}
        </p>
        <strong>{draft.subject}</strong>
      </div>
      <textarea value={body} onChange={(e) => setBody(e.target.value)} rows={4} aria-label="Draft reply" />
      <div className="row">
        <button className="primary" onClick={() => onSave(body)}>
          Save to {demo ? "demo " : "Gmail "}drafts
        </button>
        <button className="ghost" onClick={onCancel}>
          Not now
        </button>
      </div>
      <p className="small muted" style={{ margin: 0 }}>It&apos;s saved as a draft for you to send. Persona never sends email.</p>
    </div>
  );
}

/** "Nothing happens without your yes": the agent proposes, only this tap adds it. */
function EventConfirmCard({
  event,
  demo,
  onAdd,
  onCancel,
}: {
  event: CalendarEvent;
  demo: boolean;
  onAdd: () => void;
  onCancel: () => void;
}) {
  return (
    <div className="card event-card">
      <div>
        <p className="small muted" style={{ margin: 0 }}>
          {event.op === "move" ? "Move this" : "Add to your"} {demo ? "demo " : ""}
          {event.op === "move" ? "event?" : "calendar?"}
        </p>
        <strong>{event.title}</strong>
        <p className="small" style={{ margin: "2px 0 0" }}>
          {event.when}
          {event.location ? ` · ${event.location}` : ""}
        </p>
      </div>
      <div className="row">
        <button className="primary" onClick={onAdd}>
          {event.op === "move" ? "Move" : "Add"}
        </button>
        <button className="ghost" onClick={onCancel}>
          Not now
        </button>
      </div>
    </div>
  );
}

/** Show the recap once a call has ended (it's the "post-call text"), and at graduation. */
function showRecap(s: SessionState): boolean {
  const talkedOnCall = s.transcript.some((t) => t.role === "user" && t.channel === "voice");
  return s.graduated || (talkedOnCall && ["hung_up", "completed", "missed"].includes(s.call_status));
}

type RecapProps = {
  s: SessionState;
  onSaved: (s: SessionState) => void;
  onConnectGmail: () => void;
  onDisconnectGmail: () => void;
};

function RecapRows({ s, onSaved, onConnectGmail, onDisconnectGmail }: RecapProps) {
  const firstUp =
    s.starter_suggestions[0] ?? (s.help_topic ? `Start on: ${s.help_topic}` : null);
  return (
    <dl className="recap">
      <EditableRow s={s} field="agent_name" label="My name" value={s.agent_name}
        hint={s.agent_name_defaulted ? "default; rename me anytime" : undefined} onSaved={onSaved} />
      <EditableRow s={s} field="user_name" label="Your name" value={s.user_name}
        missing="Not yet. I'll ask." onSaved={onSaved} />
      <EditableRow s={s} field="help_topic" label="Helping with" value={s.help_topic}
        missing="Not yet. Tell me whenever." onSaved={onSaved} />
      <dt>Gmail</dt>
      <dd>
        {s.gmail_status === "connected" ? (
          <>
            <span>{s.gmail_demo ? "Demo data" : s.gmail}</span>
            <button className="link small" onClick={onDisconnectGmail}>Disconnect</button>
          </>
        ) : (
          <>
            <span className="muted">Not connected</span>
            <button className="link small" onClick={onConnectGmail}>Connect</button>
          </>
        )}
      </dd>
      {firstUp && (
        <>
          <dt>First up</dt>
          <dd>{firstUp}</dd>
        </>
      )}
    </dl>
  );
}

function EditableRow({
  s,
  field,
  label,
  value,
  hint,
  missing,
  onSaved,
}: {
  s: SessionState;
  field: EditableField;
  label: string;
  value: string | null;
  hint?: string;
  missing?: string;
  onSaved: (s: SessionState) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value ?? "");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      onSaved(await editField(s.session_id, field, draft));
      setEditing(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't save that.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <dt>{label}</dt>
      <dd>
        {editing ? (
          <form className="edit-row" onSubmit={(e) => { e.preventDefault(); void save(); }}>
            <input value={draft} onChange={(e) => setDraft(e.target.value)} aria-label={label} autoFocus />
            <button className="primary small" type="submit" disabled={saving || !draft.trim()}>Save</button>
            <button className="link small" type="button" onClick={() => { setEditing(false); setError(null); }}>
              Cancel
            </button>
            {error && <span className="small error-text">{error}</span>}
          </form>
        ) : (
          <>
            {value ? <span>{value}</span> : <span className="muted">{missing}</span>}
            {hint && <span className="muted small"> ({hint})</span>}
            <button className="link small" onClick={() => { setDraft(value ?? ""); setEditing(true); }}>
              {value ? "Edit" : "Add"}
            </button>
          </>
        )}
      </dd>
    </>
  );
}

function RecapCard(props: RecapProps & { onDismiss: () => void }) {
  return (
    <div className="card">
      <strong>Here&apos;s what I&apos;ve got</strong>
      <p className="muted small">Anything off? Tap to fix it.</p>
      <RecapRows {...props} />
      <div className="row end">
        <button className="ghost small" onClick={props.onDismiss}>Looks right</button>
      </div>
    </div>
  );
}

function DonePanel({
  onClose,
  onStart,
  ...props
}: RecapProps & { onClose: () => void; onStart: (text: string) => void }) {
  const s = props.s;
  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="You're in">
      <div className="modal done">
        <h2>You&apos;re in.</h2>
        <p className="muted">{s.agent_name} is ready. Here&apos;s what it knows so far; tap anything to fix it.</p>
        <RecapRows {...props} />
        {s.tomorrow && s.gmail_status === "connected" && <TomorrowCard t={s.tomorrow} />}
        {s.starter_suggestions.length > 0 && (
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

function DebugPanel({ s, latency, usage }: { s: SessionState; latency: TurnLatency | null; usage: TurnUsage | null }) {
  const cached = usage?.cache_read_input_tokens ?? 0;
  const fresh = (usage?.input_tokens ?? 0) + (usage?.cache_creation_input_tokens ?? 0);
  const rows: [string, string][] = [
    ["agent_name", s.agent_name ?? "–"],
    ["user_name", s.user_name ?? "–"],
    ["help_topic", s.help_topic ?? "–"],
    ["gmail", `${s.gmail ?? "–"} (${s.gmail_status})`],
    ["channel", s.channel],
    ["call_status", s.call_status],
    ["sentiment", s.sentiment],
    ["short_answer_streak", String(s.short_answer_streak)],
    ["turns_since_progress", String(s.turns_since_progress)],
    ["graduation_offered", String(s.graduation_offered)],
    ["graduated", String(s.graduated)],
    ["corrected", s.corrected.join(", ") || "–"],
    ["last turn", latency ? `first token ${latency.ttft_ms ?? "–"}ms · total ${latency.total_ms}ms` : "–"],
    ["prompt cache", usage ? `${cached.toLocaleString()} cached / ${fresh.toLocaleString()} new tokens` : "–"],
  ];
  return (
    <aside className="debug" aria-label="Session state">
      <h3>
        Under the hood{" "}
        <a className="small" href="/metrics" target="_blank" rel="noreferrer" style={{ fontWeight: 400 }}>
          metrics ↗
        </a>
      </h3>
      <dl>
        {rows.map(([k, v]) => (
          <div key={k}>
            <dt>{k}</dt>
            <dd>{v}</dd>
          </div>
        ))}
      </dl>
      {s.last_director_note && (
        <>
          <h3 className="note-title">Director&apos;s note (what the code told the model)</h3>
          <pre className="note">{s.last_director_note.replace(/<\/?director_note>\n?/g, "")}</pre>
        </>
      )}
    </aside>
  );
}
