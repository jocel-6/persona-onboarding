# Deploying so reviewers can just open a link

**Shape:** frontend on Vercel (free), backend as a Docker service on Render (or any container host), plus a TURN relay so voice calls carry audio through the host's network. About 30–45 minutes the first time.

Cost: Render "Starter" (always on, ~$7/month; the free tier sleeps and drops calls), Vercel free, TURN free tier. Model/speech usage is the same as local.

## Why a TURN server?

Voice calls use WebRTC, which sends audio directly between the browser and the backend. On a laptop that just works. Cloud hosts like Render and Railway only forward web traffic (HTTP on one port), so the direct audio path is blocked: the call would "connect" and then carry no sound. A TURN server relays the audio over allowed ports. Use one with **long-lived credentials**, since the app reads one fixed list from `ICE_SERVERS`:

- **Metered.ca** (free tier, recommended): sign up → create a credential (no expiry) → "Show ICE servers array".
- Cloudflare Realtime TURN issues short-lived credentials from an API, so a fixed list would stop working after its TTL. It would need a small change to fetch credentials per call.

Set it on the backend as `ICE_SERVERS`: the array as JSON on one line (double quotes, no trailing comment):

```json
[{"urls":"stun:stun.relay.metered.ca:80"},
 {"urls":"turn:global.relay.metered.ca:80","username":"<from Metered>","credential":"<from Metered>"},
 {"urls":"turn:global.relay.metered.ca:443","username":"<from Metered>","credential":"<from Metered>"},
 {"urls":"turns:global.relay.metered.ca:443?transport=tcp","username":"<from Metered>","credential":"<from Metered>"}]
```

The backend passes the same list to the browser, so both ends use the relay. (A VM with open UDP ports, e.g. a small EC2/Hetzner box running the Docker image with `--network host`, works without TURN.)

## 1. Push the repo to GitHub

Private is fine; Render and Vercel both connect to private repos. `.env` is git-ignored, so no keys are pushed.

## 2. Backend on Render

1. <https://dashboard.render.com> → **New → Blueprint** → connect the repo. Render reads `render.yaml`: a Docker web service rooted at `backend/`, a 1 GB disk at `/data` for sessions, health check `/api/health`.
2. Fill in the secrets it asks for (same values as your local `.env`, plus):
   - `GOOGLE_REDIRECT_URI` = `https://<service>.onrender.com/api/google/callback`
   - `FRONTEND_ORIGIN` = your Vercel URL from step 3 (come back and set it; comma-separate to also allow `http://localhost:3000`)
   - `ICE_SERVERS` = the TURN JSON above
3. Deploy. Check `https://<service>.onrender.com/api/health` shows `"voice": true`.

## 3. Frontend on Vercel

1. <https://vercel.com/new> → import the repo → **Root Directory: `frontend`** (framework auto-detects as Next.js).
2. Environment variable: `NEXT_PUBLIC_API_URL` = `https://<service>.onrender.com`
3. Deploy, then put the Vercel URL into the backend's `FRONTEND_ORIGIN` and redeploy the backend.

## 4. Google sign-in for the deployed site

Google Cloud Console → your OAuth client:
- **Authorized JavaScript origins:** add `https://<your-app>.vercel.app`
- **Authorized redirect URIs:** add `https://<service>.onrender.com/api/google/callback`

Keep the app in **Testing** and add the reviewers' Google accounts as test users. Anyone else can use "Can't sign in? Use demo data".

## 5. Verify like a stranger would

Open the Vercel URL in a private window:
1. Name the agent, take the call, talk, interrupt it, stay silent, hang up: the text follow-up and recap appear.
2. Connect Gmail (test user) or demo data: the agent mentions one real item.
3. Refresh mid-conversation: "welcome back".

If the call connects but there's no audio either way, `ICE_SERVERS` is missing or wrong (most common deploy issue).

## Before sharing

- **Rotate every API key** that was ever pasted into a chat or a doc (Anthropic, Deepgram, Cartesia, Google client secret), and put the new ones in Render only.
- Set spending limits in the Anthropic, Deepgram and Cartesia dashboards; reviewers may run 20–50 conversations.
- Keep the services running until you hear back.
