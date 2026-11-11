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

const detail = drawing(2360, 1680)
detail.header("Supplier–Buyer Agent Collaboration", "Run the app on your own computer  |  Container Apps hosting is optional")
detail.rect(45, 190, 540, 555, "none", "#bdc8d6")
detail.text(75, 210, "YOUR COMPUTER  /  LOCAL APP", 20, 600, 460)
detail.box(80, 275, 440, 155, "Local browser", ["Open http://localhost:8000/", "Sign in with Microsoft"], null, 1)
detail.box(80, 490, 440, 175, "Local Python server", ["Serves the chat UI + /api/chat", "Runs on your own computer", "Verifies guest, calls APIM"], null)
detail.arrow([[300, 430], [300, 490]], palette.admin)
detail.text(320, 436, ["Loopback HTTP / JSON", "localhost:8000"], 18, 400, 240)
detail.text(75, 684, ["Optional cloud hosting: Container Apps", "Move this same UI + backend to Azure"], 18, 400, 480)

detail.rect(665, 190, 1300, 1300, "none", "#a6c5e8")
detail.text(695, 210, "AZURE  /  APIM INTERNAL TARGET", 20, 600, 1200)
detail.text(700, 280, "SAME REQUEST PATH  /  TWO HOSTING CHOICES", 19, 600, 650)
detail.text(700, 340, ["Local: Python requester on your computer", "Optional: requester in the Container Apps subnet"], 20, 400, 650)
detail.box(1420, 275, 420, 155, "Foundry project API", ["Public network disabled", "Private project / agent endpoint"], "foundry")

detail.rect(680, 465, 1265, 980, "none", "#7aaed6", true)
detail.text(700, 477, "VNET  /  THREE REQUIRED SUBNETS + ONE OPTIONAL CONTAINER APPS SUBNET", 18, 600, 1225)
detail.rect(700, 530, 340, 280, "none", "#7aaed6", true)
detail.text(720, 545, "APIM SUBNET", 17, 600, 300)
detail.text(720, 575, "Dedicated / no delegation", 16, 400, 300)
detail.box(720, 610, 300, 175, "APIM gateway", ["Entra JWT + private HTTPS", "Credential Manager", "Developer / Internal VNet"], "apim", 2)

detail.rect(1420, 530, 420, 280, "none", "#7aaed6", true)
detail.text(1440, 545, "FOUNDRY HOSTED SUBNET", 17, 600, 380)
detail.text(1440, 575, "Dedicated runtime injection", 16, 400, 380)
detail.box(1440, 610, 380, 175, "Foundry Hosted agent", ["Explicit platform caller gate", "Native Studio A2A provider", "Approved delegated identity"], "foundry", 3)

detail.rect(1090, 530, 280, 840, "none", "#8ec2ba", true)
detail.text(1110, 545, "SHARED PE SUBNET", 17, 600, 240)
detail.text(1110, 575, "4 endpoints / no delegation", 16, 400, 240)
for (const endpoint of [
  { y: 650, title: "Foundry PE", note: "account group" },
  { y: 900, title: "Key Vault PE", note: "Protected secrets" },
  { y: 1050, title: "Registry PE", note: "Registry + data" },
  { y: 1200, title: "Monitor PE", note: "AMPLS" },
]) {
  detail.rect(1110, endpoint.y, 240, 70, "#edf7f6", "#8ec2ba")
  detail.icon("private-link", 1126, endpoint.y + 14, 40)
  detail.text(1172, endpoint.y + 10, endpoint.title, 20, 600, 168)
  detail.text(1172, endpoint.y + 39, endpoint.note, 16, 400, 168)
}
detail.text(1110, 1330, "Endpoints, not service hosts", 16, 400, 240)

detail.rect(700, 900, 340, 360, "none", "#7aaed6", true)
detail.text(720, 915, "CONTAINER APPS SUBNET", 17, 600, 300)
detail.text(720, 945, "Optional / workload profiles / min /27", 16, 400, 300)
detail.box(720, 990, 300, 185, "Container Apps", ["UI + Python backend", "Internal environment", "Private HTTPS ingress"], "container-apps")
detail.text(720, 1200, "Microsoft.App/environments", 16, 400, 300)
detail.text(720, 1225, "Egress: APIM + private services", 17, 400, 300)

detail.arrow([[520, 570], [600, 570], [600, 680], [720, 680]], palette.private)
detail.text(610, 590, ["Private", "HTTPS"], 17, 400, 85)
detail.arrow([[1020, 680], [1110, 680]], palette.private)
detail.arrow([[1350, 685], [1395, 685], [1395, 350], [1420, 350]], palette.private)
detail.text(1240, 385, ["Private HTTPS", "Entra bearer"], 18, 400, 165)
detail.arrow([[1840, 350], [1935, 350], [1935, 690], [1820, 690]], palette.private)
detail.text(1850, 385, ["Platform", "dispatch"], 18, 400, 140)
detail.arrow([[1020, 1050], [1065, 1050], [1065, 710], [1020, 710]], palette.private)
detail.text(720, 830, ["ACA outbound to APIM", "Private HTTPS over the VNet"], 17, 400, 320)
detail.arrow([[1020, 1130], [1080, 1130], [1080, 1085], [1110, 1085]], palette.private)

detail.rect(2010, 530, 310, 280, "none", "#d4c3e5")
detail.text(2030, 545, "POWER PLATFORM", 18, 600, 280)
detail.box(2030, 610, 270, 175, "Copilot Studio", ["Published A2A agent", "Public SaaS endpoint", "MCS.InvokeAsA2A"], "copilot-studio", 4)
detail.arrow([[1820, 740], [2030, 740]])
detail.text(1860, 754, ["Public HTTPS", "A2A"], 17, 400, 165)

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

detail.arrow([[80, 350], [25, 350], [25, 1350], [675, 1350], [675, 1100], [720, 1100]], palette.private, true)
detail.text(60, 1180, ["Optional cloud path via VPN / ExpressRoute", "Browser to private ACA HTTPS"], 18, 400, 525)
detail.text(60, 1260, "Local and cloud hosts are alternatives.", 18, 400, 525)

detail.rect(1990, 850, 350, 545, "none", "#a6c5e8")
detail.text(2010, 868, "AZURE MANAGED SERVICES", 17, 600, 310)
const services = [
  { y: 900, title: "Key Vault", icon: "key-vault", body: ["Keys + approved secrets", "Managed identity"] },
  { y: 1050, title: "ACR Premium", icon: "acr", body: ["Private image registry", "Authorized image pulls"] },
  { y: 1200, title: "Azure Monitor", icon: "monitor", body: ["AMPLS + App Insights", "Private Log Analytics"] },
]
for (const service of services) {
  detail.box(2010, service.y, 310, 145, service.title, service.body, service.icon)
  detail.arrow([[1350, service.y + 35], [2010, service.y + 35]], palette.private, true)
  detail.text(1500, service.y - 30, "Private Link", 18, 400, 300)
}
detail.text(700, 1395, "Three required subnets + optional ACA subnet. Four PEs, with no additional ACA PE.", 18, 400, 1225)

detail.text(60, 1540, "CONNECTIVITY LEGEND", 18, 600, 500)
for (const [x, label, color, dashed] of [
  [60, "Local HTTP / JSON", palette.admin, false],
  [800, "Public HTTPS / A2A", palette.blue, false],
  [1540, "Private cloud traffic", palette.private, false],
]) {
  detail.arrow([[x, 1595], [x + 80, 1595]], color, dashed)
  detail.text(x + 100, 1579, label, 21, 400, 450)
}
detail.text(60, 1630, "Logical product labels only. Responses return through the same chain. Azure private access does not make Copilot Studio or Entra private.", 20, 400, 2240)
await detail.save("architecture", "Supplier–Buyer Agent Collaboration connectivity", "Default: local browser and Python server on your own computer at localhost:8000. The VNet has separate APIM, Foundry Hosted and shared private-endpoint subnets. Optional Container Apps hosting adds its own delegated environment subnet for private ingress and VNet egress to APIM and private services. The internal environment requires no additional private endpoint. Managed services remain outside the VNet. APIM Internal with Credential Manager, private Foundry and Studio A2A are the proposed cloud topology, not completed migration evidence. The separate token-exchange panel is omitted for readability.")
