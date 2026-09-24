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
};

export type VoiceCall = {
  hangUp: () => Promise<void>;
  setMuted: (muted: boolean) => void;
};

/** Connect the browser mic/speaker to the voice agent over WebRTC. */
export async function startVoiceCall(sessionId: string, h: VoiceHandlers): Promise<VoiceCall> {
  const audio = new Audio();
  audio.autoplay = true;
  let closing = false;

  const client = new PipecatClient({
    transport: new SmallWebRTCTransport(),
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
        if (track.kind === "audio" && participant && !participant.local) {
          audio.srcObject = new MediaStream([track]);
          void audio.play().catch(() => {});
        }
      },
      onDisconnected: () => {
        if (!closing) h.onDropped();
      },
    },
  });

  await client.connect({
    webrtcRequestParams: { endpoint: `${API_URL}/api/offer`, requestData: { session_id: sessionId } },
  });

  return {
    hangUp: async () => {
      closing = true;
      audio.srcObject = null;
      await client.disconnect();
    },
    setMuted: (muted) => client.enableMic(!muted),
  };
}
