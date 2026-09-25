export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export type Channel = "text" | "voice";
export type CallStatus =
  | "not_started"
  | "offered"
  | "ringing"
  | "in_progress"
  | "hung_up"
  | "declined"
  | "missed"
  | "completed";

export type Turn = { role: "user" | "agent" | "event"; text: string; channel: Channel; ts: number };

export type SessionState = {
  session_id: string;
  agent_name: string | null;
  user_name: string | null;
  gmail: string | null;
  help_topic: string | null;
  agent_name_defaulted: boolean;
  voice_id: string | null;
  phone: string | null;
  gmail_status: "not_connected" | "popup_open" | "connected" | "denied" | "error";
  gmail_card_shown: boolean;
  gmail_demo: boolean;
  google_result: string | null;
  channel: Channel;
  call_status: CallStatus;
  sentiment: string;
  short_answer_streak: number;
  turns_since_progress: number;
  user_turns: number;
  wrapping_up: boolean;
  starter_suggestions: string[];
  graduation_offered: boolean;
  graduated: boolean;
  corrected: string[];
  last_director_note: string | null;
  insights: Insight[];
  week_summary: WeekSummary | null;
  tomorrow: Tomorrow | null;
  pending_event: CalendarEvent | null;
  pending_draft: { to_name: string; subject: string; body: string } | null;
  saved_drafts: { to_name: string; subject: string; demo: boolean }[];
  added_events: CalendarEvent[];
  transcript: Turn[];
};

export type WeekSummary = {
  events_this_week: number;
  busiest_day: string | null;
  busiest_count: number;
  emails_scanned: number;
  demo: boolean;
};

export type Insight = {
  id: string;
  kind: "deep" | "conflict" | "tight" | "packed" | "deadline" | "prep" | "reply";
  headline: string;
  detail: string;
  action?: { type: "add_event" | "move_event" | "draft_reply"; suggested_time?: string | null } | null;
};

export type CalendarEvent = {
  title: string;
  when: string;
  location?: string | null;
  all_day?: boolean;
  op?: "add" | "move";
};

export type Tomorrow = {
  label: string;
  items: { time: string; title: string }[];
  watch: string | null;
  free: string | null;
  demo: boolean;
};

export type UiEvent =
  | { type: "name_suggestions"; names: string[] }
  | { type: "show_call_offer" }
  | { type: "hide_call_offer" }
  | { type: "start_call" }
  | { type: "ringing" }
  | { type: "end_call"; reason: string }
  | { type: "show_callback" }
  | { type: "show_gmail_card" }
  | { type: "hide_gmail_card" }
  | { type: "wrap_up" }
  | { type: "suggestions"; items: string[] }
  | { type: "graduated" }
  | { type: "confirm_event"; event: CalendarEvent }
  | { type: "event_added"; event: CalendarEvent }
  | { type: "slot"; slot: string; value: string };

export type TurnLatency = { ttft_ms: number | null; total_ms: number };
export type TurnUsage = {
  input_tokens?: number;
  output_tokens?: number;
  cache_read_input_tokens?: number;
  cache_creation_input_tokens?: number;
};

export type StreamEvent =
  | { type: "delta"; text: string }
  | { type: "ui"; ui: UiEvent }
  | { type: "done"; reply: string; latency: TurnLatency; usage?: TurnUsage }
  | { type: "state"; state: SessionState }
  | { type: "end" };

export type EventType =
  | "name_skipped"
  | "call_accepted"
  | "call_declined"
  | "call_connected"
  | "call_missed"
  | "hangup"
  | "callback"
  | "gmail_popup_opened"
  | "gmail_connected"
  | "gmail_closed"
  | "gmail_denied"
  | "gmail_disconnected"
  | "graduate"
  | "event_confirmed"
  | "event_cancelled"
  | "draft_confirmed"
  | "draft_cancelled"
  | "resumed";

export type Config = {
  gmail_stub: boolean;
  gmail_mode?: "google" | "stub";
  demo_data?: boolean;
  voice?: boolean;
  voice_problem?: string | null;
  ice_servers?: RTCIceServer[];
  voice_choices?: VoiceChoice[];
  phone?: boolean;
};

/** #13: Persona calls the user's real phone (Twilio). */
export async function callMyPhone(id: string, phone: string): Promise<SessionState> {
  const r = await fetch(`${API_URL}/api/sessions/${id}/phone-call`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ phone }),
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof body.detail === "string" ? body.detail : "Couldn't place the call.");
  return body.state as SessionState;
}

export type VoiceChoice = { id: string; name: string; vibe?: string };

export function voiceSampleUrl(voiceId: string, agentName: string): string {
  return `${API_URL}/api/voices/${voiceId}/sample?name=${encodeURIComponent(agentName)}`;
}

export async function pickVoice(id: string, voiceId: string): Promise<SessionState> {
  const r = await fetch(`${API_URL}/api/sessions/${id}/voice`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ voice_id: voiceId }),
  });
  if (!r.ok) throw new Error("Couldn't set that voice.");
  return (await r.json()).state as SessionState;
}

export async function createSession(): Promise<{ state: SessionState; ui: UiEvent[]; config: Config }> {
  let tz: string | undefined;
  try {
    tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
  } catch {}
  const r = await fetch(`${API_URL}/api/sessions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tz }),
  });
  if (!r.ok) throw new Error(`create session failed: ${r.status}`);
  return r.json();
}

export async function getSession(id: string): Promise<{ state: SessionState; config: Config } | null> {
  const r = await fetch(`${API_URL}/api/sessions/${id}`);
  if (r.status === 404) return null;
  if (!r.ok) throw new Error(`get session failed: ${r.status}`);
  return r.json();
}

export type EditableField = "agent_name" | "user_name" | "help_topic";

/** Fix a field from the recap. Resolves to the new state, or throws with a friendly message. */
export async function editField(id: string, field: EditableField, value: string): Promise<SessionState> {
  const r = await fetch(`${API_URL}/api/sessions/${id}/fields`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ field, value }),
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof body.detail === "string" ? body.detail : "Couldn't save that.");
  return body.state as SessionState;
}

/** One tap on a finding's fix: returns state with a confirm card (nothing is added yet). */
export async function addInsightFix(id: string, insightId: string): Promise<SessionState> {
  const r = await fetch(`${API_URL}/api/sessions/${id}/insights/${insightId}/add`, { method: "POST" });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof body.detail === "string" ? body.detail : "Couldn't set that up.");
  return body.state as SessionState;
}

/** #8: Persona writes a reply for editing. Nothing is saved until the user taps Save. */
export async function draftReply(id: string, insightId: string): Promise<SessionState> {
  const r = await fetch(`${API_URL}/api/sessions/${id}/insights/${insightId}/draft`, { method: "POST" });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof body.detail === "string" ? body.detail : "Couldn't write a draft.");
  return body.state as SessionState;
}

export async function deleteSession(id: string): Promise<void> {
  await fetch(`${API_URL}/api/sessions/${id}`, { method: "DELETE" });
}

/** POST and read the server-sent event stream, calling onEvent for each event. */
export async function streamPost(path: string, body: unknown, onEvent: (e: StreamEvent) => void): Promise<void> {
  const r = await fetch(`${API_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok || !r.body) throw new Error(`request failed: ${r.status}`);
  const reader = r.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) !== -1) {
      const chunk = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      for (const line of chunk.split("\n")) {
        if (line.startsWith("data: ")) onEvent(JSON.parse(line.slice(6)) as StreamEvent);
      }
    }
  }
}
