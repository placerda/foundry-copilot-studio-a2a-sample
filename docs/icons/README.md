# Product icons

These unmodified Microsoft product icons are used only to identify the corresponding products in this sample's architecture diagrams.

Azure and Microsoft Foundry icons come from the [Azure Architecture Center icon collection](https://learn.microsoft.com/en-us/azure/architecture/icons/), SVG pack V25. The Copilot Studio icon comes from the [Microsoft Power Platform icon collection](https://learn.microsoft.com/en-us/power-platform/guidance/icons).

Microsoft permits copying, distributing, and displaying these icons in architectural diagrams, training materials, or documentation, and reserves all other rights. The icons are not the sample's logo and are not covered by any license for the sample's own code.

The diagram generator preserves each icon's original shape and aspect ratio. To regenerate both SVG, PNG, and editable Excalidraw files:

```powershell
npm ci
node tools\render-diagrams.mjs --png
```

PNG rendering requires an installed Playwright Chromium browser. SVG and Excalidraw output can be generated without a browser by omitting `--png`.
