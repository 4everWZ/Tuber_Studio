import { execFileSync } from "node:child_process";
import { existsSync, readFileSync, rmSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const frontendDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = path.resolve(frontendDir, "..");
const exportScript = path.join(repoRoot, "tools", "ci", "export_openapi.py");
const targetSchema = path.join(frontendDir, "src", "shared", "api", "schema.d.ts");
const tmpOpenApi = path.join(frontendDir, ".openapi.tmp.json");
const tmpSchema = path.join(frontendDir, ".schema.tmp.d.ts");
const checkOnly = process.argv.includes("--check");

function findPythonRunner() {
  const venvCandidates = [
    path.join(repoRoot, ".venv", "Scripts", "python.exe"),
    path.join(repoRoot, ".venv", "bin", "python"),
  ];
  for (const candidate of venvCandidates) {
    if (existsSync(candidate)) {
      return { cmd: candidate, prefixArgs: [] };
    }
  }
  try {
    execFileSync("python", ["-c", "import fastapi, tuber_studio"], {
      cwd: repoRoot,
      stdio: "ignore",
      env: { ...process.env, PYTHONPATH: path.join(repoRoot, "src") },
    });
    return { cmd: "python", prefixArgs: [] };
  } catch {
    return { cmd: "uv", prefixArgs: ["run", "--locked", "python"] };
  }
}

function runCommand(cmd, args, cwd = frontendDir) {
  execFileSync(cmd, args, { cwd, stdio: "inherit" });
}

function generateTypes(inputJsonPath, outputTsPath) {
  const pkgCli = path.join(frontendDir, "node_modules", "openapi-typescript", "bin", "cli.js");
  if (!existsSync(pkgCli)) {
    throw new Error("openapi-typescript CLI not found in node_modules");
  }
  runCommand(process.execPath, [pkgCli, inputJsonPath, "-o", outputTsPath]);
}

function normalizeText(text) {
  return text.replace(/\r\n/g, "\n");
}

try {
  const py = findPythonRunner();
  runCommand(py.cmd, [...py.prefixArgs, exportScript, "--output", tmpOpenApi], repoRoot);

  if (checkOnly) {
    if (!existsSync(targetSchema)) {
      console.error(`Missing generated OpenAPI TypeScript file: ${targetSchema}`);
      process.exitCode = 1;
    } else {
      generateTypes(tmpOpenApi, tmpSchema);
      const expected = normalizeText(readFileSync(tmpSchema, "utf8"));
      const actual = normalizeText(readFileSync(targetSchema, "utf8"));
      if (expected !== actual) {
        console.error(
          "OpenAPI TypeScript schema drift detected in src/shared/api/schema.d.ts. Run `pnpm --dir frontend codegen`."
        );
        process.exitCode = 1;
      } else {
        console.log("OpenAPI TypeScript schema is up to date.");
      }
    }
  } else {
    generateTypes(tmpOpenApi, targetSchema);
    console.log(`Generated ${path.relative(repoRoot, targetSchema)}`);
  }
} finally {
  rmSync(tmpOpenApi, { force: true });
  rmSync(tmpSchema, { force: true });
}
