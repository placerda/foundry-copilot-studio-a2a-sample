import http from "node:http";
import { readFile } from "node:fs/promises";
import { resolve, sep } from "node:path";
import { build } from "esbuild";

const root = resolve("frontend");
const testConfig = {
  TENANT_ID: "11111111-1111-1111-1111-111111111111",
  SPA_CLIENT_ID: "22222222-2222-2222-2222-222222222222",
  API_SCOPE: "api://33333333-3333-3333-3333-333333333333/access_as_user",
  FUNCTION_URL: "https://synthetic.lambda-url.us-east-2.on.aws/",
  REDIRECT_URI: "http://localhost:8000/",
};
const bundled = await build({
  entryPoints: [resolve(root, "app.js")], bundle: true, format: "esm", write: false,
  plugins: [{
    name: "offline-auth",
    setup(builder) {
      builder.onResolve({ filter: /^@azure\/msal-browser$/ }, () => ({
        path: resolve(root, "tests", "fixtures.js"),
      }));
      builder.onResolve({ filter: /^\.\/config\.js$/ }, () => ({
        path: "synthetic-config", namespace: "fixture",
      }));
      builder.onLoad({ filter: /.*/, namespace: "fixture" }, () => ({
        contents: `export default { ...${JSON.stringify(testConfig)},
          CONTOSO_ENABLED: Object.hasOwn(window, "contosoTestEnabled") ? window.contosoTestEnabled : false }`, loader: "js",
      }));
    },
  }],
});
http.createServer(async (request, response) => {
  const name = new URL(request.url, "http://localhost").pathname;
  if (name === "/app.js") {
    response.setHeader("Content-Type", "text/javascript");
    response.end(bundled.outputFiles[0].text);
    return;
  }
  const file = resolve(root, "." + (name === "/" ? "/index.html" : name));
  if (!file.startsWith(root + sep)) {
    response.writeHead(403).end();
    return;
  }
  try {
    const content = await readFile(file);
    response.setHeader("Content-Type", file.endsWith(".css") ? "text/css" : "text/html");
    response.end(content);
  } catch {
    response.writeHead(404).end();
  }
}).listen(Number(process.env.CONTOSO_TEST_PORT || 8000), "127.0.0.1");
