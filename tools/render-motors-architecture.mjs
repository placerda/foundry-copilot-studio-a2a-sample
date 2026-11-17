import { readFile, writeFile } from "node:fs/promises"
import { fileURLToPath } from "node:url"

const output = new URL("../docs/", import.meta.url)
const width = 1500
const height = 1600
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
    const bytes = await readFile(new URL(`icons/${name}.svg`, output))
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
const markers = Object.values(colors).map(color => `<marker id="arrow-${color.slice(1)}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="${color}"/></marker>`).join("")
const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" role="img" aria-labelledby="title desc"><title id="title">${escape(title)}</title><desc id="desc">${escape(description)}</desc><defs>${markers}</defs><rect width="100%" height="100%" fill="#ffffff"/>${fragments.join("\n")}</svg>`
const name = "contoso-motors-architecture"
await writeFile(new URL(`${name}.svg`, output), svg)
await writeFile(new URL(`${name}.excalidraw`, output), JSON.stringify({
  type: "excalidraw", version: 2, source: "contoso-motors-architecture",
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
