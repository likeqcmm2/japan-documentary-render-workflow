# Local secrets only

Do not commit real keys or tokens.

Create these files locally on the Mac or on the Vast server when needed:

```bash
cp .env.example secrets/.env
```

Typical values:

```bash
OPENAI_API_KEY=sk-proj-...
HF_TOKEN=hf_...
RCLONE_REMOTE=gdrive
DRIVE_OUTPUT_DIR=Japan_Project_Render_Workflow/final
```

For Google Drive upload, configure rclone outside git:

```bash
rclone config
```

or copy the Mac's local rclone config to the server manually if the user explicitly approves.
