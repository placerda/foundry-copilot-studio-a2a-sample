function element(tag, text) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  return node;
}

function safeLink(value) {
  try {
    const url = new URL(value);
    return ["https:", "http:"].includes(url.protocol) && !url.username && !url.password
      ? url.href : null;
  } catch { return null; }
}

// Deliberately small Markdown subset. Model text never becomes HTML or an element name.
function inline(parent, text) {
  const tokens = /(`[^`\n]+`|\*\*[^*\n]+\*\*|__[^_\n]+__|\[[^\]\n]+\]\([^\s)]+\)|\*[^*\n]+\*)/g;
  let offset = 0;
  for (const match of text.matchAll(tokens)) {
    parent.append(document.createTextNode(text.slice(offset, match.index)));
    const value = match[0];
    if (value.startsWith("`")) parent.append(element("code", value.slice(1, -1)));
    else if (value.startsWith("**") || value.startsWith("__")) {
      parent.append(element("b", value.slice(2, -2)));
    } else if (value.startsWith("*")) parent.append(element("em", value.slice(1, -1)));
    else {
      const link = /^\[([^\]]+)\]\((.+)\)$/.exec(value);
      const href = safeLink(link[2]);
      if (href) {
        const anchor = element("a", link[1]);
        anchor.href = href;
        anchor.target = "_blank";
        anchor.rel = "noopener noreferrer";
        parent.append(anchor);
      } else parent.append(document.createTextNode(value));
    }
    offset = match.index + value.length;
  }
  parent.append(document.createTextNode(text.slice(offset)));
}

const cells = line => line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map(cell => cell.trim());
const tableRule = line => /^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(line);

export function renderMarkdown(text) {
  const root = element("div");
  root.className = "content";
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  let paragraph = [];
  let list = null;
  const flush = () => {
    if (paragraph.length) {
      const node = element("p");
      inline(node, paragraph.join("\n"));
      root.append(node);
      paragraph = [];
    }
    list = null;
  };
  for (let index = 0; index < lines.length; index++) {
    const line = lines[index];
    if (/^\s*```/.test(line)) {
      flush();
      const code = [];
      while (++index < lines.length && !/^\s*```\s*$/.test(lines[index])) code.push(lines[index]);
      const pre = element("pre");
      pre.append(element("code", code.join("\n")));
      root.append(pre);
    } else if (index + 1 < lines.length && line.includes("|") && tableRule(lines[index + 1])) {
      flush();
      const wrapper = element("div");
      wrapper.className = "table-scroll";
      wrapper.tabIndex = 0;
      wrapper.setAttribute("role", "region");
      wrapper.setAttribute("aria-label", "Response table");
      const table = element("table");
      const head = element("thead");
      const heading = element("tr");
      for (const cell of cells(line)) {
        const th = element("th");
        th.scope = "col";
        inline(th, cell);
        heading.append(th);
      }
      head.append(heading);
      table.append(head);
      const body = element("tbody");
      index++;
      while (index + 1 < lines.length && lines[index + 1].includes("|") && lines[index + 1].trim()) {
        const row = element("tr");
        for (const cell of cells(lines[++index])) {
          const td = element("td");
          inline(td, cell);
          row.append(td);
        }
        body.append(row);
      }
      table.append(body);
      wrapper.append(table);
      root.append(wrapper);
    } else if (/^#{1,6}\s+/.test(line)) {
      flush();
      const match = /^(#{1,6})\s+(.+)$/.exec(line);
      const heading = element(match[1].length <= 2 ? "h3" : "h4");
      inline(heading, match[2]);
      root.append(heading);
    } else if (/^\s*(?:[-*+]|\d+\.)\s+/.test(line)) {
      const match = /^\s*([-*+]|\d+\.)\s+(.+)$/.exec(line);
      const tag = /^\d/.test(match[1]) ? "ol" : "ul";
      if (!list || list.tagName.toLowerCase() !== tag) {
        flush();
        list = element(tag);
        root.append(list);
      }
      const item = element("li");
      inline(item, match[2]);
      list.append(item);
    } else if (/^>\s?/.test(line)) {
      flush();
      const quote = element("blockquote");
      inline(quote, line.replace(/^>\s?/, ""));
      root.append(quote);
    } else if (!line.trim()) flush();
    else {
      if (list) flush();
      paragraph.push(line);
    }
  }
  flush();
  return root;
}
