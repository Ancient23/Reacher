---
title: Reachy Fleet Supervisor
emoji: 🤖
colorFrom: purple
colorTo: gray
sdk: static
pinned: false
license: apache-2.0
tags:
  - reachy_mini
  - reachy_mini_python_app
---

# Reachy Fleet Supervisor

Forked from the Reachy Mini conversation app (Pollen Robotics, Apache-2.0). See `NOTICE` for
attribution and `LICENSE` for the full license text.

## Secrets

Never commit a `.env` file (`.gitignore` already excludes it). Copy `.env.example` to `.env` for
local runs. When hosted as a Hugging Face Space, set the following as **Space secrets** (Settings →
Variables and secrets) rather than committing them:
- `OPENAI_API_KEY` — OpenAI Realtime (voice brain).
- `HF_TOKEN` — only if pulling gated models/datasets.
Coding itself uses the Claude Max plan via the local Claude Code CLI (no Anthropic API key is used
or stored by this app).

Use the `src/reachy_fleet_supervisor/profiles/_reachy_fleet_supervisor_locked_profile` folder to customize your own app from this template:
- Edit instructions `_reachy_fleet_supervisor_locked_profile/instructions.txt`
- Edit available tools in `_reachy_fleet_supervisor_locked_profile/tools.txt`
- You can create your own tools in `_reachy_fleet_supervisor_locked_profile` by subclassing the `Tool` class.

Do not forget to customize:
- this `README.md` file
- the `index.html` file (Hugging Face Spaces landing page)
- the `src/reachy_fleet_supervisor/static/index.html` (the web app parameters page)

The original README from the conversation app is available in `README_OLD.md`.