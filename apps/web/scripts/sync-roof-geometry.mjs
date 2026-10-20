import { copyFile, mkdir, readFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const scriptDirectory = dirname(fileURLToPath(import.meta.url));
const repositoryRoot = resolve(scriptDirectory, "../../..");
const sourcePath = resolve(repositoryRoot, "fixtures/geometry/demo_roofr_83_rue_falardeau.json");
const destinationPath = resolve(
  scriptDirectory,
  "../public/geometry/demo_roofr_83_rue_falardeau.json",
);

try {
  const source = await readFile(sourcePath, "utf8");
  JSON.parse(source);
  await mkdir(dirname(destinationPath), { recursive: true });
  await copyFile(sourcePath, destinationPath);
} catch (error) {
  const message = error instanceof Error ? error.message : "unknown error";
  console.error(`Unable to prepare the demo roof static asset: ${message}`);
  process.exitCode = 1;
}
