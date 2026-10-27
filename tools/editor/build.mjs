// Builds app/static/vendor/editor/editor.js and its LICENSES.txt from entry.js,
// and copies PDF.js to app/static/vendor/pdfjs/. Run it through `make editor`,
// which installs the locked packages first.
//
// It refuses to write a bundle that holds two copies of one package. Two yjs
// copies break every `instanceof` between y-websocket, y-codemirror.next and
// the page, and two @codemirror/state copies break CodeMirror the same way.

import { build } from "esbuild";
import { copyFileSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const outDir = join(here, "..", "..", "app", "static", "vendor", "editor");
const outFile = join(outDir, "editor.js");
mkdirSync(outDir, { recursive: true });

const result = await build({
  absWorkingDir: here,
  entryPoints: ["entry.js"],
  outfile: outFile,
  bundle: true,
  format: "esm",
  platform: "browser",
  target: ["es2020"],
  minify: true,
  legalComments: "none",
  metafile: true,
  banner: { js: "/* meertex editor bundle, built by tools/editor (make editor). Licenses: LICENSES.txt next to this file. */" },
  logLevel: "warning",
});

// Every package directory the bundle took code from, by package name.
const packages = new Map();
for (const input of Object.keys(result.metafile.inputs)) {
  const m = input.match(/^(.*node_modules\/((?:@[^/]+\/)?[^/]+))\//);
  if (!m) continue;
  const [, dir, name] = m;
  if (!packages.has(name)) packages.set(name, new Set());
  packages.get(name).add(dir);
}
const doubled = [...packages].filter(([, dirs]) => dirs.size > 1);
if (doubled.length) {
  for (const [name, dirs] of doubled) console.error(`${name} is bundled ${dirs.size} times: ${[...dirs].join(", ")}`);
  process.exit(1);
}

const LICENSE_FILE = /^(licen[cs]e|copying)(\.(md|txt))?$/i;
const sections = [];
for (const name of [...packages.keys()].sort()) {
  const dir = join(here, [...packages.get(name)][0]);
  const pkg = JSON.parse(readFileSync(join(dir, "package.json"), "utf8"));
  const file = readdirSync(dir).find((f) => LICENSE_FILE.test(f));
  const text = file ? readFileSync(join(dir, file), "utf8").trim() : `License: ${pkg.license} (the package ships no license file)`;
  sections.push(`${"=".repeat(78)}\n${pkg.name} ${pkg.version} (${pkg.license})\n${"=".repeat(78)}\n\n${text}\n`);
}
writeFileSync(join(outDir, "LICENSES.txt"),
  "The editor bundle (editor.js) contains code from these packages, under these licenses.\n\n" + sections.join("\n"));

const size = readFileSync(outFile).length;
const names = [...packages.keys()].sort().map((n) => {
  const dir = join(here, [...packages.get(n)][0]);
  return `${n}@${JSON.parse(readFileSync(join(dir, "package.json"), "utf8")).version}`;
});
console.log(`${relative(process.cwd(), outFile)}: ${(size / 1024).toFixed(0)} KB from ${names.length} packages`);
console.log(`  ${names.join("\n  ")}`);

// PDF.js comes ready to use as two ES modules, the library and its worker.
// They are copied as they are, so the worker stays a file of its own. The
// legacy build carries polyfills for what the newest browsers only just got
// (Map.prototype.getOrInsertComputed), for 60 KB more.
const pdfDir = join(here, "..", "..", "app", "static", "vendor", "pdfjs");
const pdfjs = join(here, "node_modules", "pdfjs-dist");
const pdfPkg = JSON.parse(readFileSync(join(pdfjs, "package.json"), "utf8"));
mkdirSync(pdfDir, { recursive: true });
copyFileSync(join(pdfjs, "legacy", "build", "pdf.min.mjs"), join(pdfDir, "pdf.js"));
copyFileSync(join(pdfjs, "legacy", "build", "pdf.worker.min.mjs"), join(pdfDir, "pdf.worker.js"));
writeFileSync(join(pdfDir, "LICENSE.txt"),
  `pdf.js and pdf.worker.js are ${pdfPkg.name} ${pdfPkg.version} (${pdfPkg.license}), copied unchanged from legacy/build.\n\n`
  + readFileSync(join(pdfjs, "LICENSE"), "utf8"));
console.log(`${relative(process.cwd(), pdfDir)}: ${pdfPkg.name}@${pdfPkg.version}`);
