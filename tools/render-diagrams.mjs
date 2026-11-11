import { mkdir, readFile, writeFile } from "node:fs/promises"
import { fileURLToPath } from "node:url"

const output = new URL("../docs/", import.meta.url)
const palette = { ink: "#000000", blue: "#0078d4", private: "#008575", admin: "#64748b" }
const escape = value => String(value).replace(/[&<>"]/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[char])
const icons = {}
for (const name of ["foundry", "copilot-studio", "container-apps", "apim", "key-vault", "acr", "monitor", "private-link", "dns", "vpn"]) {
  const data = await readFile(new URL(`icons/${name}.svg`, output))
  icons[name] = `data:image/svg+xml;base64,${data.toString("base64")}`
}

function drawing(width, height) {
  const fragments = []
  const elements = []
  const files = {}
  const connectors = []
  function element(type, x, y, w, h, extra = {}) {
    const value = {
      id: `element-${elements.length + 1}`, type, x, y, width: w, height: h,
      angle: 0, strokeColor: palette.ink, backgroundColor: "transparent",
      fillStyle: "solid", strokeWidth: 1.5, strokeStyle: "solid", roughness: 0,
      opacity: 100, groupIds: [], frameId: null, roundness: null, seed: elements.length + 1,
      version: 1, versionNonce: elements.length + 1, isDeleted: false,
      boundElements: null, updated: 1, link: null, locked: false, ...extra,
    }
    elements.push(value)
    return value
  }
  function rect(x, y, w, h, fill = "none", stroke = "#b7c9df", dashed = false) {
    fragments.push(`<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="12" fill="${fill}" stroke="${stroke}" stroke-width="2"${dashed ? ' stroke-dasharray="8 6"' : ""}/>`)
    element("rectangle", x, y, w, h, {
      backgroundColor: fill === "none" ? "transparent" : fill,
      strokeColor: stroke, strokeStyle: dashed ? "dashed" : "solid", roundness: { type: 3 },
    })
  }
  function text(x, y, lines, size = 22, weight = 400, w = 350, color = palette.ink) {
    const values = Array.isArray(lines) ? lines : [lines]
    fragments.push(`<text x="${x}" y="${y + size}" data-max-width="${w}" font-family="Segoe UI, Helvetica, Arial, sans-serif" font-size="${size}" font-weight="${weight}" fill="${color}">${values.map((line, i) => `<tspan x="${x}" dy="${i ? size * 1.35 : 0}">${escape(line)}</tspan>`).join("")}</text>`)
    element("text", x, y, w, values.length * size * 2.5, {
      text: values.join("\n"), originalText: values.join("\n"), fontSize: size, fontFamily: 2,
      textAlign: "left", verticalAlign: "top", containerId: null,
      autoResize: false, lineHeight: 1.35, strokeColor: color,
    })
  }
  function icon(name, x, y, size = 52) {
    fragments.push(`<image x="${x}" y="${y}" width="${size}" height="${size}" href="${icons[name]}" preserveAspectRatio="xMidYMid meet"/>`)
    files[name] = { id: name, mimeType: "image/svg+xml", dataURL: icons[name], created: 1, lastRetrieved: 1 }
    element("image", x, y, size, size, { fileId: name, status: "saved", scale: [1, 1], crop: null })
  }
  function number(value, x, y) {
    fragments.push(`<circle cx="${x}" cy="${y}" r="23" fill="#ffffff" stroke="${palette.blue}" stroke-width="3"/>`)
    element("ellipse", x - 23, y - 23, 46, 46, { backgroundColor: "#ffffff", strokeColor: palette.blue })
    fragments.push(`<text x="${x}" y="${y + 8}" font-family="Segoe UI, Helvetica, Arial, sans-serif" font-size="24" font-weight="600" text-anchor="middle" fill="${palette.ink}">${value}</text>`)
    element("text", x - 12, y - 15, 24, 60, {
      text: String(value), originalText: String(value), fontSize: 24, fontFamily: 2,
      textAlign: "center", verticalAlign: "top", containerId: null,
      autoResize: false, lineHeight: 1.35, strokeColor: palette.ink,
    })
  }
  function box(x, y, w, h, title, body, product, step, fill = "#ffffff") {
    rect(x, y, w, h, fill)
    const inset = product ? 85 : 24
    if (product) icon(product, x + 19, y + 19)
    text(x + inset, y + 25, title, 24, 600, w - inset - 18)
    text(x + 24, y + 85, body, 21, 400, w - 48)
    if (step) number(step, x + w - 28, y - 4)
  }
  function arrow(points, color = palette.blue, dashed = false, head = true) {
    connectors.push(points)
    const path = points.map((point, i) => `${i ? "L" : "M"} ${point.join(" ")}`).join(" ")
    fragments.push(`<path d="${path}" fill="none" stroke="${color}" stroke-width="3"${dashed ? ' stroke-dasharray="8 7"' : ""}${head ? ` marker-end="url(#arrow-${color.slice(1)})"` : ""}/>`)
    const [x, y] = points[0]
    const xs = points.map(point => point[0])
    const ys = points.map(point => point[1])
    element(head ? "arrow" : "line", x, y, Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys), {
      points: points.map(point => [point[0] - x, point[1] - y]), strokeColor: color,
      strokeWidth: 2, strokeStyle: dashed ? "dashed" : "solid",
      startBinding: null, endBinding: null, startArrowhead: null,
      endArrowhead: head ? "arrow" : null, elbowed: false,
    })
  }
  function header(title, subtitle) {
    text(60, 38, title, 44, 600, width - 120)
    text(60, 103, subtitle, 24, 400, width - 120)
  }
  function validateConnectors() {
    const same = (a, b) => a[0] === b[0] && a[1] === b[1]
    const segments = connectors.flatMap((points, id) =>
      points.slice(1).map((end, i) => ({ a: points[i], b: end, id }))
    )
    for (let i = 0; i < segments.length; i++) {
      const first = segments[i]
      for (const second of segments.slice(i + 1)) {
        if (first.id === second.id) continue
        const horizontal = segment => segment.a[1] === segment.b[1]
        if (horizontal(first) === horizontal(second)) {
          const axis = horizontal(first) ? 0 : 1
          const other = 1 - axis
          if (first.a[other] !== second.a[other]) continue
          const lo = Math.max(Math.min(first.a[axis], first.b[axis]), Math.min(second.a[axis], second.b[axis]))
          const hi = Math.min(Math.max(first.a[axis], first.b[axis]), Math.max(second.a[axis], second.b[axis]))
          if (lo < hi) throw new Error("Overlapping connectors")
          continue
        }
        const h = horizontal(first) ? first : second
        const v = horizontal(first) ? second : first
        const point = [v.a[0], h.a[1]]
        const between = (n, a, b) => n >= Math.min(a, b) && n <= Math.max(a, b)
        if (between(point[0], h.a[0], h.b[0]) && between(point[1], v.a[1], v.b[1])) {
          if ([first.a, first.b].some(p => same(p, point)) && [second.a, second.b].some(p => same(p, point))) continue
          throw new Error(`Crossing connectors at ${point}`)
        }
      }
    }
  }
  async function save(name, title, description) {
    validateConnectors()
    const markers = [...new Set(Object.values(palette))].map(color =>
      `<marker id="arrow-${color.slice(1)}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="${color}"/></marker>`
    ).join("")
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" role="img" aria-labelledby="title desc"><title id="title">${escape(title)}</title><desc id="desc">${escape(description)}</desc><defs>${markers}</defs><rect width="100%" height="100%" fill="#ffffff"/>${fragments.join("\n")}</svg>`
    await mkdir(output, { recursive: true })
    await writeFile(new URL(`${name}.svg`, output), svg)
    await writeFile(new URL(`${name}.excalidraw`, output), JSON.stringify({
      type: "excalidraw", version: 2, source: "local-reference-architecture",
      elements, appState: { viewBackgroundColor: "#ffffff", gridSize: null }, files,
    }, null, 2))
    if (process.argv.includes("--png")) {
      const { chromium } = await import("@playwright/test")
      const browser = await chromium.launch({ executablePath: process.env.ARCHITECTURE_BROWSER || undefined, headless: true })
      try {
        const page = await browser.newPage({ viewport: { width, height }, deviceScaleFactor: 1 })
        await page.setContent(`<html><body style="margin:0">${svg}</body></html>`)
        await page.locator("svg").screenshot({ path: fileURLToPath(new URL(`${name}.png`, output)) })
      } finally {
        await browser.close()
      }
    }
    console.log(`${name}: saved editable diagram, checked all connectors for crossings`)
  }
  return { rect, text, icon, number, box, arrow, header, save }
}

const overview = drawing(2200, 850)
overview.header("Supplier–Buyer Agent Collaboration", "Run the app on your own computer  |  Container Apps hosting is optional")
overview.text(60, 239, "YOUR COMPUTER  /  LOCAL APP", 20, 600, 500)
overview.rect(570, 220, 1110, 375, "none", "#a6c5e8")
overview.text(600, 239, "AZURE  /  APIM INTERNAL TARGET", 20, 600, 1000)
overview.rect(1810, 220, 360, 375, "none", "#d4c3e5")
overview.text(1835, 239, "POWER PLATFORM", 20, 600, 300)
overview.box(60, 320, 340, 230, "Local application", ["Chat UI + Python requester", "Runs at localhost:8000", "Optional: Container Apps"], null, 1)
overview.box(610, 320, 370, 230, "API Management", ["Developer / Internal VNet", "Private HTTPS ingress", "Credential Manager for OAuth"], "apim", 2)
overview.box(1210, 320, 410, 230, "Microsoft Foundry", ["Private Hosted agent", "Explicit capability routing", "Preserves source attribution"], "foundry", 3)
overview.box(1830, 320, 320, 230, "Copilot Studio", ["Published agent", "Native A2A endpoint", "Approved delegated token"], "copilot-studio", 4)
overview.arrow([[400, 435], [610, 435]], palette.private)
overview.text(420, 376, ["Private HTTPS", "A2A 1.0"], 20, 400, 180)
overview.arrow([[980, 435], [1210, 435]], palette.private)
overview.text(1005, 376, ["Private Link", "HTTPS"], 20, 400, 185)
overview.arrow([[1620, 435], [1830, 435]])
overview.text(1665, 376, ["Public HTTPS", "A2A"], 20, 400, 180)
overview.text(60, 632, "THE PATTERN", 19, 600, 1000)
overview.text(60, 684, ["Microsoft Entra authorization", "Separate identity at each boundary"], 21, 400, 670)
overview.icon("private-link", 770, 695, 42)
overview.text(830, 684, ["Private Azure service access", "Existing corporate network + DNS"], 21, 400, 620)
overview.icon("copilot-studio", 1500, 695, 42)
overview.text(1560, 684, ["Fictional business scenario", "Insight + citations, or explicit no-match"], 21, 400, 620)
overview.text(60, 790, "APIM Internal and Credential Manager are the target. Optional Container Apps hosting moves the same UI + backend to Azure.", 20, 400, 2080)
await overview.save("architecture-overview", "Supplier–Buyer Agent Collaboration overview", "The browser and Python server run on your own computer at localhost:8000. Hosting the same application in Azure Container Apps is optional. APIM Internal and Credential Manager are the proposed gateway target, followed by private Foundry Hosted and delegated Copilot Studio A2A.")

const detail = drawing(2360, 1890)
detail.header("Supplier–Buyer Agent Collaboration", "Run the app on your own computer  |  Container Apps hosting is optional")
detail.rect(45, 190, 540, 545, "none", "#bdc8d6")
detail.text(75, 210, "YOUR COMPUTER  /  LOCAL APP", 20, 600, 460)
detail.box(80, 275, 440, 155, "Local browser", ["Open http://localhost:8000/", "Sign in with Microsoft"], null, 1)
detail.box(80, 490, 440, 165, "Local Python server", ["Serves the chat UI + /api/chat", "Runs on your own computer", "Verifies guest, calls APIM"], null)
detail.arrow([[300, 430], [300, 490]], palette.admin)
detail.text(320, 436, ["Loopback HTTP / JSON", "localhost:8000"], 18, 400, 240)
detail.text(75, 674, ["Optional cloud hosting: Container Apps", "Move this same UI + backend to Azure"], 18, 400, 480)

detail.rect(665, 190, 1300, 930, "none", "#a6c5e8")
detail.text(695, 210, "AZURE  /  APIM INTERNAL TARGET", 20, 600, 1200)
detail.box(700, 275, 340, 155, "APIM gateway", ["Private HTTPS + Entra JWT", "Credential Manager for OAuth"], "apim", 2)
detail.box(1420, 275, 420, 155, "Foundry project API", ["Public network disabled", "Private project / agent endpoint"], "foundry")
detail.arrow([[520, 570], [620, 570], [620, 350], [700, 350]], palette.private)
detail.text(530, 430, ["Private", "HTTPS", "A2A 1.0"], 18, 400, 90)

detail.rect(680, 465, 1265, 300, "none", "#7aaed6", true)
detail.text(700, 477, "VNET  /  ISOLATED SUBNETS", 19, 600, 1100)
detail.box(700, 530, 340, 180, "APIM subnet", ["Dedicated, no delegation", "Developer / Internal VNet", "APIM managed identity"], null, null, "#eaf4ff")
detail.box(1100, 530, 240, 180, "Foundry PE", ["Shared PE subnet", "account group", "Private HTTPS"], "private-link", null, "#edf7f6")
detail.box(1420, 530, 420, 180, "Foundry Hosted agent", ["Dedicated injected subnet", "Explicit platform caller gate", "Native Studio A2A provider"], "foundry", 3, "#eaf4ff")
detail.arrow([[870, 430], [870, 530]], palette.private)
detail.text(890, 436, "VNet injection", 18, 400, 200)
detail.arrow([[1040, 620], [1100, 620]], palette.private)
detail.arrow([[1220, 530], [1220, 350], [1420, 350]], palette.private)
detail.text(1240, 385, ["Private HTTPS", "Entra bearer"], 18, 400, 165)
detail.arrow([[1630, 430], [1630, 530]], palette.private)
detail.text(1650, 445, "Platform dispatch", 18, 400, 215)

detail.rect(2010, 465, 310, 300, "none", "#d4c3e5")
detail.text(2030, 477, "POWER PLATFORM", 18, 600, 280)
detail.box(2030, 530, 270, 180, "Copilot Studio", ["Published A2A agent", "Public SaaS endpoint", "MCS.InvokeAsA2A"], "copilot-studio", 4)
detail.arrow([[1840, 620], [2030, 620]])
detail.text(1860, 550, ["Public HTTPS", "A2A"], 18, 400, 165)

detail.rect(45, 805, 540, 315, "none", "#bdc8d6")
detail.icon("dns", 75, 835)
detail.text(150, 837, "Private DNS / VNet-linked", 25, 600, 405)
detail.text(75, 915, [
  "Foundry / OpenAI / services.ai zones",
  "ACR registry and data endpoint zone",
  "Container Apps + Key Vault zones",
  "Private names resolve inside the VNet",
  "Existing corporate VPN / ExpressRoute",
  "Reuse private DNS and network access",
], 21, 400, 480)

detail.text(700, 803, "PRIVATE SERVICE ACCESS  /  CONTAINER APPS HOSTING IS OPTIONAL", 19, 600, 1220)
detail.box(700, 855, 280, 260, "Container Apps", [
  "Optional cloud host",
  "Same UI + backend",
  "Internal environment",
  "Dedicated VNet subnet",
  "No additional PE",
], "container-apps")
const services = [
  { x: 1020, pe: "Key Vault PE", title: "Key Vault", icon: "key-vault", body: ["Keys + approved secrets", "Managed identity"] },
  { x: 1340, pe: "Registry PE", title: "ACR Premium", icon: "acr", body: ["Private image registry", "Agent AcrPull + digest"] },
  { x: 1660, pe: "Monitor PE", title: "Azure Monitor", icon: "monitor", body: ["AMPLS + App Insights", "Private Log Analytics"] },
]
for (const service of services) {
  detail.rect(service.x, 855, 280, 65, "#edf7f6", "#8ec2ba")
  detail.icon("private-link", service.x + 18, 867, 40)
  detail.text(service.x + 75, 874, service.pe, 22, 600, 195)
  detail.arrow([[service.x + 140, 920], [service.x + 140, 970]], palette.private, true)
  detail.box(service.x, 970, 280, 145, service.title, service.body, service.icon)
}

detail.rect(45, 1170, 2275, 535, "none", "#bdc8d6", true)
detail.text(75, 1190, "OPERATOR AND DEPLOYMENT  /  SEPARATE FROM THE RUNTIME REQUEST", 20, 600, 2190)
detail.box(80, 1260, 500, 160, "Authorized operator", ["Separate from the business route", "Approved private execution host"], null)
detail.box(800, 1260, 500, 160, "Private build / deployment runner", ["Build and publish from private network", "APIM does not run publication jobs"], null)
detail.box(1520, 1260, 500, 160, "Private Azure services", ["Foundry publication + private ACR", "Explicit operator / workload access"], "foundry")
detail.arrow([[580, 1340], [800, 1340]], palette.admin, true)
detail.text(605, 1290, "Operator-only", 19, 400, 190)
detail.arrow([[1300, 1340], [1520, 1340]], palette.private, true)
detail.text(1310, 1290, "Private HTTPS", 19, 400, 200)
detail.box(80, 1470, 500, 150, "Optional direct operator path", ["Private diagnostics only", "Not required for the main runtime"], null)
detail.box(800, 1470, 500, 150, "Existing VPN / ExpressRoute", ["Reuse corporate connectivity", "Private DNS and route controls"], "vpn")
detail.box(1520, 1470, 500, 150, "Private endpoints + DNS", ["Scoped private DNS for the operator", "Direct endpoint access when authorized"], "private-link")
detail.arrow([[580, 1545], [800, 1545]], palette.admin, true)
detail.arrow([[1300, 1545], [1520, 1545]], palette.admin, true)
detail.text(75, 1655, "APIM Internal / broker cutover and optional Container Apps hosting require acceptance. Existing resources remain unchanged.", 19, 400, 2190)

detail.text(60, 1750, "CONNECTIVITY LEGEND", 18, 600, 500)
for (const [x, label, color, dashed] of [
  [60, "Local HTTP / JSON", palette.admin, false],
  [620, "Public HTTPS / A2A", palette.blue, false],
  [1180, "Private cloud traffic", palette.private, false],
  [1740, "Operator only", palette.admin, true],
]) {
  detail.arrow([[x, 1805], [x + 80, 1805]], color, dashed)
  detail.text(x + 100, 1789, label, 21, 400, 450)
}
detail.text(60, 1840, "Logical product labels only. Responses return through the same chain. Azure private access does not make Copilot Studio or Entra private.", 20, 400, 2240)
await detail.save("architecture", "Supplier–Buyer Agent Collaboration connectivity", "Default: local browser and Python server on your own computer at localhost:8000. Optional: host the same UI and backend in Azure Container Apps, with an internal workload profiles environment and dedicated VNet-integration subnet, without an additional private endpoint. APIM Internal with Credential Manager, private Foundry and Studio A2A are the proposed cloud topology, not completed migration evidence. The separate token-exchange panel is omitted for readability.")
