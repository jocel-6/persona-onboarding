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
  gmail_status: "not_connected" | "popup_open" | "connected" | "denied" | "error";
  gmail_card_shown: boolean;
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
  transcript: Turn[];
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
  | { type: "slot"; slot: string; value: string };

export type StreamEvent =
  | { type: "delta"; text: string }
  | { type: "ui"; ui: UiEvent }
  | { type: "done"; reply: string; latency: { ttft_ms: number | null; total_ms: number } }
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
  | "graduate"
  | "resumed";

export type Config = { gmail_stub: boolean };

export async function createSession(): Promise<{ state: SessionState; ui: UiEvent[]; config: Config }> {
  const r = await fetch(`${API_URL}/api/sessions`, { method: "POST" });
  if (!r.ok) throw new Error(`create session failed: ${r.status}`);
  return r.json();
}

export async function getSession(id: string): Promise<{ state: SessionState; config: Config } | null> {
  const r = await fetch(`${API_URL}/api/sessions/${id}`);
  if (r.status === 404) return null;
  if (!r.ok) throw new Error(`get session failed: ${r.status}`);
  return r.json();
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
