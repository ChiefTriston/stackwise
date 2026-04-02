document.addEventListener("DOMContentLoaded", () => {
  const clean = (value = "") =>
    value.replace(/\u00A0/g, " ").replace(/\s+/g, " ").trim();

  const priceHtml = (price) => {
    const p = clean(price);
    if (!p) return 'See<span>details</span>';
    if (/^custom pricing$/i.test(p) || /^custom$/i.test(p)) return 'Custom<span>pricing</span>';
    if (/^contact/i.test(p)) return 'Contact<span>sales</span>';
    if (/^free$/i.test(p) || /^forever free/i.test(p)) return 'Free<span>plan</span>';
    return p
      .replace(/\/month/gi, '<span>/month</span>')
      .replace(/\/year/gi, '<span>/year</span>')
      .replace(/per seat/gi, '<span>per seat</span>');
  };

  const tagForPlan = (name) => {
    const v = clean(name).toLowerCase();
    if (v.includes("free")) return "Best for trying it out";
    if (v.includes("education") || v.includes("student")) return "Best for students";
    if (v.includes("enterprise") || v.includes("business")) return "Best for teams";
    if (v.includes("pro")) return "Best overall value";
    if (v.includes("plus")) return "Good starting tier";
    if (v.includes("starter")) return "Best entry paid tier";
    if (v.includes("creator")) return "Best for creators";
    if (v.includes("max")) return "Best for power users";
    if (v.includes("open-source")) return "Best for self-hosting";
    return "Compare features";
  };

  const badgeForPlan = (name) => {
    const v = clean(name).toLowerCase();
    if (v === "pro" || v === "pro plan" || v.includes("education pro")) return "Most people";
    return "";
  };

  const splitFeatures = (details) => {
    return clean(details)
      .split(/\s*,\s*|\s*;\s*/g)
      .map(clean)
      .filter(Boolean)
      .slice(0, 5);
  };

  const summarize = (details) => {
    const parts = clean(details).split(/\s*,\s*/).slice(0, 2);
    return clean(parts.join(", "));
  };

  const parsePriceAndDetails = (rest) => {
    const value = clean(rest).replace(/^[—\-:]\s*/, "");
    if (!value) return null;

    const colonIdx = value.indexOf(":");
    const dashIdx = value.indexOf(" - ");
    const emDashIdx = value.indexOf(" — ");

    if (colonIdx !== -1) {
      return {
        price: clean(value.slice(0, colonIdx)),
        details: clean(value.slice(colonIdx + 1))
      };
    }

    let splitIdx = -1;
    if (dashIdx !== -1) splitIdx = dashIdx;
    if (emDashIdx !== -1 && (splitIdx === -1 || emDashIdx < splitIdx)) splitIdx = emDashIdx;

    if (splitIdx !== -1) {
      return {
        price: clean(value.slice(0, splitIdx)),
        details: clean(value.slice(splitIdx + 3))
      };
    }

    return { price: value, details: "" };
  };

  const parseParagraph = (p) => {
    const clone = p.cloneNode(true);
    clone.querySelectorAll("a").forEach(a => a.remove());

    const strong = clone.querySelector("strong");
    const fullText = clean(clone.textContent);
    if (!fullText) return null;

    if (/^key differences:/i.test(fullText)) {
      return { type: "note", note: fullText.replace(/^key differences:\s*/i, "") };
    }
    if (/^annual billing/i.test(fullText) || /^all plans include/i.test(fullText)) {
      return { type: "note", note: fullText };
    }

    if (strong) {
      const name = clean(strong.textContent);
      let rest = fullText.replace(name, "").trim().replace(/^[—\-:]\s*/, "");
      const parsed = parsePriceAndDetails(rest);
      if (!parsed) return null;
      return { type: "plan", name, price: parsed.price, details: parsed.details };
    }

    const colonIdx = fullText.indexOf(":");
    if (colonIdx !== -1) {
      const name = clean(fullText.slice(0, colonIdx));
      const parsed = parsePriceAndDetails(fullText.slice(colonIdx + 1));
      if (!parsed) return null;
      return { type: "plan", name, price: parsed.price, details: parsed.details };
    }

    return null;
  };

  const parseCombinedParagraph = (text) => {
    const input = clean(text);
    if (!input) return [];

    const out = [];
    const regex = /([A-Z][A-Za-z0-9/&+().,' -]{1,40}):\s*((?:\$|Custom pricing|Custom|Free|Forever free)[^-—:]*?(?:\/month|\/year|per seat|savings|\)|only)?(?:\s+or\s+[^-—:]+?)?)\s*[-—]\s*(.*?)(?=\s+[A-Z][A-Za-z0-9/&+().,' -]{1,40}:\s*(?:\$|Custom pricing|Custom|Free|Forever free)|$)/g;

    let match;
    while ((match = regex.exec(input)) !== null) {
      out.push({
        type: "plan",
        name: clean(match[1]),
        price: clean(match[2]),
        details: clean(match[3])
      });
    }

    return out;
  };

  const pricingSections = [...document.querySelectorAll(".tool-section")].filter(section => {
    const title = section.querySelector(".tool-section-title");
    return title && /^pricing\b/i.test(clean(title.textContent));
  });

  pricingSections.forEach(section => {
    if (section.querySelector(".pricing-cards-wrap")) return;

    const linkEl = [...section.querySelectorAll("a")].find(a =>
      /pricing/i.test(clean(a.textContent))
    );

    const paragraphs = [...section.querySelectorAll("p")];
    const plans = [];
    const notes = [];

    paragraphs.forEach(p => {
      const parsed = parseParagraph(p);
      if (parsed?.type === "plan") plans.push(parsed);
      if (parsed?.type === "note") notes.push(parsed.note);
    });

    if (plans.length <= 1) {
      const combinedText = paragraphs.map(p => clean(p.textContent)).join(" ");
      parseCombinedParagraph(combinedText).forEach(item => plans.push(item));
    }

    if (!plans.length) return;

    const deduped = [];
    const seen = new Set();

    plans.forEach(plan => {
      const key = `${clean(plan.name)}|${clean(plan.price)}|${clean(plan.details)}`;
      if (!seen.has(key)) {
        seen.add(key);
        deduped.push(plan);
      }
    });

    const wrap = document.createElement("div");
    wrap.className = "pricing-cards-wrap";

    const grid = document.createElement("div");
    grid.className = "pricing-grid";

    deduped.forEach(plan => {
      const badge = badgeForPlan(plan.name);
      const tag = tagForPlan(plan.name);
      const features = splitFeatures(plan.details);
      const note = summarize(plan.details);
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
          <div class="pricing-price">${priceHtml(plan.price)}</div>
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

    paragraphs.forEach(p => p.classList.add("pricing-hide-original"));
    section.appendChild(wrap);
  });
});