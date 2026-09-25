# Connecting real Gmail: Google Cloud setup

About 20 minutes, once. Until this is done, the app uses a clearly labeled stand-in for Google sign-in, and reviewers can pick "Use demo data" instead.

Google renames its console menus now and then. If a label below doesn't match, the same pages may appear under **Google Auth Platform** (Branding, Audience, Data Access, Clients) instead of **OAuth consent screen** and **Credentials**.

## 1. Create a project
1. Go to <https://console.cloud.google.com/> and sign in.
2. Click the project picker (top left, next to "Google Cloud"), then **New project**.
3. Name it `Persona Onboarding`, then **Create**. Make sure it's selected in the project picker afterward.

## 2. Turn on the two APIs
1. Go to **APIs & Services → Library**.
2. Search **Gmail API**, open it, click **Enable**.
3. Go back to the Library, search **Google Calendar API**, open it, click **Enable**.

## 3. Set up the consent screen (Testing mode)
1. Go to **APIs & Services → OAuth consent screen** (or **Google Auth Platform → Branding**).
2. User type: **External**. Click **Create** / **Get started**.
3. App name: `Persona (test)`. User support email and developer contact: your email. Save.
4. **Scopes / Data Access → Add or remove scopes.** Add exactly these (paste into the filter box):
   - `openid`, `.../auth/userinfo.email`, `.../auth/userinfo.profile`
   - `https://www.googleapis.com/auth/calendar.events` (read events, and add ones the user confirms on screen)
   - `https://www.googleapis.com/auth/gmail.metadata`

   `gmail.metadata` reads headers only (subject, sender, date), never message bodies, which is why Persona uses it. Google lists it as a "restricted" scope; that's fine in Testing mode.
5. **Test users / Audience → Add users:** add your Google account and every tester's account. Only these accounts can sign in while the app is in Testing mode (up to 100). **Ask the recruiter which Google accounts the testers will use.**
6. Leave the app in **Testing**. Don't publish it; publishing a Gmail app requires Google's verification, which takes weeks.

## 4. Create the OAuth client
1. Go to **APIs & Services → Credentials** (or **Google Auth Platform → Clients**).
2. **Create credentials → OAuth client ID**, application type **Web application**, name `Persona local`.
3. **Authorized redirect URIs → Add URI:** `http://localhost:8000/api/google/callback`
   (Add your deployed backend's `https://…/api/google/callback` here too when you deploy.)
4. **Create**, then copy the **Client ID** and **Client secret**.

## 5. Add them to the app
In `~/Documents/Persona/.env`:

```
GOOGLE_CLIENT_ID=1234567890-abc...apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=GOCSPX-...
```

Restart `./dev.sh`. The app switches from the stand-in to real Google sign-in automatically (`GMAIL_STUB=1` forces the stand-in back on).

## 6. Try it
1. Start a session, get to the Connect Gmail card, click **Connect Gmail**.
2. Google warns **"Google hasn't verified this app."** That's expected for a test app: click **Advanced → Go to Persona (test)**.
3. Leave all the boxes ticked and click **Continue**. (If you untick calendar or email, Persona treats it as "not connected" and says so.)
4. The agent should confirm, then mention something real from your upcoming calendar or inbox subjects.

## What happens with the data
- Persona reads the next ~10 calendar events and ~20 recent inbox subject lines, once, right after you connect. Anything that looks medical, financial, or otherwise private is filtered out before the model sees it.
- Tokens are stored only on the server (a separate table, never in the browser or sent to the model), and are revoked and deleted when you click **Disconnect** or the session is reset.
- The only thing Persona ever writes is a calendar event you approve by tapping **Add** on its confirm card. Nothing is sent, edited, or deleted. Email contents are never read or logged.

## Troubleshooting
| You see | Why | Fix |
|---|---|---|
| "Access blocked: Persona has not completed the Google verification process" | The account isn't a test user | Add it under Test users (step 3.5), or use demo data |
| "Error 400: redirect_uri_mismatch" | The redirect URI doesn't match exactly | Check step 4.3: `http://localhost:8000/api/google/callback`, no trailing slash |
| The popup doesn't open | Browser blocked pop-ups | Allow pop-ups for localhost:3000 |
| Connected, but the agent never mentions anything | Empty calendar and inbox, or everything was filtered as private | Expected: it falls back to what you told it |
