import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { spawn } from "node:child_process";

const packageDirectory = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const mode = process.argv[2];
const source = resolve(packageDirectory, process.argv[3] ?? process.env.KITSUNE_OPENAPI_SCHEMA ?? "../../services/workspace/openapi.json");
const committedOutput = resolve(packageDirectory, "src/api/openapi.generated.ts");
const executable = resolve(packageDirectory, "node_modules/.bin/openapi-typescript");

if (mode !== "generate" && mode !== "check") {
  throw new Error("Usage: openapi-client.mjs <generate|check> [openapi-schema-path]");
}

const generate = (output) =>
  new Promise((resolvePromise, rejectPromise) => {
    const child = spawn(executable, [source, "--output", output, "--alphabetize"], {
      cwd: packageDirectory,
      stdio: "inherit",
    });
    child.on("error", rejectPromise);
    child.on("exit", (code) => {
      if (code === 0) resolvePromise();
      else rejectPromise(new Error(`openapi-typescript exited with code ${String(code)}`));
    });
  });

if (mode === "generate") {
  await generate(committedOutput);
  process.stdout.write(`Generated ${committedOutput} from ${source}\n`);
} else {
  const temporaryDirectory = await mkdtemp(join(tmpdir(), "kitsune-openapi-"));
  const temporaryOutput = join(temporaryDirectory, "openapi.generated.ts");
  try {
    await generate(temporaryOutput);
    const [expected, actual] = await Promise.all([
      readFile(committedOutput, "utf8"),
      readFile(temporaryOutput, "utf8"),
    ]);
    if (expected !== actual) {
      process.stderr.write(`OpenAPI client is stale. Run: pnpm generate:api -- ${source}\n`);
      process.exitCode = 1;
    } else {
      process.stdout.write(`OpenAPI client matches ${source}\n`);
    }
  } finally {
    await rm(temporaryDirectory, { recursive: true, force: true });
  }
}
