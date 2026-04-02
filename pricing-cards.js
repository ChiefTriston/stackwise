document.addEventListener("DOMContentLoaded", () => {
  const clean = (value = "") =>
    value
      .replace(/\u00A0/g, " ")
      .replace(/\s+/g, " ")
      .trim();

  const parsePriceDisplay = (price) => {
    const p = clean(price);
    if (!p) return 'Custom<span>pricing</span>';
    if (/^custom$/i.test(p) || /^custom pricing$/i.test(p)) return 'Custom<span>pricing</span>';
    if (/^contact/i.test(p)) return 'Contact<span>sales</span>';
    if (/^free$/i.test(p) || /^forever free/i.test(p)) return 'Free<span>plan</span>';
    return p
      .replace(/\/month/gi, '<span>/month</span>')
      .replace(/\/year/gi, '<span>/year</span>')
      .replace(/per seat/gi, '<span>per seat</span>');
  };

  const isPricingTitle = (el) => /^pricing\b/i.test(clean(el?.textContent || ""));

  const splitCombinedPlans = (text) => {
    let t = clean(text);
    if (!t) return [];

    t = t
      .replace(/\s*[–—]\s*/g, " — ")
      .replace(/\s+-\s+/g, " - ");

    const periodParts = t.split(/\.\s+(?=[A-Z][A-Za-z0-9/&+().,' -]{1,60}(?:\sPlan|\sTier|\sSuite|\sFree|\sPro|\sPlus|\sMax|\sBusiness|\sEnterprise|\sCreator|\sMarketer|\sStarter|\sWriter|\sAgency|\sEducation|\sCustom|\sDomains|\sAgents|\sBasic|\sProfessional|\sAdvanced|\sGenerative)?\s*[:—-])/g);

    const pieces = [];
    for (const part of periodParts) {
      const chunk = clean(part);
      if (!chunk) continue;

      const sub = chunk.split(/\s+(?=[A-Z][A-Za-z0-9/&+().,' -]{1,60}(?:\sPlan|\sTier|\sSuite|\sFree|\sPro|\sPlus|\sMax|\sBusiness|\sEnterprise|\sCreator|\sMarketer|\sStarter|\sWriter|\sAgency|\sEducation|\sCustom|\sDomains|\sAgents|\sBasic|\sProfessional|\sAdvanced|\sGenerative)?\s*[:—-]\s*(?:\$|Free|Forever free|Custom|Contact|From \$|Mid-tier|Additional cost))/g);
      for (const s of sub) {
        const item = clean(s);
        if (item) pieces.push(item);
      }
    }

    return pieces;
  };

  const parsePlanLine = (line) => {
    const text = clean(line);
    if (!text) return null;
    if (/^view full pricing details/i.test(text)) return null;
    if (/^key differences:/i.test(text)) return { type: "note", note: text.replace(/^key differences:\s*/i, "") };
    if (/^all plans include/i.test(text)) return { type: "note", note: text };
    if (/^annual billing/i.test(text)) return { type: "note", note: text };

    const patterns = [
      /^(?<name>.+?)\s*[—-]\s*(?<price>(?:\$|Free|Forever free|Custom|Contact|From \$|Mid-tier|Additional cost)[^:]{0,140}?)\s*:\s*(?<details>.+)$/i,
      /^(?<name>.+?)\s*:\s*(?<price>(?:\$|Free|Forever free|Custom|Contact|From \$|Mid-tier|Additional cost)[^-—]{0,140}?)\s*[—-]\s*(?<details>.+)$/i,
      /^(?<name>.+?)\s*[—-]\s*(?<price>(?:\$|Free|Forever free|Custom|Contact|From \$|Mid-tier|Additional cost).+)$/i,
      /^(?<name>.+?)\s*:\s*(?<price>(?:\$|Free|Forever free|Custom|Contact|From \$|Mid-tier|Additional cost).+)$/i
    ];

    for (const pattern of patterns) {
      const match = text.match(pattern);
      if (match?.groups) {
        return {
          type: "plan",
          name: clean(match.groups.name),
          price: clean(match.groups.price),
          details: clean(match.groups.details || "")
        };
      }
    }

    return null;
  };

  const makeFallbackPlan = (sectionText) => {
    const text = clean(sectionText);
    if (!text) return null;

    if (/open-source/i.test(text) && /free to self-host/i.test(text)) {
      return {
        type: "plan",
        name: "Open-source",
        price: "$0 core",
        details: "Free to self-host; API keys and VPS/server costs are separate."
      };
    }

    if (/\$/.test(text) || /custom/i.test(text) || /free/i.test(text)) {
      return {
        type: "plan",
        name: "Pricing",
        price: /custom/i.test(text) ? "Custom pricing" : "See details",
        details: text
      };
    }

    return null;
  };

  const tagForPlan = (name) => {
    const v = name.toLowerCase();
    if (v.includes("free")) return "Best for trying it out";
    if (v.includes("enterprise") || v.includes("business")) return "Best for teams";
    if (v.includes("education") || v.includes("student")) return "Best for students";
    if (v.includes("pro")) return "Best overall value";
    if (v.includes("plus")) return "Good starting tier";
    if (v.includes("max") || v.includes("advanced")) return "Best for power users";
    if (v.includes("starter")) return "Best entry paid tier";
    if (v.includes("creator")) return "Best for creators";
    if (v.includes("agency")) return "Best for agencies";
    if (v.includes("open-source")) return "Best for self-hosting";
    return "Compare features";
  };

  const badgeForPlan = (name) => {
    const v = name.toLowerCase();
    if (v === "pro" || v === "pro plan" || v.includes("pro ")) return "Most people";
    if (v.includes("starter")) return "Entry tier";
    return "";
  };

  const featureList = (details) => {
    const d = clean(details);
    if (!d) return [];

    return d
      .split(/\s*,\s*|\s*;\s*/g)
      .map(clean)
      .filter(Boolean)
      .slice(0, 5);
  };

  const summaryNote = (details) => {
    const d = clean(details);
    if (!d) return "";
    const parts = d.split(/\s*,\s*/).slice(0, 2);
    return clean(parts.join(", "));
  };

  const sections = [...document.querySelectorAll(".tool-section")].filter(section =>
    isPricingTitle(section.querySelector(".tool-section-title"))
  );

  sections.forEach((section) => {
    if (section.querySelector(".pricing-cards-wrap")) return;

    const linkEl = [...section.querySelectorAll("a")].find(a =>
      /pricing|github|plan/i.test(clean(a.textContent))
    );

    const candidateNodes = [...section.children].filter(node =>
      !node.classList.contains("tool-section-title") &&
      !node.classList.contains("pricing-cards-wrap")
    );

    const rawBlocks = [];

    candidateNodes.forEach((node) => {
      if (node.tagName === "A") return;

      if (node.matches("p, div")) {
        const text = clean(node.textContent);
        if (text) rawBlocks.push(text);
      }

      node.querySelectorAll?.("li").forEach((li) => {
        const text = clean(li.textContent);
        if (text) rawBlocks.push(text);
      });
    });

    let plans = [];
    let notes = [];

    rawBlocks.forEach((block) => {
      const pieces = splitCombinedPlans(block);
      if (!pieces.length) return;

      pieces.forEach((piece) => {
        const parsed = parsePlanLine(piece);
        if (!parsed) return;
        if (parsed.type === "note") notes.push(parsed.note);
        if (parsed.type === "plan") plans.push(parsed);
      });
    });

    if (!plans.length) {
      const fallback = makeFallbackPlan(rawBlocks.join(" "));
      if (fallback) plans.push(fallback);
    }

    const seen = new Set();
    plans = plans.filter((plan) => {
      const key = `${plan.name}|${plan.price}|${plan.details}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });

    if (!plans.length) return;

    const wrap = document.createElement("div");
    wrap.className = "pricing-cards-wrap";

    const grid = document.createElement("div");
    grid.className = "pricing-grid";

    plans.forEach((plan) => {
      const badge = badgeForPlan(plan.name);
      const tag = tagForPlan(plan.name);
      const features = featureList(plan.details);
      const note = summaryNote(plan.details);
      const featuredClass = badge ? " featured" : "";

      const card = document.createElement("div");
      card.className = `pricing-card${featuredClass}`;
      card.innerHTML = `
        ${badge ? `<div class="pricing-badge">${badge}</div>` : ""}
        <div class="pricing-top">
          <div>
            <div class="pricing-name">${plan.name}</div>
            <div class="pricing-tag">${tag}</div>
          </div>
          <div class="pricing-price">${parsePriceDisplay(plan.price)}</div>
        </div>
        ${note ? `<p class="pricing-note">${note}</p>` : ""}
        ${features.length ? `<ul class="pricing-list">${features.map(item => `<li>${item}</li>`).join("")}</ul>` : ""}
      `;
      grid.appendChild(card);
    });

    wrap.appendChild(grid);

    if (notes.length) {
      const noteEl = document.createElement("p");
      noteEl.className = "pricing-note";
      noteEl.style.marginTop = "14px";
      noteEl.textContent = notes.join(" ");
      wrap.appendChild(noteEl);
    }

    if (linkEl) {
      const linkClone = linkEl.cloneNode(true);
      linkClone.classList.add("pricing-source-link");
      wrap.appendChild(linkClone);
    }

    candidateNodes.forEach((node) => node.classList.add("pricing-hide-original"));
    section.appendChild(wrap);
  });
});