# Audiobook Factory — GitHub Actions

Automates long-form motivational audiobook videos using Gemini, Microsoft Edge TTS, FFmpeg, and the YouTube Data API.

## Included layout

```text
.
├── .github/workflows/daily.yml
├── audiobook_factory.py
├── queue.txt
├── requirements.txt
├── clips/       # Put landscape background video clips here
├── music/       # Put ambient music tracks here
├── covers/      # Six demo covers included; add more to reach 30+
├── topics/      # Topic notes / optional research text files
├── output/      # Rendered video and upload metadata (not committed)
└── work/        # Temporary audio, script and subtitle files (not committed)
```

Git does not track empty folders, so `.gitkeep` files are included.

## 1. Add your assets

- `clips/`: add one or more licensed background clips, preferably 2–3 minutes each. Supported: MP4, MOV, MKV, WEBM, M4V.
- `music/`: add licensed ambient tracks. Supported: MP3, WAV, M4A, AAC, OGG, FLAC.
- `covers/`: six original demo covers (`cover_01.jpg`–`cover_06.jpg`) are included and match the six starter queue entries. Add your own covers and extend `queue.txt` when ready; 30+ covers are supported. JPG, PNG, and WEBP are supported.
- `topics/`: optional topic research notes. The queue itself is `queue.txt`.

The included queue has 30 example motivational topics. Replace them with your own topics if desired. **The ZIP includes six original placeholder-style sample covers, one 12-second sample background clip, and one short synthesized ambient WAV so you can inspect the folder layout. Replace these demo assets with your own licensed assets for production.**

## 2. Add GitHub Actions secrets

In your repository, open **Settings → Secrets and variables → Actions → New repository secret** and add:

| Secret | Value |
|---|---|
| `GEMINI_API_KEY` | Google AI Studio API key |
| `YOUTUBE_CLIENT_ID` | OAuth client ID used for your YouTube channel |
| `YOUTUBE_CLIENT_SECRET` | Matching OAuth client secret |
| `YOUTUBE_REFRESH_TOKEN` | Refresh token authorized for the target YouTube channel |

All three YouTube values must belong to the same OAuth client/account authorization. The Google account used during OAuth must have access to the channel that should publish videos. Never commit these values to the repository.

## 3. Enable queue commits

The workflow requests `contents: write`. In repository **Settings → Actions → General → Workflow permissions**, allow read and write permissions. If branch protection blocks direct pushes to the default branch, adjust it or change the workflow to open a pull request.

The queue advances only after YouTube returns a video ID for a successful upload. The workflow then commits the updated `queue.txt`.

## 4. Run it

1. Push this repository to GitHub.
2. Add your assets and secrets.
3. Open **Actions → Daily Audiobook Factory → Run workflow**.
4. Check the logs. The first full run may take a long time because the target is 40–50 minutes of narration.
5. Scheduled runs start at 02:00 UTC (07:00 Pakistan Standard Time).

**Important:** Important: the manual `test` input currently only prints a reminder; it does not shorten narration or prevent a public upload. Do not run the workflow until secrets are configured and you are ready to publish. For a safe first run, set `YOUTUBE_PRIVACY_STATUS=unlisted` and temporarily reduce the script section sizes in `audiobook_factory.py`; restore your desired production settings after validation.

## Output and visual layout

- 1920×1080, 30 FPS, H.264 video.
- AAC 192 kbps narration/music mix.
- Cover overlay defaults: x=260, y=150, width=540, height=820.
- Right-center captions in bold white with an amber/orange background box.
- Ambient music volume is `0.08`.
- Word boundary metadata from Edge TTS is used to time 3–4-word subtitle groups.

Optional GitHub Actions environment variables:
- `GEMINI_MODELS`: comma-separated Gemini model IDs, in fallback order.
- `TARGET_MINUTES`: target prompt length (default 45; actual duration is determined by generated text).
- `VIDEO_BITRATE`: default `6000k`.
- `COVER_X`, `COVER_Y`, `COVER_WIDTH`, `COVER_HEIGHT`: cover placement.
- `YOUTUBE_PRIVACY_STATUS`: default public.

## Important operational notes

- Gemini model availability changes. If the configured model is not available for your API key, the script falls back to the next model. Override `GEMINI_MODELS` with IDs available to your account.
- The generated narration length is approximate. The script logs actual audio duration; if it falls outside 40–50 minutes, adjust section length or script limits.
- Edge TTS is a community client for Microsoft's online speech service, not a guaranteed SLA service. Network throttling or service changes can fail a run.
- A YouTube refresh token can be revoked or expire; reauthorize and update the secret if upload authentication fails.
- Use only book covers, footage, and music you own or are licensed to publish. Public uploads must follow YouTube policies and copyright law.
- The workflow keeps generated media out of Git; it commits only the queue update.
