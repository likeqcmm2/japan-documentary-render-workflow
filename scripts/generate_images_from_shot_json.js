#!/usr/bin/env node

const fs = require("fs");
const path = require("path");
const process = require("process");

const OpenAI = require("openai");

const MODEL = "gpt-image-2";
const SIZE = "1536x864";
const QUALITY = "low";
const OUTPUT_FORMAT = "png";
const REQUEST_TIMEOUT_MS = 150_000;
const MAX_CONCURRENT_REQUESTS = 15;
const REQUEST_START_INTERVAL_MS = 4_000;
const MAX_RETRIES = 2;
const RETRY_BASE_DELAY_MS = 5_000;

function loadEnvFile(envPath) {
  if (!fs.existsSync(envPath)) return;
  const lines = fs.readFileSync(envPath, "utf8").split(/\r?\n/);
  for (const rawLine of lines) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) continue;
    const match = line.match(/^([A-Za-z_][A-Za-z0-9_]*)=(.*)$/);
    if (!match) continue;
    const key = match[1];
    let value = match[2].trim();
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1);
    }
    process.env[key] = value;
  }
}

function parseArgs(argv) {
  const args = {
    input: path.join(process.cwd(), "inputs", "shot_list.json"),
    output: path.join(process.cwd(), "generated_images"),
    force: false,
    startId: null,
    endId: null,
    limit: null,
  };

  for (let i = 2; i < argv.length; i += 1) {
    const arg = argv[i];
    const next = argv[i + 1];
    if (arg === "--input" && next) {
      args.input = path.resolve(next);
      i += 1;
    } else if (arg === "--output" && next) {
      args.output = path.resolve(next);
      i += 1;
    } else if (arg === "--force") {
      args.force = true;
    } else if (arg === "--start-id" && next) {
      args.startId = Number.parseInt(next, 10);
      i += 1;
    } else if (arg === "--end-id" && next) {
      args.endId = Number.parseInt(next, 10);
      i += 1;
    } else if (arg === "--limit" && next) {
      args.limit = Number.parseInt(next, 10);
      i += 1;
    } else {
      throw new Error(`Unknown argument: ${arg}`);
    }
  }
  return args;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function withTimeoutAbort(operation, timeoutMs) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await operation(controller.signal);
  } catch (error) {
    if (controller.signal.aborted) {
      const timeoutError = new Error(`Request timed out after ${Math.ceil(timeoutMs / 1000)}s.`);
      timeoutError.code = "REQUEST_TIMEOUT";
      timeoutError.name = "RequestTimeoutError";
      throw timeoutError;
    }
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}

function describeError(error) {
  const parts = [];
  if (error?.status) parts.push(`status=${error.status}`);
  if (error?.code) parts.push(`code=${error.code}`);
  if (error?.type) parts.push(`type=${error.type}`);
  if (error?.message) parts.push(error.message);
  return parts.join(" | ") || String(error);
}

function shouldRetry(error) {
  const status = error?.status;
  const code = error?.code;
  const type = error?.type;
  if (code === "moderation_blocked") return false;
  if (type === "image_generation_user_error") return false;
  if (status === 429) return true;
  if (status >= 500) return true;
  if (code === "ETIMEDOUT" || code === "ECONNRESET") return true;
  if (code === "REQUEST_TIMEOUT") return true;
  if (error?.name === "RequestTimeoutError") return true;
  if (error?.name === "AbortError") return true;
  if (error?.name === "APIConnectionTimeoutError") return true;
  if (error?.name === "APIConnectionError") return true;
  return false;
}

function isFatalConfigError(error) {
  const status = error?.status;
  const code = error?.code;
  return (
    status === 401 ||
    status === 403 ||
    code === "invalid_api_key" ||
    code === "billing_not_active" ||
    code === "insufficient_quota"
  );
}

function normalizeItems(data) {
  if (Array.isArray(data)) return data;
  return data.segments || data.shots || data.items || [];
}

function outputNameForId(id) {
  return `shot_${String(id).padStart(3, "0")}.png`;
}

function buildJobs(inputPath, outputDir) {
  const data = JSON.parse(fs.readFileSync(inputPath, "utf8"));
  return normalizeItems(data).map((item, index) => {
    const id = item.id ?? index + 1;
    const shot = String(item.shot || "").trim();
    const onScreenText = item.on_screen_text_ja == null ? "" : String(item.on_screen_text_ja).trim();
    const prompt = onScreenText ? `${shot}${onScreenText}` : shot;
    return {
      id,
      mediaType: item.media_type || "",
      prompt,
      outputName: outputNameForId(id),
      item,
    };
  }).filter((job) => job.prompt);
}

function writeJsonl(filePath, entry) {
  fs.appendFileSync(filePath, `${JSON.stringify(entry)}\n`);
}

async function generateOne({ client, job, outputPath }) {
  for (let attempt = 1; attempt <= MAX_RETRIES + 1; attempt += 1) {
    try {
      console.log(`[generate] shot_${String(job.id).padStart(3, "0")} media=${job.mediaType} attempt=${attempt}`);
      const result = await withTimeoutAbort(
        async (signal) => client.images.generate({
          model: MODEL,
          prompt: job.prompt,
          size: SIZE,
          quality: QUALITY,
          output_format: OUTPUT_FORMAT,
          n: 1,
        }, { signal }),
        REQUEST_TIMEOUT_MS,
      );
      const base64 = result?.data?.[0]?.b64_json;
      if (!base64) throw new Error("API response did not include data[0].b64_json.");
      fs.writeFileSync(outputPath, Buffer.from(base64, "base64"));
      console.log(`[done] ${outputPath}`);
      return { ok: true };
    } catch (error) {
      const message = describeError(error);
      console.error(`[error] shot_${String(job.id).padStart(3, "0")} attempt=${attempt}: ${message}`);
      if (isFatalConfigError(error)) return { ok: false, fatal: true, error: message };
      if (attempt > MAX_RETRIES || !shouldRetry(error)) return { ok: false, error: message };
      const delay = RETRY_BASE_DELAY_MS * attempt;
      console.log(`[retry] waiting ${Math.ceil(delay / 1000)}s`);
      await sleep(delay);
    }
  }
  return { ok: false, error: "Unknown generation failure." };
}

async function runJobs({ client, jobs, outputDir, logPath }) {
  let nextIndex = 0;
  let active = 0;
  let stopRequested = false;
  let failed = false;

  return new Promise((resolve) => {
    const maybeLaunch = () => {
      if (stopRequested) {
        if (active === 0) resolve({ failed });
        return;
      }

      while (active < MAX_CONCURRENT_REQUESTS && nextIndex < jobs.length && !stopRequested) {
        const job = jobs[nextIndex];
        nextIndex += 1;
        active += 1;

        const outputPath = path.join(outputDir, job.outputName);
        const startedAt = new Date().toISOString();
        generateOne({ client, job, outputPath })
          .then((result) => {
            writeJsonl(logPath, {
              time: new Date().toISOString(),
              startedAt,
              id: job.id,
              mediaType: job.mediaType,
              outputPath,
              ok: result.ok,
              error: result.error || null,
              prompt: job.prompt,
            });

            if (!result.ok) {
              failed = true;
              stopRequested = true;
              console.error(`[stop] generation failed for shot_${String(job.id).padStart(3, "0")}; stopping new launches for manual prompt fix.`);
            }
          })
          .catch((error) => {
            failed = true;
            stopRequested = true;
            const message = describeError(error);
            writeJsonl(logPath, {
              time: new Date().toISOString(),
              startedAt,
              id: job.id,
              mediaType: job.mediaType,
              outputPath,
              ok: false,
              error: message,
              prompt: job.prompt,
            });
            console.error(`[stop] generation crashed for shot_${String(job.id).padStart(3, "0")}: ${message}`);
          })
          .finally(() => {
            active -= 1;
            if ((stopRequested || nextIndex >= jobs.length) && active === 0) {
              resolve({ failed });
            }
          });

        if (active >= MAX_CONCURRENT_REQUESTS || nextIndex >= jobs.length) break;
        setTimeout(maybeLaunch, REQUEST_START_INTERVAL_MS);
        return;
      }

      if (nextIndex >= jobs.length && active === 0) resolve({ failed });
    };

    maybeLaunch();
  });
}

async function main() {
  loadEnvFile(path.join(process.cwd(), ".env"));
  loadEnvFile(path.join(process.cwd(), "secrets", ".env"));
  const args = parseArgs(process.argv);
  const apiKey = process.env.OPENAI_API_KEY;
  if (!apiKey) throw new Error("Missing OPENAI_API_KEY.");

  fs.mkdirSync(args.output, { recursive: true });
  let jobs = buildJobs(args.input, args.output);
  if (Number.isInteger(args.startId)) jobs = jobs.filter((job) => job.id >= args.startId);
  if (Number.isInteger(args.endId)) jobs = jobs.filter((job) => job.id <= args.endId);
  jobs = jobs.filter((job) => args.force || !fs.existsSync(path.join(args.output, job.outputName)));
  if (Number.isInteger(args.limit)) jobs = jobs.slice(0, args.limit);

  console.log(`[input] ${args.input}`);
  console.log(`[output] ${args.output}`);
  console.log(`[config] model=${MODEL} size=${SIZE} quality=${QUALITY} format=${OUTPUT_FORMAT}`);
  console.log(`[rate] max_concurrent=${MAX_CONCURRENT_REQUESTS} start_interval=${REQUEST_START_INTERVAL_MS / 1000}s`);
  console.log(`[jobs] selected=${jobs.length}`);

  const client = new OpenAI({ apiKey });
  const logPath = path.join(args.output, "api-run-log.jsonl");
  const result = await runJobs({ client, jobs, outputDir: args.output, logPath });
  if (result.failed) {
    process.exitCode = 1;
    return;
  }
  console.log("[summary] all selected images completed");
}

main().catch((error) => {
  console.error(`[fatal] ${describeError(error)}`);
  process.exit(1);
});
