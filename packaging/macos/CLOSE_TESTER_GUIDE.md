# Clippie assisted alpha — close tester guide (draft)

The completed handoff for the current candidate is
`output/macos-close-testers/START_HERE.txt`, included in the complete tester ZIP.
See [the verified candidate report](../../docs/MAC_APP_CLOSE_TESTER_CANDIDATE_2026-10-27.md).
The draft fields below describe the earlier preparation state.

**Do not send this draft or the current runtime-spike DMG as an invitation.**
The exact assisted-alpha installer, notice/source delivery, private download and
clean-Mac test remain pending. This build is **unnotarized**: Apple has not checked
it for known malware and macOS may prevent it from opening. An intact local
signature does not establish an Apple-verified developer identity. Install only if
you trust the sender and have confirmed the download and checksum independently.

The owner completes these details for the exact build before sending this guide:

- Version: **[version]**
- Private installer link: **[link]**
- Installer SHA-256, confirmed with the owner through a separate trusted channel: **[checksum]**
- Notice and corresponding source access, including any separate download: **[location]**
- Support contact: **[contact]**
- Tested Mac models and macOS versions: **[list]**
- Free space needed for the app, models and recovery: **[measured amount]**
- Known issues: **[issues]**
- Agreed provider allowance and who pays: **[allowance]**

 The existing native inventory records a
26.2 minimum for the prototype on Apple silicon; this is not a completed device
compatibility claim. Intel support is unverified.

## Install

1. Confirm the link and checksum with the owner through a trusted channel. Download
   the supplied installer, open it and drag Clippie into Applications. If the image
   or app is reported damaged, contains malware, or is unexpectedly different,
   stop and contact the owner; do not bypass the warning.
2. Open Clippie from Applications. If macOS blocks it as unnotarized or from an
   unidentified developer, and you have independently decided to trust it, Apple
   describes an app-specific exception: after the blocked attempt, open System
   Settings → Privacy & Security → Open Anyway, then confirm Open. See
   [Apple's instructions](https://support.apple.com/en-au/102445). The option may
   not be available on managed Macs; ask your administrator rather than changing
   managed policy. Record the exact result for the owner.
3. Read the delivered third-party notices and source information. In Settings,
   review model download sizes and publisher terms before preparing the needed
   transcription and embedding models. Face tracking is optional. Downloads need
   network and disk space; interrupted downloads can be resumed.
4. Connect your own Gemini AI Studio key if agreed, check model access and select
   a routing preset. Your account's billing and quotas apply. Generation can send
   selected transcripts and sampled frames to the provider. Use authorised media
   and stay within the agreed allowance. Never share keys or passwords.

## Test and report

Try a short local video, create a small project within the agreed allowance, edit,
export MP4 and SRT, then play the result outside Clippie. Review speech, captions,
framing and meaning. Report the exact steps, error messages, version, Mac model,
RAM, macOS and browser. Share media or project data only deliberately. Local-file
input through export is the initial scope; URL ingestion and connected publishing
need separate acceptance.

Quit with the app's Quit action before replacing it; closing the browser alone
leaves the local service running. Retain the previous installer and backups. Data
and credentials live outside the replaceable app; local recovery does not undo
provider charges, remote uploads or changes to external files. Ask the owner for
help before restoring or replacing data.
