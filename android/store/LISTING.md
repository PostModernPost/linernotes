# Liner Notes on Google Play

Everything to paste into Play Console. Images are in this folder.

## Main store listing

**App name:** Liner Notes

**Short description (80 max):**
Album art, lyrics and credits for the record on your turntable, on your phone.

**Full description:**
Liner Notes puts the liner notes for the record you are playing on your phone, so you can read them from your desk or the couch without getting up for the sleeve.

Put a record on. Liner Notes hears it, names the song, and shows the cover, the lyrics and who played on it. It follows the record song by song, side by side, and tells you when to flip.

It works on the phone on its own, listening with the phone's microphone. If you also run Liner Notes on a Windows PC (free at liner-notes.app), the phone can connect to it over your home Wi-Fi or a USB cable and show what the PC hears, including Spotify and other apps playing on the PC.

- The cover, song, side and track, big enough to read from the couch
- Timed lyrics that scroll along with the song
- The band's story, full credits from Discogs, and similar bands
- Your shelf: every record you play, synced with your Discogs collection
- A gentle warning when a side is about to end
- The screen wakes for the next record and goes dark when nothing is playing
- Share the song you're hearing with a link that opens in any music app

Free, no ads, no account. Your listening stays on your own phone or PC.

**App icon:** icon-512.png
**Feature graphic:** feature-graphic.png
**Phone screenshots:** play-1.png to play-7.png, in that order (1080 × 1920). Every album, band and cover in them is
made up, so they show no one else's artwork. To remake them after the app changes: in `mockup/`, run
`python mock_engine.py`, then the covers, screens and slides with `shot.py` (see the files there).

**Category:** Music & Audio
**Contact email:** linernotes987@gmail.com
**Website:** https://liner-notes.app
**Privacy policy:** https://liner-notes.app/privacy

## App content answers

- **Ads:** No ads.
- **App access:** "All functionality is available without special access." The app works on its own: open it, allow the microphone, and play music near the phone. (Connecting to a PC is optional.) If Google asks for instructions, paste: "Open the app and tap Use this phone on its own. Allow the microphone and play a song near the phone: it is recognized within about 30 seconds. Connecting to the free Liner Notes desktop app (https://liner-notes.app) is optional."
- **Content rating:** Questionnaire: category "Utility, Productivity, Communication, or Other"; answer No to everything (no violence, no user content, no chat). Lyrics come from public lyric sites; answer the profanity question No unless they object.
- **Target audience:** 18 and over (nothing is aimed at children, and it keeps the children's-app rules out of the way).
- **News app:** No. **Government app:** No. **Financial features:** None. **Health:** None.

## Data safety

- **Does your app collect or share any of the required user data types?** Yes.
- **Is all of the user data collected by your app encrypted in transit?** No. (Clips to Shazam go over HTTPS, but audio sent to the user's own PC over the home network isn't encrypted.)
- **Do you provide a way for users to request that their data is deleted?** Yes: everything is stored on the user's own phone or PC. Uninstalling the app or clearing its data deletes it; disconnecting the phone in Settings removes its key.
- **Data types:**
  - **Audio > Voice or sound recordings:**
    - Collected: Yes. Shared: Yes (short clips go to Shazam to name the song; on a PC setup, to the user's own PC).
    - Processed ephemerally: Yes (only the last clip is kept, in memory).
    - Required or optional: Required for recognizing music on the phone (the user can still use the app connected to a PC without it).
    - Purpose: App functionality.
  - **App activity > Other actions** (the songs recognized, kept as listening history): Collected: No. It never leaves the device, so Play doesn't count it as collected.
  - Nothing else: no location, contacts, identifiers, analytics, ads or crash reporting.

## Permissions declarations

- **Foreground service, microphone:** "Liner Notes recognizes the record playing in the room. While music is playing it records with the phone's microphone and sends short clips to Shazam (or to the user's own Liner Notes PC app) to name the song. It runs while the screen is off so recognition continues through a whole record side. A notification is shown the whole time, and listening can be turned off in the app."
- **Foreground service, special use:** "Keeps the now-playing display in sync while the screen is off: it watches the Liner Notes engine (running in the app, or on the user's own PC on the local network) about once a second, and turns the screen on when the next song starts. A notification is shown the whole time." Google may ask for a short video: record the phone screen going dark, then waking when a song starts.

## Liner Notes Plus (the paid unlock)

The app is free. Liner Notes Plus unlocks timed lyrics, the song page (the band's story, credits, similar bands), the shelf, saved songs, history and stats, Spotify, Discogs and Last.fm, the side-end warning and waking the screen for the next song. Recognizing the record, the cover, song, side and track, the progress bar and connecting to a PC stay free. The GitHub copy has everything unlocked.

The IDs below must be typed exactly; the app looks for them.

1. **Payments profile** (once): Play Console > **Settings** > **Payments profile**: your bank account and US tax details. Selling isn't possible until it's set up.
2. Upload a build with billing (1.0.8 or later) to a testing track first: Play only lets you create products once it has seen one.
3. **Subscription:** Play Console > the app > **Monetize with Play** > **Products** > **Subscriptions** > **Create subscription**.
   - Product ID: `liner_notes_plus`. Name: Liner Notes Plus.
   - Add a base plan: ID `monthly`, auto-renewing, billing period 1 month, price $1.00 (let Play set the other countries' prices). Activate it.
   - Add a base plan: ID `yearly`, auto-renewing, billing period 1 year, price $10.00. Activate it.
4. **Lifetime:** **Products** > **One-time products** (or In-app products) > **Create**.
   - Product ID: `liner_notes_plus_lifetime`. Name: Liner Notes Plus, lifetime. Price $25.00. Activate it.
5. **Testing without paying:** Play Console > **Settings** > **License testing**: add the testers' Gmail addresses. Their purchases are test purchases (subscriptions renew every few minutes and end after a few renewals).
6. **Data safety:** purchases add **Financial info > Purchase history** as collected by Google Play, not by the app. Play's form says purchases handled by Google Play Billing don't need to be declared; leave it off.

## Releasing

1. Internal testing: upload `LinerNotes-Android-<version>.aab` from the GitHub release `android` (the newest Play version is always there). Add yourself as a tester.
2. Closed testing (required for new personal accounts): at least 12 testers opted in for 14 days in a row. Ask friends with Android phones to join via the opt-in link.
3. Then apply for production access in Play Console.

The Play version doesn't update itself (Play updates it) and has no donation links (Google Play only allows its own payments in apps).
