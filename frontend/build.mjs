import { build } from "esbuild";
import { copyFile, mkdir, access } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

const root = path.dirname(fileURLToPath(import.meta.url));
const config = path.join(root, "config.js");
try {
  await access(config);
} catch {
  throw new Error("Create frontend/config.js from config.example.js before building.");
}
const dist = path.join(root, "dist");
await mkdir(dist, { recursive: true });
await build({
  entryPoints: [path.join(root, "app.js")],
  outfile: path.join(dist, "app.js"),
  bundle: true,
  external: ["./config.js"],
  format: "esm",
  target: "es2022",
  minify: true,
  sourcemap: false,
});
for (const name of ["index.html", "styles.css", "config.js"]) {
  await copyFile(path.join(root, name), path.join(dist, name));
}
