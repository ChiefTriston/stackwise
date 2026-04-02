document.addEventListener("DOMContentLoaded", () => {
  const clean = (value) => (value || "").replace(/\s+/g, " ").trim();

  const isPlanLine = (line) => {
    const text = clean(line);
    if (!text) return false;
    if (/view full pricing details/i.test(text)) return false;
    if (!text.includes(":")) return false;
    if (!text.includes(" - ")) return false;
    return /\$/.test(text) || /free/i.test(text) || /custom/i.test(text) || /contact/i.test(text);
  };

  const pickSection = () => {
    const pricingLink = [...document.querySelectorAll("a")].find(a =>
      /view full pricing details/i.test(clean(a.textContent))
    );

    if (pricingLink) {
      let el = pricingLink.parentElement;
      while (el && el !== document.body) {
        const text = clean(el.textContent);
        const planLines = text.split(/\n+/).map(clean).filter(isPlanLine);
        if (/pricing/i.test(text) && planLines.length >= 2) return { section: el, pricingLink };
        el = el.parentElement;
      }
    }

    const candidates = [...document.querySelectorAll("section, article, div")];
    for (const el of candidates) {
      const text = clean(el.textContent);
      const planLines = text.split(/\n+/).map(clean).filter(isPlanLine);
      if (/pricing/i.test(text) && planLines.length >= 2) {
        return { section: el, pricingLink: null };
      }
    }

    return null;
  };

  const parsePlanLine = (line) => {
    const normalized = clean(line);
    const colonIndex = normalized.indexOf(":");
    const dashIndex = normalized.indexOf(" - ");

    if (colonIndex === -1 || dashIndex === -1 || dashIndex <= colonIndex) return null;

    const name = normalized.slice(0, colonIndex).trim();
    const price = normalized.slice(colonIndex + 1, dashIndex).trim();
    const details = normalized.slice(dashIndex + 3).trim();

    const features = details
      .split(/\s*,\s*/)
      .map(clean)
      .filter(Boolean)
      .slice(0, 5);

    return { name, price, details, features };
  };

  const tagForPlan = (name) => {
    const value = name.toLowerCase();
    if (value.includes("free")) return "Best for trying it out";
    if (value.includes("education") || value.includes("student")) return "Best for students";
    if (value.includes("enterprise")) return "Best for teams";
    if (value === "pro" || value === "pro plan") return "Best overall value";
    if (value.includes("max")) return "Best for power users";
    return "Compare features";
  };

  const badgeForPlan = (name) => {
    const value = name.toLowerCase();
    if (value === "pro" || value === "pro plan") return "Most people";
    return "";
  };

  const priceHtml = (price) => {
    if (/custom/i.test(price)) return `Custom<span>pricing</span>`;
    if (/contact/i.test(price)) return `Contact<span>sales</span>`;
    return `${price.replace(/\/month/gi, '<span>/month</span>').replace(/\/year/gi, '<span>/year</span>')}`;
  };

  const sectionInfo = pickSection();
  if (!sectionInfo) return;

  const { section, pricingLink } = sectionInfo;

  if (section.querySelector(".pricing-cards-wrap")) return;

  const planLines = clean(section.textContent)
    .split(/\n+/)
    .map(clean)
    .filter(isPlanLine);

  const plans = [...new Map(planLines.map(line => [line, parsePlanLine(line)])).values()].filter(Boolean);
  if (plans.length < 2) return;

  const wrap = document.createElement("div");
  wrap.className = "pricing-cards-wrap";

  const grid = document.createElement("div");
  grid.className = "pricing-grid";

  plans.forEach((plan) => {
    const badge = badgeForPlan(plan.name);
    const tag = tagForPlan(plan.name);
    const featuredClass = badge ? " featured" : "";

    const features = (plan.features.length ? plan.features : [plan.details])
      .slice(0, 5)
      .map(item => `<li>${item}</li>`)
      .join("");

    const note = plan.details.split(/,\s*/).slice(0, 2).join(", ");

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
      <p class="pricing-note">${note}</p>
      <ul class="pricing-list">${features}</ul>
    `;
    grid.appendChild(card);
  });

  wrap.appendChild(grid);

  if (pricingLink) {
    const linkClone = pricingLink.cloneNode(true);
    linkClone.classList.add("pricing-source-link");
    wrap.appendChild(linkClone);
  }

  const nodesToHide = [...section.querySelectorAll("p, li, div")];
  nodesToHide.forEach((node) => {
    const text = clean(node.textContent);
    if (isPlanLine(text)) {
      node.classList.add("pricing-hide-original");
    }
  });

  if (pricingLink && pricingLink.parentElement) {
    pricingLink.parentElement.classList.add("pricing-hide-original");
    pricingLink.parentElement.insertAdjacentElement("beforebegin", wrap);
  } else {
    section.appendChild(wrap);
  }
});