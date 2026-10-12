# HR Screening Voice Agent

An open-source AI rep that recruiters and hiring managers can call or chat with to screen *you*.
It answers only from the profile you give it, says when it doesn't know, discloses that it's an
AI, and points serious prospects to you. Python (FastAPI + Pydantic AI on Groq) and a Next.js
voice UI; speech is ElevenLabs.

## Make it yours

```bash
cp config/example.md config/owner.md   # your name, roles and links, in plain Markdown
cp .env.example .env                   # GROQ_API_KEY (+ ElevenLabs for voice)

uv sync --all-packages
uv run python scripts/check_owner.py                                # shows what the rep read
uv run --package hireme-api uvicorn app:app --reload --port 8000   # API

cd web && npm install && npm run dev                               # UI on http://localhost:3000
```

`config/owner.md` is the rep's only source of facts about you: a `# Name` heading, an
`## Experience` list, a `## Links` list, and any other sections you like (About, Open to, FAQ),
which the rep quotes as your own words. Add `Pronouns:` or `Booking:` (a scheduling link; the
rep never reads your availability) under the name. Commit the file to your fork so your host
deploys it. Without it, the rep runs as the fictional example persona and logs a warning. To
change how the rep behaves, edit `agent/src/hireme_agent/prompts/system.md`.

## Configuration

All settings live in the repository-root `.env` (see `.env.example`); real environment
variables take precedence.

| Variable | |
| --- | --- |
| `GROQ_API_KEY` | Required. From console.groq.com. |
| `LLM_MODEL`, `LLM_REASONING_EFFORT` | Groq model (default `groq/openai/gpt-oss-120b`) and gpt-oss effort (`low` default). |
| `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID` | Required for voice; text works without them. Use a neutral voice, not a clone of yours. |
| `NEXT_PUBLIC_API_URL`, `CORS_ORIGINS` | Where the UI finds the API, and which origins the API accepts. |
| `API_SHARED_SECRET` | Bearer token required on `/turn` and `/greeting`; set it when the API is public. |
| `OWNER_FILE` | Optional path to the owner file (default `config/owner.md`, then `config/example.md`). |
| `POSTHOG_API_KEY`, `POSTHOG_API_URL`, `DEPLOYMENT_ENVIRONMENT` | Optional LLM tracing and browser analytics. |
| `NEXT_PUBLIC_POSTHOG_FEEDBACK_*` | Optional end-of-call feedback survey (survey, rating and comment question IDs). |

## How it works

```
web/app/          call UI: voice activity detection, streamed turns, ordered audio playback
   │ POST /greeting, POST /turn   (Server-Sent Events)
api/app.py        auth, CORS, SSE; conversation_turn.py runs STT → agent → per-sentence TTS
agent/            prompt + owner profile tools, Pydantic AI → Groq, bounded in-memory history
```

Text turns stream tokens. Voice turns transcribe first, then synthesize each reply sentence as
soon as it lands, so audio starts after the first sentence. Conversations are in memory only.

## Tests

```bash
uv run pytest -q && uv run ruff check api agent        # offline; no keys needed
cd web && npm test && npx tsc --noEmit
RUN_INTEGRATION=1 uv run pytest -q -m integration      # live providers, costs credits
docker build -t hireme . && docker run --env-file .env -p 8000:8000 hireme
```

## Contributing and security

Issues and pull requests are welcome. Keep personal details out of code (they belong in the
owner file) and run the checks above before opening a PR. Report vulnerabilities privately
through GitHub security advisories rather than in a public issue.

Created by [João Lousada](https://github.com/jlousada315). <!-- privacy: allow -->
Licensed under [MIT](LICENSE).
