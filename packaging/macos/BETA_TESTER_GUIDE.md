# Clippie Mac beta tester handoff — draft

This handoff is for a future signed, notarized beta. The current developer
prototype has not passed these distribution gates. Complete the release fields
below before inviting independent testers.

## Release details

- Download URL and version: **pending**
- Installer SHA-256: **pending**
- Supported Macs: Apple silicon; the current native dependency floor is macOS 26.2.
  The tested OS/RAM list must accompany the release; older systems and Intel are
  not declared supported.
- Free-space requirement, including models and recovery copies: **pending measurement**
- Test allowance for the provider account: **agree before generation**
- Support contact and known issues: **pending**

## Install and prepare

1. Download the supplied DMG, open it, and drag Clippie to Applications.
2. Open Clippie from Applications. Its editor opens in your browser. If macOS
   prevents launch, report the exact message; the release owner needs that result
   for the installation acceptance check.
3. In Settings, review the local model descriptions, download sizes and publisher
   terms. Prepare the required transcription and embedding models. Face tracking
   is optional. Keep the app open while preparation runs; interrupted downloads
   can be resumed from Settings.
4. In “Connect an AI provider,” obtain your own Gemini AI Studio API key, save it,
   explicitly check model access, then choose and apply a routing preset. Review
   any warnings. A model-list result alone does not prove generation will work.
5. Provider billing and quotas belong to your account. Local model preparation
   downloads model files; clip generation can send selected transcripts and
   sampled frames to the provider. Use only the agreed test allowance and media
   you are authorized to process.

## First project

Use a short local video with audible speech. Import it, prepare its transcript,
generate a small project within the agreed allowance, review the result, make a
manual edit, and export MP4 and SRT. Play the exported file outside Clippie.
Check speech, caption timing, framing and whether the clip preserves the
speaker’s meaning. Record failures and the point at which they happened.

The initial packaging acceptance focuses on local file input through export.
URL ingestion and connected publishing need their own release acceptance before
they are advertised as supported beta features.

## Quit, update and recovery

Quit using the Clippie app’s Quit action. Closing the browser tab alone does not
quit its local service. Before replacing the app with a newer version, fully quit
Clippie and keep the previous installer.

Models, projects and credentials live outside the replaceable app. A backend
upgrade can take time while Clippie creates a recovery copy. The native recovery
menu can restore a selected copy after stopping the local service. Restoration
preserves a separate rescue copy and pauses automatic background work for review.
Review projects and publishing state before enabling recovery for a later launch.
External files, Keychain credentials, remote uploads and provider charges are
outside local restoration.

## Report the result

Send the release owner the app version, macOS version, Mac model/RAM, browser,
the steps you tried, exact error text and whether the exported video played
correctly. Never include API keys or passwords. Share media, transcripts or
project data only if you intend the owner to receive them. Report disk use and
how long setup, first launch and export took where practical.
