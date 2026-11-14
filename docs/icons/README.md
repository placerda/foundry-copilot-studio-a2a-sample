# Product icons

These unmodified Microsoft product icons are used only to identify the corresponding products in this sample's architecture diagrams.

Azure and Microsoft Foundry icons come from the [Azure Architecture Center icon collection](https://learn.microsoft.com/en-us/azure/architecture/icons/), SVG pack V25. The Copilot Studio icon comes from the [Microsoft Power Platform icon collection](https://learn.microsoft.com/en-us/power-platform/guidance/icons).

Microsoft permits copying, distributing, and displaying these icons in architectural diagrams, training materials, or documentation, and reserves all other rights. The icons are not the sample's logo and are not covered by any license for the sample's own code.

The Teams product icon is the unmodified
[Microsoft Fluent UI brand asset](https://res.cdn.office.net/files/fabric-cdn-prod_20221201.001/assets/brand-icons/product/svg/teams_48x1.svg).
See Microsoft's [architecture icon use terms](https://learn.microsoft.com/en-us/previous-versions/microsoft-365/solutions/architecture-icons-templates).

The current overview uses the Azure pack's API Center, Foundry Agent Service,
Foundry Project, AI Foundry, Browser, Users and Virtual Networks icons alongside
the existing APIM, Private Link, VPN, Storage, Key Vault, ACR and Monitor icons.
The Copilot Studio and Teams icons retain their original brand colors.
Only diagram borders and connectors use the Microsoft-blue palette.

The diagram generator preserves each icon's original shape and aspect ratio.
To regenerate the current overview's SVG, PNG and editable Excalidraw source
from the repository root:

```powershell
node tools\render-motors-architecture.mjs --png
```

Use the existing Node.js dependencies. PNG rendering requires an installed
Playwright Chromium browser. SVG and Excalidraw output can be generated without
a browser by omitting `--png`.

`tools/render-diagrams.mjs` regenerates the retained **historical**
Internal-APIM proposal assets, not the current Motors overview.
