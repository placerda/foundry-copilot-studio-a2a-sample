import { mkdir, readFile, writeFile } from "node:fs/promises"
import { fileURLToPath } from "node:url"

let output = new URL("../docs/", import.meta.url)
let width = 1500
let height = 1600
const colors = { blue: "#0078d4", teal: "#008575", gray: "#7b8fa3" }
const fragments = []
const elements = []
const files = {}
let offsetX = 0
const escape = value => String(value).replace(/[&<>"]/g, char => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;",
})[char])

function element(type, x, y, w, h, extra = {}) {
  const n = elements.length + 1
  elements.push({
    id: `motors-${n}`, type, x, y, width: w, height: h, angle: 0,
    strokeColor: "#000000", backgroundColor: "transparent", fillStyle: "solid",
    strokeWidth: 1.5, strokeStyle: "solid", roughness: 0, opacity: 100,
    groupIds: [], frameId: null, roundness: null, seed: n, version: 1,
    versionNonce: n, isDeleted: false, boundElements: null, updated: 1,
    link: null, locked: false, ...extra,
  })
}

function rect(x, y, w, h, { fill = "none", stroke = "#b5cde0", dashed = false, radius = 10 } = {}) {
  x += offsetX
  fragments.push(`<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="${radius}" fill="${fill}" stroke="${stroke}" stroke-width="2"${dashed ? ' stroke-dasharray="8 7"' : ""}/>`)
  element("rectangle", x, y, w, h, {
    backgroundColor: fill === "none" ? "transparent" : fill,
    strokeColor: stroke, strokeStyle: dashed ? "dashed" : "solid",
    roundness: radius ? { type: 3 } : null,
  })
}

function text(x, y, lines, size = 23, weight = 400, maxWidth = 500) {
  x += offsetX
  const values = Array.isArray(lines) ? lines : [lines]
  fragments.push(`<text x="${x}" y="${y + size}" data-max-width="${maxWidth}" font-family="Segoe UI, Helvetica, Arial, sans-serif" font-size="${size}" font-weight="${weight}" fill="#000000">${values.map((line, i) => `<tspan x="${x}" dy="${i ? size * 1.45 : 0}">${escape(line)}</tspan>`).join("")}</text>`)
  element("text", x, y, maxWidth, values.length * size * 2.5, {
    text: values.join("\n"), originalText: values.join("\n"), fontSize: size,
    fontFamily: 2, textAlign: "left", verticalAlign: "top", containerId: null,
    autoResize: false, lineHeight: 1.45,
  })
}

async function icon(name, x, y, size = 48) {
  x += offsetX
  if (!files[name]) {
    const bytes = await readFile(new URL(`../docs/icons/${name}.svg`, import.meta.url))
    files[name] = {
      id: name, mimeType: "image/svg+xml",
      dataURL: `data:image/svg+xml;base64,${bytes.toString("base64")}`,
      created: 1, lastRetrieved: 1,
    }
  }
  fragments.push(`<image x="${x}" y="${y}" width="${size}" height="${size}" href="${files[name].dataURL}" preserveAspectRatio="xMidYMid meet"/>`)
  element("image", x, y, size, size, {
    fileId: name, status: "saved", scale: [1, 1], crop: null,
  })
}

function arrow(points, color = colors.blue, { dashed = false, both = false } = {}) {
  points = points.map(([x, y]) => [x + offsetX, y])
  const path = points.map((p, i) => `${i ? "L" : "M"} ${p.join(" ")}`).join(" ")
  fragments.push(`<path d="${path}" fill="none" stroke="${color}" stroke-width="2.5" stroke-linejoin="round"${dashed ? ' stroke-dasharray="7 7"' : ""} marker-end="url(#arrow-${color.slice(1)})"${both ? ` marker-start="url(#arrow-${color.slice(1)})"` : ""}/>`)
  const [x, y] = points[0]
  const xs = points.map(p => p[0])
  const ys = points.map(p => p[1])
  element("arrow", x, y, Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys), {
    points: points.map(p => [p[0] - x, p[1] - y]), strokeColor: color,
    strokeWidth: 2, strokeStyle: dashed ? "dashed" : "solid",
    startBinding: null, endBinding: null, startArrowhead: both ? "arrow" : null,
    endArrowhead: "arrow", elbowed: false,
  })
}

async function card(x, y, w, h, title, body, product, fill = "#ffffff") {
  rect(x, y, w, h, { fill })
  await icon(product, x + 22, y + 23, 48)
  text(x + 86, y + 22, title, 26, 600, w - 105)
  text(x + 24, y + 82, body, 22, 400, w - 48)
}

if (process.argv.includes("--guides")) {
  output = new URL("../docs/diagrams/", import.meta.url)
  await mkdir(output, { recursive: true })
  await renderGuides()
} else {
  text(60, 36, "Contoso Motors", 52, 650, 1380)
  text(60, 103, "Governed agent collaboration across Teams, Foundry and Copilot Studio", 29, 400, 1380)
  rect(40, 172, 1420, 1330, { stroke: "#a9bdcf", dashed: true, radius: 14 })

  // Reclaim the former annotation column without changing the component layout.
  offsetX = -380
  text(530, 191, "USER CHANNEL", 20, 600, 360)
  text(1000, 191, "POWER PLATFORM", 20, 600, 360)
  text(1430, 191, "LOCAL APPLICATION", 20, 600, 360)
  await card(530, 230, 360, 150, "Microsoft Teams",
    ["Approved tenant user", "Native Foundry publication"], "teams", "#f4f8fc")
  await card(1000, 230, 360, 150, "Copilot Studio",
    ["Contoso Motors supplier", "Delivery terms + citations"], "copilot-studio", "#f4f8fc")
  await card(1430, 230, 360, 150, "Contoso user",
    ["Browser + Python requester", "Certificate obtains Bearer"], "browser", "#f4f8fc")

  rect(450, 430, 1360, 1050, { stroke: colors.blue, radius: 12 })
  text(475, 446, "CONTOSO MOTORS  /  AZURE WORKLOAD", 21, 600, 800)
  rect(500, 490, 760, 290, { stroke: "#87add0" })
  await icon("foundry-project", 524, 508, 42)
  text(580, 507, "Microsoft Foundry project", 29, 600, 640)
  text(580, 548, "Private project APIs  |  Scoped public Activity exception", 20, 400, 650)
  await card(530, 595, 310, 145, "Router agent",
    ["Hosted agent", "Discover, route, validate"], "foundry-agent", "#f4f8fc")
  await card(900, 595, 310, 145, "Logistics agent",
    ["Foundry Prompt Agent", "Receiving + returnables"], "foundry", "#f4f8fc")
  await card(1380, 535, 400, 205, "API Management",
    ["Developer / External VNet", "Entry + specialist A2A routes", "Credential Manager"], "apim", "#f4f8fc")

  arrow([[710, 380], [710, 411], [462, 411], [462, 650], [530, 650]])
  text(490, 383, "Activity / HTTPS", 18, 400, 205)
  arrow([[1610, 380], [1610, 535]])
  text(1630, 451, ["Bearer", "A2A 1.0"], 20, 400, 155)
  arrow([[1495, 535], [1495, 406], [1180, 406], [1180, 380]])
  text(1280, 451, ["Delegated", "Studio A2A"], 19, 400, 135)
  arrow([[1380, 650], [1210, 650]])
  text(1265, 596, ["Managed", "identity"], 18, 400, 110)
  arrow([[685, 740], [685, 818], [1515, 818], [1515, 740]], colors.blue, { both: true })
  text(843, 783, "Entra / A2A: entry and specialist requests", 21, 400, 625)

  rect(520, 890, 1240, 275, { stroke: colors.blue })
  await icon("vnet", 544, 905, 38)
  text(598, 906, "Workload VNet", 27, 600, 400)
  text(1205, 913, "Runtime and private service connectivity", 20, 400, 530)
  rect(550, 970, 340, 155, { dashed: true })
  text(573, 987, "Hosted agent subnet", 24, 600, 295)
  text(573, 1030, ["Dedicated runtime injection", "Microsoft.App/environments"], 20, 400, 295)
  rect(920, 970, 400, 155, { dashed: true })
  await icon("private-link", 940, 992, 36)
  text(990, 990, "Private endpoint subnet", 23, 600, 310)
  text(943, 1040, ["Service attachments, not service hosts", "Foundry, registry, storage, monitoring"], 19, 400, 355)
  rect(1350, 970, 370, 155, { dashed: true })
  text(1373, 987, "APIM subnet", 24, 600, 320)
  text(1373, 1030, ["External VNet gateway", "Private backend connectivity"], 20, 400, 320)
  arrow([[610, 740], [610, 850], [850, 850], [850, 970]], colors.gray, { dashed: true })
  text(626, 862, "Runtime injection", 18, 400, 260)
  arrow([[1690, 740], [1690, 970]], colors.gray, { dashed: true })
  text(1460, 850, "Gateway VNet integration", 18, 400, 275)

  text(548, 1185, "SHARED AZURE SERVICES", 19, 600, 490)
  rect(520, 1220, 1240, 120)
  arrow([[1120, 1125], [1120, 1220]], colors.gray, { dashed: true })
  text(1140, 1175, "Private service access", 18, 400, 310)
  for (const [x, w, title, body, product] of [
    [540, 255, "Storage", "Retained shared data", "storage"],
    [815, 255, "Key Vault", "Protected credentials", "key-vault"],
    [1090, 255, "ACR", "Private image registry", "acr"],
    [1365, 375, "Azure Monitor", "AMPLS / App Insights / logs", "monitor"],
  ]) {
    await icon(product, x + 8, 1243, 43)
    text(x + 65, 1240, title, 24, 600, w - 70)
    text(x + 10, 1290, body, 18, 400, w - 20)
  }

  rect(520, 1370, 1240, 86, { fill: "#f2f8fd", stroke: "#87add0" })
  await icon("api-center", 545, 1386, 48)
  text(615, 1380, "API Center", 27, 600, 1100)
  text(615, 1418, "Capability catalog + approved A2A routes + source metadata  |  Read on every request", 21, 400, 1110)
  arrow([[530, 707], [468, 707], [468, 1415], [520, 1415]], colors.teal)

  offsetX = 0
  arrow([[65, 1540], [120, 1540]], colors.blue)
  text(138, 1523, "Authenticated messages", 20, 400, 380)
  arrow([[535, 1540], [590, 1540]], colors.teal)
  text(608, 1523, "Catalog discovery", 20, 400, 320)
  arrow([[940, 1540], [995, 1540]], colors.gray, { dashed: true })
  text(1013, 1523, "Network / private access", 20, 400, 360)

  const title = "Contoso Motors agent collaboration architecture"
  const description = "Microsoft Teams uses the native authenticated Activity route to the Foundry Hosted Router. The local Python requester calls API Management using a Bearer token obtained with a certificate. The Router discovers capabilities in API Center and calls APIM specialist routes. APIM uses shared delegated credentials for the Copilot Studio supplier and managed identity for the Foundry logistics Prompt Agent. The workload VNet contains Hosted, private endpoint and External APIM subnets. Shared Azure services appear below the network."
  await saveDiagram("contoso-motors-architecture", title, description)
}

async function saveDiagram(name, title, description) {
  const markers = Object.values(colors).map(color => `<marker id="arrow-${color.slice(1)}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="${color}"/></marker>`).join("")
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" role="img" aria-labelledby="title desc"><title id="title">${escape(title)}</title><desc id="desc">${escape(description)}</desc><defs>${markers}</defs><rect width="100%" height="100%" fill="#ffffff"/>${fragments.join("\n")}</svg>`
  await writeFile(new URL(`${name}.svg`, output), svg)
  await writeFile(new URL(`${name}.excalidraw`, output), JSON.stringify({
    type: "excalidraw", version: 2, source: name,
    elements, appState: { viewBackgroundColor: "#ffffff", gridSize: null }, files,
  }, null, 2))

  if (process.argv.includes("--png")) {
    const { chromium } = await import("@playwright/test")
    const browser = await chromium.launch({
      executablePath: process.env.ARCHITECTURE_BROWSER || undefined, headless: true,
    })
    try {
      const page = await browser.newPage({ viewport: { width, height }, deviceScaleFactor: 1.5 })
      await page.setContent(`<html><body style="margin:0">${svg}</body></html>`)
      await page.evaluate(() => document.fonts.ready)
      const overflow = await page.locator("svg text[data-max-width]").evaluateAll(nodes =>
        nodes.flatMap(node => [...node.querySelectorAll("tspan")]
          .filter(line => line.getComputedTextLength() > Number(node.dataset.maxWidth))
          .map(line => ({ text: line.textContent, actual: line.getComputedTextLength(), max: node.dataset.maxWidth }))))
      if (overflow.length) throw new Error(`Text exceeds its allocated space: ${JSON.stringify(overflow)}`)
      if (process.argv.includes("--guides")) {
        const collisions = await page.evaluate(() => {
          const svg = document.querySelector("svg")
          const { width, height } = svg.viewBox.baseVal
          const labels = [...svg.querySelectorAll("text tspan")]
            .map(node => ({ text: node.textContent, box: node.getBBox() }))
          const problems = []
          for (const [index, { text, box }] of labels.entries()) {
            if (box.x < 0 || box.y < 0 || box.x + box.width > width || box.y + box.height > height) {
              problems.push(`Outside canvas: ${text}`)
            }
            for (const other of labels.slice(index + 1)) {
              if (box.x < other.box.x + other.box.width && box.x + box.width > other.box.x
                && box.y < other.box.y + other.box.height && box.y + box.height > other.box.y) {
                problems.push(`Overlapping labels: ${text} / ${other.text}`)
              }
            }
          }
          return problems
        })
        if (collisions.length) throw new Error(`Invalid guide layout: ${JSON.stringify(collisions)}`)
      }
      const crossings = await page.evaluate(() => {
        const labels = [...document.querySelectorAll("svg > text tspan")]
          .map(node => ({ text: node.textContent, box: node.getBBox() }))
        return [...document.querySelectorAll("svg > path")].flatMap(path => {
          const coordinates = path.getAttribute("d").match(/-?\d+(?:\.\d+)?/g).map(Number)
          const hits = []
          for (let i = 2; i < coordinates.length; i += 2) {
            const [x1, y1, x2, y2] = coordinates.slice(i - 2, i + 2)
            for (const { text, box } of labels) {
              const intersects = x1 === x2
                ? x1 > box.x && x1 < box.x + box.width && Math.max(y1, y2) > box.y && Math.min(y1, y2) < box.y + box.height
                : y1 > box.y && y1 < box.y + box.height && Math.max(x1, x2) > box.x && Math.min(x1, x2) < box.x + box.width
              if (intersects) hits.push(text)
            }
          }
          return hits
        })
      })
      if (crossings.length) throw new Error(`Connectors cross text: ${JSON.stringify(crossings)}`)
      await page.locator("svg").screenshot({ path: fileURLToPath(new URL(`${name}.png`, output)) })
      console.log(`${name}: PNG rendered with ${Object.keys(files).length} embedded official icons, no text-width overflow and no connectors crossing text`)
    } finally {
      await browser.close()
    }
  }
  console.log(`${name}: SVG and editable Excalidraw saved`)
}

function startGuide(canvasHeight, title, subtitle) {
  width = 1200
  height = canvasHeight
  offsetX = 0
  fragments.length = 0
  elements.length = 0
  for (const key of Object.keys(files)) delete files[key]
  text(40, 26, title, 32, 650, 1120)
  text(40, 79, subtitle, 19, 400, 1120)
}

function section(x, y, w, h, title) {
  rect(x, y, w, h, { radius: 0, stroke: "#9cbad3" })
  rect(x, y, w, 38, { radius: 0, fill: "#e5f0f8", stroke: "#9cbad3" })
  text(x + 16, y + 6, title, 19, 600, w - 32)
}

async function guideCard(x, y, w, h, title, body, product) {
  rect(x, y, w, h, { fill: "#ffffff", radius: 0 })
  if (product) await icon(product, x + 18, y + 19, 40)
  text(x + (product ? 74 : 20), y + 17, title, 22, 600, w - (product ? 92 : 40))
  text(x + 20, y + 88, body, 19, 400, w - 40)
}

function note(x, y, w, title, lines) {
  rect(x, y, w, 126, { fill: "#f3f8fc", radius: 0, stroke: "#b5cde0" })
  text(x + 20, y + 14, title, 21, 600, w - 40)
  text(x + 20, y + 54, lines, 19, 400, w - 40)
}

async function renderGuides() {
  startGuide(850, "Two identities, two different tokens",
    "The user signs in. The Python requester authenticates with its own certificate.")
  section(40, 132, 1120, 232, "USER IDENTITY  /  Browser to local Python API")
  await guideCard(70, 192, 340, 148, "Signed-in user",
    ["Microsoft sign-in in the browser", "Delegated token for the local API"], "users")
  await guideCard(670, 192, 460, 148, "Local Python API",
    ["Validate the user token and approved user", "Do not forward this token to APIM"], "browser")
  arrow([[410, 266], [670, 266]])
  text(450, 205, ["User token", "Local API audience"], 18, 400, 190)
  section(40, 394, 1120, 364, "APPLICATION IDENTITY  /  OAuth 2.0 Client Credentials with a certificate")
  await guideCard(70, 468, 280, 172, ["Python", "requester"],
    ["Private key in Windows", "Credential Manager"], "app-registration")
  await guideCard(460, 468, 280, 172, ["Microsoft", "Entra ID"],
    ["Checks the registered cert", "Issues an application token"], "entra-id")
  await guideCard(850, 468, 280, 172, "APIM",
    ["Validates the token and role", "CONTOSO.Invoke"], "apim")
  arrow([[350, 520], [460, 520]])
  text(361, 472, ["Signed", "assertion"], 16, 400, 90)
  arrow([[460, 610], [350, 610]], colors.teal)
  text(361, 620, ["Access", "token"], 16, 400, 90)
  arrow([[210, 640], [210, 718], [990, 718], [990, 640]])
  text(465, 676, "Bearer token in the HTTPS request", 20, 600, 450)
  text(40, 785, "The certificate proves identity to Entra. APIM receives a token, not a client certificate. No mTLS.", 19, 400, 1120)
  await saveDiagram("client-certificate-auth", "Contoso Motors client certificate authentication",
    "User sign-in and application authentication are separate. The browser sends a delegated local API token to Python. The Python requester signs a client assertion with its protected private key. Entra validates the registered certificate and returns a Gateway API application token. Python sends that Bearer token to APIM. The certificate is not used for mutual TLS with APIM.")

  startGuide(700, "Same A2A request, different credentials",
    "The Router authenticates as an application. Copilot Studio sees the shared connection owner.")
  section(430, 138, 340, 476, "API MANAGEMENT")
  await guideCard(50, 200, 300, 180, ["Foundry", "Hosted Router"],
    ["Gateway API application token", "A2A 1.0 / SendMessage"], "foundry-agent")
  await guideCard(460, 200, 280, 180, ["APIM", "policy"],
    ["Validate Router and role", "Replace Authorization"], "apim")
  await guideCard(850, 200, 300, 180, ["Copilot Studio", "supplier"],
    ["Shared user's delegated token", "Contoso Motors Supplier"], "copilot-studio")
  arrow([[350, 290], [460, 290]])
  text(362, 242, ["Gateway", "token"], 16, 400, 86)
  arrow([[740, 290], [850, 290]], colors.teal)
  text(752, 242, ["Studio", "token"], 16, 400, 86)
  await guideCard(460, 448, 280, 146, ["Credential", "Manager"],
    ["Shared user connection", "Owner's delegated identity"], "apim")
  arrow([[600, 448], [600, 380]], colors.teal)
  text(620, 399, "Get token", 17, 400, 120)
  note(50, 448, 300, "The message is still A2A",
    ["Only the outbound credential", "changes at this boundary."])
  note(850, 448, 300, "Not the browser user",
    ["This is shared delegation,", "not end-user OBO forwarding."])
  text(40, 644, "The Studio token never returns to the Router or browser. Logistics uses APIM managed identity instead.", 19, 400, 1120)
  await saveDiagram("studio-credential-bridge", "Contoso Motors APIM credential bridge to Copilot Studio",
    "The Foundry Hosted Router sends A2A with a Gateway API application token. APIM validates the Router, obtains a delegated Studio token from its shared authorized Credential Manager connection, and replaces the outbound Authorization header. Copilot Studio executes as the shared connection owner, not the browser user. The Studio credential is never returned to the Router or browser.")

  startGuide(850, "Three routes, three backend paths",
    "APIM holds API routes and policies. Agent prompts and models stay in Foundry and Copilot Studio.")
  section(430, 140, 340, 600, "API MANAGEMENT")
  text(50, 152, "CALLER", 17, 600, 300)
  text(850, 152, "BACKEND AND CREDENTIAL", 17, 600, 300)
  await guideCard(50, 208, 300, 146, ["Python", "requester"],
    ["Gateway API application token", "Role: CONTOSO.Invoke"], "app-registration")
  await guideCard(460, 208, 280, 146, "Entry API",
    ["/api/a2a", "A2A message in"], null)
  await guideCard(850, 208, 300, 146, ["Foundry", "Hosted Router"],
    ["Native invocations endpoint", "APIM managed identity"], "foundry-agent")
  arrow([[350, 281], [460, 281]])
  arrow([[740, 281], [850, 281]])
  await guideCard(50, 466, 300, 170, ["Same Hosted", "Router"],
    ["Gateway API application token", "CONTOSO.Connection.Use"], "foundry-agent")
  await guideCard(460, 390, 280, 146, "Supplier API",
    ["/agents/.../versions/1/a2a", "Studio specialist route"], null)
  await guideCard(850, 390, 300, 146, ["Copilot Studio", "supplier"],
    ["Studio A2A channel", "Shared delegated connection"], "copilot-studio")
  await guideCard(460, 570, 280, 146, "Logistics API",
    ["/agents/.../versions/1/a2a", "Logistics specialist route"], null)
  await guideCard(850, 570, 300, 146, ["Foundry", "logistics agent"],
    ["Foundry A2A endpoint", "APIM managed identity"], "foundry")
  arrow([[350, 550], [390, 550], [390, 463], [460, 463]])
  arrow([[390, 550], [390, 643], [460, 643]])
  arrow([[740, 463], [850, 463]], colors.teal)
  arrow([[740, 643], [850, 643]])
  text(40, 784, "The Router is the entry API's backend, then the caller of a specialist API. Full paths are in the table below.", 19, 400, 1120)
  await saveDiagram("apim-agent-routes", "Contoso Motors APIM entry and specialist routes",
    "The certificate requester calls the APIM entry API, which invokes the Hosted Router with APIM managed identity. The same Router calls either the supplier or logistics specialist API using its Gateway API application role. APIM forwards to Studio with the shared delegated connection, or to the Foundry logistics A2A endpoint with its managed identity. APIM does not host agent prompts.")

  startGuide(670, "Discover in API Center. Invoke through APIM.",
    "The Router reads the catalog for every admitted request, then calls the matching approved route.")
  await guideCard(50, 186, 320, 180, ["Foundry", "Hosted Router"],
    ["Read and validate metadata", "Select a registered capability"], "foundry-agent")
  await guideCard(820, 186, 330, 180, "API Center",
    ["Capability and APIM endpoint", "Version and source metadata"], "api-center")
  arrow([[370, 242], [820, 242]], colors.teal)
  text(466, 205, "1  Read catalog with Router identity", 18, 600, 345)
  arrow([[820, 330], [370, 330]], colors.teal)
  text(420, 286, "2  Return route and source metadata", 18, 600, 370)
  await guideCard(820, 436, 330, 150, "APIM",
    ["Authenticate and apply policy", "Call the selected specialist"], "apim")
  arrow([[210, 366], [210, 510], [820, 510]])
  text(390, 450, ["3  Invoke the approved A2A route", "Use a Gateway API application token"], 19, 600, 410)
  text(40, 618, "API Center returns metadata. It does not proxy business requests, create APIs or grant permissions.", 19, 400, 1120)
  await saveDiagram("catalog-discovery", "Contoso Motors API Center discovery versus APIM invocation",
    "On each admitted request the Router uses its identity to read API Center. API Center returns capability, APIM endpoint, version and source metadata. The Router validates the metadata and invokes the matching approved APIM A2A route with a Gateway API application token. The catalog does not proxy the business request or create permissions.")

  startGuide(730, "From identity probe to business routing",
    "First deployment: learn the real platform caller before admitting business requests.")
  await guideCard(50, 164, 300, 180, ["1  Bootstrap", "Router"],
    ["Deploy identity-probe mode", "Keep Entra authorization"], "foundry-agent")
  await guideCard(450, 164, 300, 180, ["2  Configure", "APIM ingress"],
    ["Certificate requester to APIM", "APIM identity to Router"], "apim")
  await guideCard(850, 164, 300, 180, ["3  Send exact", "probe"],
    ["Invoke through APIM", "CONTOSO identity probe"], "app-registration")
  arrow([[350, 254], [450, 254]])
  arrow([[750, 254], [850, 254]])
  arrow([[1000, 344], [1000, 458]])
  await guideCard(850, 458, 300, 180, ["4  Read verified", "caller"],
    ["SDK platform_caller_id", "Not an assumed principal ID"], "foundry-agent")
  await guideCard(450, 458, 300, 180, ["5  Publish", "business version"],
    ["Set caller, catalog and APIM", "Preserve image and protocols"], "foundry-agent")
  await guideCard(50, 458, 300, 180, ["6  Select and", "verify"],
    ["Wait until the version is active", "Select 100% and read back"], "foundry")
  arrow([[850, 548], [750, 548]])
  arrow([[450, 548], [350, 548]])
  text(40, 676, "Use the requester-to-APIM path. A direct operator call to Foundry identifies a different caller.", 19, 400, 1120)
  await saveDiagram("router-bootstrap", "Contoso Motors first Router deployment and platform caller admission",
    "Create an Entra-protected Router in identity-probe mode. Configure APIM ingress. Invoke the exact diagnostic message through APIM as the certificate requester and read the SDK-verified platform caller ID. Create a business version with that caller, catalog and APIM settings while retaining the image and protocols. Wait for active state, select the version at 100 percent, and verify selection. Direct operator invocation identifies a different caller.")
}
