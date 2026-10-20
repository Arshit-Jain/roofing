import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import ts from "typescript";

const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const webRoot = path.resolve(scriptDirectory, "..");
const tempRoot = mkdtempSync(path.join(os.tmpdir(), "roofline-geometry-tests-"));
const fixturePath = path.resolve(webRoot, "../../fixtures/geometry/demo_roofr_83_rue_falardeau.json");
const sources = [
  [path.join(webRoot, "lib/roof/geometry.ts"), path.join(tempRoot, "geometry.js")],
  [path.join(webRoot, "lib/roof/geometry.test.ts"), path.join(tempRoot, "geometry.test.cjs")],
  [path.join(webRoot, "lib/roof/geometry-review.ts"), path.join(tempRoot, "geometry-review.js")],
  [path.join(webRoot, "lib/roof/geometry-review.test.ts"), path.join(tempRoot, "geometry-review.test.cjs")],
];

try {
  for (const [sourcePath, outputPath] of sources) {
    const source = readFileSync(sourcePath, "utf8");
    const result = ts.transpileModule(source, {
      fileName: sourcePath,
      compilerOptions: {
        target: ts.ScriptTarget.ES2022,
        module: ts.ModuleKind.CommonJS,
        esModuleInterop: true,
      },
      reportDiagnostics: true,
    });
    const errors = (result.diagnostics ?? []).filter((diagnostic) => diagnostic.category === ts.DiagnosticCategory.Error);
    if (errors.length > 0) {
      throw new Error(ts.formatDiagnosticsWithColorAndContext(errors, {
        getCurrentDirectory: () => webRoot,
        getCanonicalFileName: (fileName) => fileName,
        getNewLine: () => "\n",
      }));
    }
    writeFileSync(outputPath, result.outputText);
  }

  const result = spawnSync(process.execPath, ["--test", sources[1][1], sources[3][1]], {
    cwd: webRoot,
    env: {
      ...process.env,
      NODE_PATH: path.join(webRoot, "node_modules"),
      ROOF_DEMO_FIXTURE_PATH: fixturePath,
    },
    stdio: "inherit",
  });
  if (result.error) throw result.error;
  process.exitCode = result.status ?? 1;
} finally {
  rmSync(tempRoot, { recursive: true, force: true });
}
