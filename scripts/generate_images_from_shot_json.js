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
const MAX_CONCURRENT_REQUESTS = 30;
const REQUEST_START_INTERVAL_MS = 2_000;
const MAX_RETRIES = 2;
const RETRY_BASE_DELAY_MS = 5_000;
const POST_PASS_RETRY_ROUNDS = 3;

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

function shotLabel(runtimeId) {
  return `shot_${String(runtimeId).padStart(3, "0")}`;
}

function outputNameForRuntimeId(runtimeId) {
  return `${shotLabel(runtimeId)}.png`;
}

function buildJobs(inputPath, outputDir) {
  const data = JSON.parse(fs.readFileSync(inputPath, "utf8"));
  return normalizeItems(data).map((item, index) => {
    const runtimeId = index + 1;
    const sourceId = item.id ?? runtimeId;
    const shot = String(item.shot || "").trim();
    const onScreenText = item.on_screen_text_ja == null ? "" : String(item.on_screen_text_ja).trim();
    const prompt = onScreenText ? `${shot}${onScreenText}` : shot;
    return {
      id: runtimeId,
      runtimeId,
      sourceId,
      mediaType: item.media_type || "",
      prompt,
      outputName: outputNameForRuntimeId(runtimeId),
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
      console.log(`[generate] ${shotLabel(job.runtimeId)} source_id=${job.sourceId} media=${job.mediaType} attempt=${attempt}`);
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
      console.error(`[error] ${shotLabel(job.runtimeId)} source_id=${job.sourceId} attempt=${attempt}: ${message}`);
      if (isFatalConfigError(error)) return { ok: false, fatal: true, error: message };
      if (attempt > MAX_RETRIES || !shouldRetry(error)) return { ok: false, error: message };
      const delay = RETRY_BASE_DELAY_MS * attempt;
      console.log(`[retry] waiting ${Math.ceil(delay / 1000)}s`);
      await sleep(delay);
    }
  }
  return { ok: false, error: "Unknown generation failure." };
}

async function runJobs({ client, jobs, outputDir, logPath, phase }) {
  let nextIndex = 0;
  let active = 0;
  const failedJobs = [];

  return new Promise((resolve) => {
    const finishIfDone = () => {
      if (nextIndex >= jobs.length && active === 0) {
        resolve({ failedJobs });
      }
    };

    const launchNext = () => {
      if (nextIndex >= jobs.length) {
        finishIfDone();
        return;
      }

      if (active >= MAX_CONCURRENT_REQUESTS) {
        setTimeout(launchNext, REQUEST_START_INTERVAL_MS);
        return;
      }

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
            phase,
            id: job.sourceId,
            runtimeId: job.runtimeId,
            mediaType: job.mediaType,
            outputPath,
            ok: result.ok,
            fatal: Boolean(result.fatal),
            error: result.error || null,
            prompt: job.prompt,
          });

          if (!result.ok) {
            failedJobs.push({
              job,
              error: result.error || "Unknown generation failure.",
              fatal: Boolean(result.fatal),
              phase,
            });
            console.error(`[${phase}] generation failed for ${shotLabel(job.runtimeId)} source_id=${job.sourceId}; recording failure and continuing the batch.`);
          }
        })
        .catch((error) => {
          const message = describeError(error);
          failedJobs.push({ job, error: message, fatal: false, phase });
          writeJsonl(logPath, {
            time: new Date().toISOString(),
            startedAt,
            phase,
            id: job.sourceId,
            runtimeId: job.runtimeId,
            mediaType: job.mediaType,
            outputPath,
            ok: false,
            fatal: false,
            error: message,
            prompt: job.prompt,
          });
          console.error(`[${phase}] generation crashed for ${shotLabel(job.runtimeId)} source_id=${job.sourceId}; recording failure and continuing the batch: ${message}`);
        })
        .finally(() => {
          active -= 1;
          finishIfDone();
        });

      if (nextIndex < jobs.length) {
        setTimeout(launchNext, REQUEST_START_INTERVAL_MS);
      } else {
        finishIfDone();
      }
    };

    if (jobs.length === 0) {
      resolve({ failedJobs });
      return;
    }
    launchNext();
  });
}

function writeFailureManifest(outputDir, failedJobs, roundsAttempted) {
  const manifestPath = path.join(outputDir, "failed-images.json");
  const manifest = {
    generatedAt: new Date().toISOString(),
    roundsAttempted,
    failures: failedJobs.map(({ job, error, fatal, phase }) => ({
      id: job.sourceId,
      runtimeId: job.runtimeId,
      mediaType: job.mediaType,
      outputName: job.outputName,
      outputPath: path.join(outputDir, job.outputName),
      prompt: job.prompt,
      error,
      fatal,
      lastPhase: phase,
    })),
  };
  fs.writeFileSync(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`);
  return manifestPath;
}

async function main() {
  loadEnvFile(path.join(process.cwd(), ".env"));
  loadEnvFile(path.join(process.cwd(), "secrets", ".env"));
  const args = parseArgs(process.argv);
  const apiKey = process.env.OPENAI_API_KEY;
  if (!apiKey) throw new Error("Missing OPENAI_API_KEY.");

  fs.mkdirSync(args.output, { recursive: true });
  let jobs = buildJobs(args.input, args.output);
  if (Number.isInteger(args.startId)) jobs = jobs.filter((job) => job.runtimeId >= args.startId);
  if (Number.isInteger(args.endId)) jobs = jobs.filter((job) => job.runtimeId <= args.endId);
  jobs = jobs.filter((job) => args.force || !fs.existsSync(path.join(args.output, job.outputName)));
  if (Number.isInteger(args.limit)) jobs = jobs.slice(0, args.limit);

  console.log(`[input] ${args.input}`);
  console.log(`[output] ${args.output}`);
  console.log(`[config] model=${MODEL} size=${SIZE} quality=${QUALITY} format=${OUTPUT_FORMAT}`);
  console.log(`[rate] max_concurrent=${MAX_CONCURRENT_REQUESTS} start_interval=${REQUEST_START_INTERVAL_MS / 1000}s`);
  console.log(`[jobs] selected=${jobs.length}`);

  const client = new OpenAI({ apiKey });
  const logPath = path.join(args.output, "api-run-log.jsonl");
  let result = await runJobs({
    client,
    jobs,
    outputDir: args.output,
    logPath,
    phase: "initial",
  });
  let failedJobs = result.failedJobs;

  console.log(`[summary] initial pass complete: failed=${failedJobs.length}`);

  if (failedJobs.some((entry) => entry.fatal)) {
    const manifestPath = writeFailureManifest(args.output, failedJobs, 0);
    console.error(`[pause] fatal API configuration error; fix credentials or billing before continuing. See ${manifestPath}`);
    process.exitCode = 1;
    return;
  }

  for (let round = 1; round <= POST_PASS_RETRY_ROUNDS && failedJobs.length > 0; round += 1) {
    console.log(`[retry-round] ${round}/${POST_PASS_RETRY_ROUNDS} failed_images=${failedJobs.length}`);
    result = await runJobs({
      client,
      jobs: failedJobs.map((entry) => entry.job),
      outputDir: args.output,
      logPath,
      phase: `retry-${round}`,
    });
    failedJobs = result.failedJobs;

    if (failedJobs.some((entry) => entry.fatal)) {
      const manifestPath = writeFailureManifest(args.output, failedJobs, round);
      console.error(`[pause] fatal API configuration error during retry; see ${manifestPath}`);
      process.exitCode = 1;
      return;
    }
  }

  if (failedJobs.length > 0) {
    const manifestPath = writeFailureManifest(args.output, failedJobs, POST_PASS_RETRY_ROUNDS);
    const ids = failedJobs.map((entry) => `${shotLabel(entry.job.runtimeId)}(source_id=${entry.job.sourceId})`).join(", ");
    console.error(`[pause] ${failedJobs.length} image(s) still failed after ${POST_PASS_RETRY_ROUNDS} retry rounds: ${ids}`);
    console.error(`[pause] Fix the prompts or API issue, then rerun the same workflow. See ${manifestPath}`);
    process.exitCode = 1;
    return;
  }

  const failureManifestPath = path.join(args.output, "failed-images.json");
  if (fs.existsSync(failureManifestPath)) fs.unlinkSync(failureManifestPath);
  console.log("[summary] all selected images completed");
}

main().catch((error) => {
  console.error(`[fatal] ${describeError(error)}`);
  process.exit(1);
});
