import { PipecatClient } from "@pipecat-ai/client-js";
import { SmallWebRTCTransport } from "@pipecat-ai/small-webrtc-transport";
import { API_URL, type StreamEvent } from "./api";

export type VoiceHandlers = {
  /** Same event shapes as the text stream: delta / ui / done / state. */
  onEvent: (e: StreamEvent) => void;
  onUserTranscript: (text: string, final: boolean) => void;
  onBotSpeaking: (speaking: boolean) => void;
  onUserSpeaking: (speaking: boolean) => void;
  /** The connection closed without us asking (network drop, server ended). */
  onDropped: () => void;
  /** 0..1 loudness of the user's mic, for the meter. */
  onMicLevel: (level: number) => void;
  /** Which mic the browser is using. */
  onMicName: (name: string) => void;
  /** The mic isn't working: blocked, missing, or silent. */
  onMicProblem: (message: string) => void;
};

/** If the mic has produced nothing above this level for this long, something's wrong. */
const SILENT_MIC_LEVEL = 0.01;
const SILENT_MIC_MS = 6000;

export type VoiceCall = {
  hangUp: () => Promise<void>;
  setMuted: (muted: boolean) => void;
};

/** Connect the browser mic/speaker to the voice agent over WebRTC. */
export async function startVoiceCall(
  sessionId: string,
  h: VoiceHandlers,
  iceServers?: RTCIceServer[],
): Promise<VoiceCall> {
  const audio = new Audio();
  audio.autoplay = true;
  let closing = false;
  let heardSomething = false;

  const client = new PipecatClient({
    transport: new SmallWebRTCTransport(iceServers?.length ? { iceServers } : undefined),
    enableMic: true,
    enableCam: false,
    callbacks: {
      onServerMessage: (data) => h.onEvent(data as StreamEvent),
      onUserTranscript: (d) => h.onUserTranscript(d.text, d.final),
      onBotStartedSpeaking: () => h.onBotSpeaking(true),
      onBotStoppedSpeaking: () => h.onBotSpeaking(false),
      onUserStartedSpeaking: () => h.onUserSpeaking(true),
      onUserStoppedSpeaking: () => h.onUserSpeaking(false),
      onTrackStarted: (track, participant) => {
        // The agent's track arrives with no participant info; only our own mic is marked local.
        if (track.kind === "audio" && !participant?.local) {
          audio.srcObject = new MediaStream([track]);
          void audio.play().catch(() => {
            // Autoplay blocked: start sound on the next click anywhere.
            document.addEventListener("click", () => void audio.play().catch(() => {}), { once: true });
          });
        }
      },
      onDisconnected: () => {
        if (!closing) h.onDropped();
      },
      onLocalAudioLevel: (level) => {
        if (level > SILENT_MIC_LEVEL) heardSomething = true;
        h.onMicLevel(level);
      },
      onMicUpdated: (mic) => h.onMicName(mic.label || "default microphone"),
      onDeviceError: (err) => h.onMicProblem(micErrorMessage(`${err?.type ?? ""} ${err?.message ?? ""}`)),
    },
  });

  await client.connect({
    webrtcRequestParams: { endpoint: `${API_URL}/api/offer`, requestData: { session_id: sessionId } },
  });

  // connect() still succeeds without a mic (permission blocked, no device), so check.
  if (!client.tracks().local.audio) {
    h.onMicProblem(micErrorMessage("no-track"));
  } else {
    setTimeout(() => {
      if (!closing && !heardSomething) {
        h.onMicProblem(
          "Your microphone isn't picking up any sound. It may be muted, or Chrome may be using the wrong mic.",
        );
      }
    }, SILENT_MIC_MS);
  }

  return {
    hangUp: async () => {
      closing = true;
      audio.srcObject = null;
      await client.disconnect();
    },
    setMuted: (muted) => client.enableMic(!muted),
  };
}

function micErrorMessage(reason: string): string {
  if (/permission|denied|notallowed|blocked/i.test(reason)) {
    return "Chrome doesn't have permission to use your microphone.";
  }
  if (/notfound|no-?device|missing/i.test(reason)) return "No microphone was found.";
  if (/in-?use|notreadable/i.test(reason)) return "Another app is using your microphone.";
  return "Your microphone didn't start.";
}
